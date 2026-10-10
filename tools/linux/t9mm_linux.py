#!/usr/bin/env python3
"""Run T9 Mod Manager on Linux, next to Black Ops Cold War.

T9 Mod Manager is a Windows program that works on the *running* game. On Linux
it therefore has to run inside the same Wine prefix, with the same Proton/Wine
build and in the same sandbox, as Black Ops Cold War. Otherwise it cannot see
the game. This helper finds that combination for Steam (native, Flatpak,
Snap), Lutris, Heroic and Bottles (native or Flatpak), and starts programs
there.

    t9mm_linux.py detect              list what was found, and known problems
    t9mm_linux.py install SETUP.exe   run the T9 Mod Manager installer in the game's prefix
    t9mm_linux.py run                 wait for the game, then start T9 Mod Manager beside it

It does not modify the game, the launcher, or any Steam/Lutris setting.
Standard library only, Python 3.8 or newer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

APP_ID = "1985810"  # Call of Duty: Black Ops Cold War on Steam
GAME_EXE = os.environ.get("T9MM_GAME_EXE", "BlackOpsColdWar.exe").lower()  # override only for testing
GAME_TITLE_RE = re.compile(r"black[\s_-]*ops[\s_-]*cold[\s_-]*war", re.I)
T9MM_RE = re.compile(r"t9[\s_-]*mod[\s_-]*manager", re.I)
SPACES_ISSUE = "https://github.com/FanaticSoftware/T9ModManager/issues/1"
RELEASES = "https://github.com/FanaticSoftware/T9ModManager/releases"
NOT_A_LAUNCHER_EXE = re.compile(r"(unins|uninstall|update|elevate|crashpad|squirrel|notification_helper)", re.I)
UMU_DEFAULT_GAMEID = "umu-default"  # what Lutris uses for games umu has no entry for (BOCW is one)

# Environment of the running game that a second program in the same prefix has
# to share: prefix and Proton/umu selection, the sync primitive (fsync, esync,
# ntsync; a mismatch with the running wineserver can hang both processes),
# Proton's per-game settings and the game id they are keyed on.
PROPAGATE_ENV = re.compile(
    r"^(WINEPREFIX|WINEARCH|WINEFSYNC|WINEESYNC|WINENTSYNC|WINE_DISABLE_FAST_SYNC|"
    r"WINE_LARGE_ADDRESS_AWARE|PROTONPATH|GAMEID|UMU_ID|STORE|PROTON_\w+|STEAM_COMPAT_\w+|"
    r"SteamAppId|SteamGameId|SteamOverlayGameId)$"
)
# Flatpak apps the game may be running in, and their sandboxed /tmp (which is
# where the wineserver socket lives).
FLATPAK_IDS = {"steam-flatpak": "com.valvesoftware.Steam", "lutris": "net.lutris.Lutris",
               "heroic": "com.heroicgameslauncher.hgl", "bottles": "com.usebottles.bottles"}


# --------------------------------------------------------------------------
# File formats
# --------------------------------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)|([^\s{}"]+)')
_VDF_ESCAPES = {"n": "\n", "t": "\t"}


def parse_vdf(text: str) -> dict:
    """Parse Valve KeyValues text (libraryfolders.vdf, appmanifest_*.acf, config.vdf).

    Keys are lower-cased because Steam compares them case-insensitively.
    """
    root: dict = {}
    stack = [root]
    key: Optional[str] = None
    for m in _VDF_TOKEN.finditer(text):
        quoted, brace, comment, bare = m.groups()
        if comment is not None:
            continue
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key.lower()] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            if quoted is not None:
                tok = re.sub(r"\\(.)", lambda e: _VDF_ESCAPES.get(e.group(1), e.group(1)), quoted)
            else:
                tok = bare
            if key is None:
                key = tok
            else:
                stack[-1][key.lower()] = tok
                key = None
    return root


def dig(tree: object, *keys: str) -> Optional[object]:
    node = tree
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k.lower())
    return node


def _reg_unescape(s: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "x":
                m = re.match(r"[0-9a-fA-F]{1,4}", s[i + 2:])
                if m:
                    out.append(chr(int(m.group(0), 16)))
                    i += 2 + len(m.group(0))
                    continue
            out.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


_REG_VALUE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(?:str\(2\):)?"((?:[^"\\]|\\.)*)"\s*$')


def parse_wine_reg(text: str) -> Dict[str, Dict[str, str]]:
    """Parse a Wine registry hive (system.reg, user.reg).

    Returns {lower-cased key path: {lower-cased value name: value}}. Only string
    values are kept; that is all the install lookups need.
    """
    keys: Dict[str, Dict[str, str]] = {}
    cur: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        if line.startswith("["):
            end = line.rfind("]")
            cur = keys.setdefault(_reg_unescape(line[1:end]).lower(), {}) if end > 0 else None
            continue
        if cur is not None and line.startswith('"'):
            m = _REG_VALUE.match(line)
            if m:
                cur[_reg_unescape(m.group(1)).lower()] = _reg_unescape(m.group(2))
    return keys


_UNINSTALL_ROOTS = (
    "software\\microsoft\\windows\\currentversion\\uninstall\\",
    "software\\wow6432node\\microsoft\\windows\\currentversion\\uninstall\\",
)


def uninstall_entries(prefix: Path, name_re: "re.Pattern[str]") -> Iterator[Dict[str, str]]:
    """Add/Remove Programs entries in a prefix whose DisplayName matches.

    This is the Linux stand-in for the registry lookups a Windows program would
    do: Wine keeps the registry as plain text files in the prefix.
    """
    for hive in ("user.reg", "system.reg"):
        try:
            reg = parse_wine_reg((prefix / hive).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for key, values in reg.items():
            if key.startswith(_UNINSTALL_ROOTS) and name_re.search(values.get("displayname", "")):
                yield values


def read_simple_yaml(text: str) -> Dict[str, Dict[str, str]]:
    """Just enough YAML for Lutris and Bottles configs.

    `section:` lines start a section; indented `key: value` pairs below it are
    collected into that section, nested levels flattened. Top-level scalars go
    under the "" section.
    """
    out: Dict[str, Dict[str, str]] = {"": {}}
    section = ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([^:#]+?):\s*(.*?)\s*$", raw)
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2).strip(), m.group(3)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if indent == 0:
            if value == "":
                section = key
                out.setdefault(section, {})
            else:
                out[""][key] = value
                section = ""
        elif section and value:
            out[section].setdefault(key, value)
    return out


# --------------------------------------------------------------------------
# Paths. Linux is case-sensitive; Windows, and so the registry and every path
# a Windows program reports, is not.
# --------------------------------------------------------------------------

def ci_resolve(base: Path, parts: Iterable[str]) -> Optional[Path]:
    """Join `parts` onto `base`, matching each component case-insensitively."""
    cur = base
    for part in parts:
        if part in ("", "."):
            continue
        direct = cur / part
        if direct.exists():
            cur = direct
            continue
        try:
            with os.scandir(cur) as it:
                match = next((e.name for e in it if e.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        cur = cur / match
    return cur


def win_to_linux(prefix: Path, winpath: str) -> Optional[Path]:
    """Map a Windows path seen inside `prefix` (C:\\..., Z:\\...) to a Linux path."""
    p = winpath.strip().strip('"')
    if p.startswith("\\\\?\\"):
        p = p[4:]
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", p)
    if not m:
        return None
    letter, rest = m.group(1).lower(), m.group(2)
    parts = [x for x in re.split(r"[\\/]+", rest) if x]
    if letter == "c" and (prefix / "drive_c").is_dir():
        base = prefix / "drive_c"
    else:
        device = prefix / "dosdevices" / (letter + ":")
        if not device.exists():
            return None
        base = device.resolve()
    return ci_resolve(base, parts) or base.joinpath(*parts)


def linux_to_win(prefix: Path, path: Path) -> str:
    """Best-effort Windows spelling of a Linux path, as programs in `prefix` see it."""
    try:
        rel = path.resolve().relative_to((prefix / "drive_c").resolve())
        return "C:\\" + "\\".join(rel.parts)
    except (ValueError, OSError):
        return "Z:" + str(path).replace("/", "\\")


def exe_from_command(cmd: str) -> str:
    """The program in an UninstallString such as '"C:\\x\\Uninstall y.exe" /currentuser'."""
    cmd = cmd.strip()
    m = re.match(r'^"([^"]+)"', cmd) or re.match(r"^(.+?\.exe)\b", cmd, re.I)
    return m.group(1) if m else cmd


# --------------------------------------------------------------------------
# What we find
# --------------------------------------------------------------------------

@dataclass
class Runner:
    """How programs are started in a prefix.

    proton: a Proton folder; started with its own `proton runinprefix` verb.
    umu:    umu-launcher (Lutris, Heroic) with PROTONPATH = a Proton folder or
            "GE-Proton"; started with PROTON_VERB=runinprefix.
    wine:   a plain wine binary.
    """
    kind: str
    path: Path
    version: str = ""
    source: str = ""
    protonpath: str = ""

    def describe(self) -> str:
        if self.kind == "umu":
            if self.protonpath == "GE-Proton":
                what = "umu + GE-Proton (latest)"
            else:
                what = f"umu + {Path(self.protonpath).name if '/' in self.protonpath else self.protonpath}"
        elif self.kind == "proton":
            what = self.path.name if self.path.name.lower().startswith("proton") else f"Proton {self.path.name}"
        else:
            what = f"Wine {self.path}"
        ver = f" ({self.version})" if self.version and self.version.lower() not in what.lower() else ""
        return f"{what}{ver}  [from {self.source}]"


@dataclass
class Install:
    source: str                          # steam, steam-flatpak, steam-snap, lutris, heroic, bottles, manual
    label: str
    prefix: Path                         # the WINEPREFIX (the folder holding drive_c)
    game_dir: Optional[Path] = None
    runner: Optional[Runner] = None
    compat_data: Optional[Path] = None   # Proton: the STEAM_COMPAT_DATA_PATH
    steam_root: Optional[Path] = None
    flatpak_app: Optional[str] = None    # run inside this Flatpak app's sandbox
    env: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "source": self.source, "label": self.label, "prefix": str(self.prefix),
            "game_dir": str(self.game_dir) if self.game_dir else None,
            "runner": None if not self.runner else {
                "kind": self.runner.kind, "path": str(self.runner.path), "version": self.runner.version,
                "protonpath": self.runner.protonpath or None, "source": self.runner.source},
            "compat_data": str(self.compat_data) if self.compat_data else None,
            "flatpak_app": self.flatpak_app, "notes": self.notes,
        }


def _expand(home: Path, p: str) -> Path:
    return home / p[2:] if p.startswith("~/") else Path(p)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def proton_version(folder: Path) -> str:
    """A Proton build's version; its `version` file reads '<build timestamp> <version>'."""
    parts = _read(folder / "version").split()
    return parts[-1] if parts else ""


def _flatpak_for(path: Path, home: Path) -> Optional[str]:
    """The Flatpak app id if `path` is inside ~/.var/app/<id>/."""
    try:
        rel = path.resolve().relative_to((home / ".var" / "app").resolve())
        return rel.parts[0] if rel.parts else None
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# Steam
# --------------------------------------------------------------------------

STEAM_ROOTS = (
    ("steam", "~/.local/share/Steam"),
    ("steam", "~/.steam/steam"),
    ("steam", "~/.steam/root"),
    ("steam-flatpak", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"),
    ("steam-flatpak", "~/.var/app/com.valvesoftware.Steam/data/Steam"),
    ("steam-snap", "~/snap/steam/common/.local/share/Steam"),
)
SYSTEM_COMPAT_TOOL_DIRS = ("/usr/share/steam/compatibilitytools.d", "/usr/local/share/steam/compatibilitytools.d")
# Folder names of Valve's own Proton builds that do not follow "Proton N.0".
_OFFICIAL_PROTON = {"experimental": "Proton - Experimental", "hotfix": "Proton Hotfix",
                    "37": "Proton 3.7", "316": "Proton 3.16", "42": "Proton 4.2", "411": "Proton 4.11",
                    "5": "Proton 5.0", "513": "Proton 5.13", "63": "Proton 6.3"}


def steam_roots(home: Path, extra: Sequence[Path] = ()) -> List[Tuple[str, Path]]:
    seen, out = set(), []
    candidates = [("steam", Path(p)) for p in extra] + [(k, _expand(home, p)) for k, p in STEAM_ROOTS]
    for kind, p in candidates:
        if not (p / "steamapps").is_dir():
            continue
        real = p.resolve()
        if real not in seen:
            seen.add(real)
            out.append((kind, real))
    return out


def steam_libraries(root: Path) -> List[Path]:
    libs = [root]
    for vdf in (root / "steamapps" / "libraryfolders.vdf", root / "config" / "libraryfolders.vdf"):
        folders = dig(parse_vdf(_read(vdf)), "libraryfolders")
        if isinstance(folders, dict):
            for entry in folders.values():
                path = entry.get("path") if isinstance(entry, dict) else entry  # old format: "1" "/path"
                if isinstance(path, str) and path:
                    libs.append(Path(path))
    seen, out = set(), []
    for lib in libs:
        if (lib / "steamapps").is_dir() and lib.resolve() not in seen:
            seen.add(lib.resolve())
            out.append(lib)
    return out


def proton_from_config_info(compat: Path) -> Optional[Tuple[Path, str]]:
    """The Proton folder and version a prefix last ran with, as Proton itself recorded it.

    Works for Steam's compatdata/<appid> and for umu-managed prefixes (Lutris,
    Heroic), which Proton treats the same way.
    """
    lines = _read(compat / "config_info").splitlines()
    version = (lines[0].strip() if lines else "") or _read(compat / "version").strip()
    for line in lines[1:]:
        m = re.match(r"^(.*?)/(?:files|dist)/", line.strip())
        if m and (Path(m.group(1)) / "proton").is_file():
            return Path(m.group(1)), version
    return None


def _compat_tool_dirs(root: Path, home: Path) -> Iterator[Path]:
    for base in [root / "compatibilitytools.d", home / ".steam" / "root" / "compatibilitytools.d",
                 *map(Path, SYSTEM_COMPAT_TOOL_DIRS)]:
        try:
            yield from sorted(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue


def find_compat_tool(name: str, root: Path, libraries: Sequence[Path], home: Path) -> Optional[Path]:
    """Folder of the compatibility tool Steam calls `name` (proton_experimental, GE-Proton9-20, ...)."""
    for d in _compat_tool_dirs(root, home):
        tools = dig(parse_vdf(_read(d / "compatibilitytool.vdf")), "compatibilitytools", "compat_tools")
        if isinstance(tools, dict) and name.lower() in tools:
            entry = tools[name.lower()]
            install = entry.get("install_path", ".") if isinstance(entry, dict) else "."
            return (d / install).resolve()
    m = re.match(r"^proton_(\w+)$", name, re.I)
    if m:
        key = m.group(1).lower()
        folder = _OFFICIAL_PROTON.get(key, f"Proton {key}.0" if key.isdigit() else None)
        for lib in libraries:
            if folder and (lib / "steamapps" / "common" / folder / "proton").is_file():
                return lib / "steamapps" / "common" / folder
    return None


def runner_from_mapping(root: Path, libraries: Sequence[Path], home: Path) -> Optional[Runner]:
    """The compatibility tool chosen for the game (or for all games) in Steam's settings."""
    cfg = parse_vdf(_read(root / "config" / "config.vdf"))
    mapping = dig(cfg, "installconfigstore", "software", "valve", "steam", "compattoolmapping")
    if not isinstance(mapping, dict):
        return None
    for appid, why in ((APP_ID, "Steam compatibility setting for the game"), ("0", "Steam default compatibility tool")):
        entry = mapping.get(appid)
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            d = find_compat_tool(name, root, libraries, home)
            if d and (d / "proton").is_file():
                return Runner("proton", d, name, why)
    return None


def newest_proton(libraries: Sequence[Path]) -> Optional[Runner]:
    found = [d for lib in libraries for d in (lib / "steamapps" / "common").glob("Proton*") if (d / "proton").is_file()]
    if not found:
        return None
    exp = [d for d in found if "experimental" in d.name.lower()]
    best = exp[0] if exp else sorted(found, key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)] or [0])[-1]
    return Runner("proton", best, "", "guess: newest installed Proton")


def detect_steam(home: Path, extra_roots: Sequence[Path] = ()) -> List[Install]:
    installs = []
    for kind, root in steam_roots(home, extra_roots):
        libraries = steam_libraries(root)
        for lib in libraries:
            manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
            if not manifest.is_file():
                continue
            state = dig(parse_vdf(_read(manifest)), "appstate")
            installdir = state.get("installdir", "") if isinstance(state, dict) else ""
            game_dir = ci_resolve(lib / "steamapps" / "common", [installdir]) if installdir else None
            compat = next((c for c in (lib / "steamapps" / "compatdata" / APP_ID,
                                       root / "steamapps" / "compatdata" / APP_ID) if c.is_dir()),
                          lib / "steamapps" / "compatdata" / APP_ID)
            inst = Install(kind, f"Steam ({lib})", compat / "pfx", game_dir=game_dir, compat_data=compat,
                           steam_root=root, flatpak_app=FLATPAK_IDS.get(kind))
            if not (compat / "pfx" / "drive_c").is_dir():
                inst.notes.append("The game's Proton prefix does not exist yet. Start Black Ops Cold War once "
                                  "from Steam, quit, then run this again.")
            recorded = proton_from_config_info(compat)
            inst.runner = (Runner("proton", recorded[0], recorded[1], "compatdata/config_info") if recorded else None) \
                or runner_from_mapping(root, libraries, home) or newest_proton(libraries)
            if inst.runner and inst.runner.source.startswith("guess"):
                inst.notes.append("Could not tell which Proton the game uses, so guessed the newest one. "
                                  "Start the game once so Proton records it.")
            if kind == "steam-flatpak":
                inst.notes.append("Flatpak Steam support is untested. If it does not work, use Protontricks from "
                                  f"Flathub: flatpak run --command=protontricks-launch com.github.Matoking.protontricks "
                                  f"--appid {APP_ID} <T9 Mod Manager .exe>")
            elif kind == "steam-snap":
                inst.notes.append("Snap Steam runs the game in a sandbox this helper cannot enter; this is untested.")
            installs.append(inst)
    return installs


# --------------------------------------------------------------------------
# Lutris, Heroic, Bottles (Battle.net installs)
# --------------------------------------------------------------------------

def game_in_prefix(prefix: Path) -> Optional[Path]:
    """Black Ops Cold War's folder inside a Wine prefix, if it is installed there."""
    for values in uninstall_entries(prefix, GAME_TITLE_RE):
        loc = values.get("installlocation", "")
        p = win_to_linux(prefix, loc) if loc else None
        if p and p.is_dir():
            return p
    for base in ("Program Files (x86)", "Program Files", "Games"):
        d = ci_resolve(prefix / "drive_c", [base])
        if not d:
            continue
        try:
            for child in d.iterdir():
                if GAME_TITLE_RE.search(child.name) and ci_resolve(child, [GAME_EXE]):
                    return child
        except OSError:
            continue
    return None


def _wine_binary(d: Path) -> Optional[Path]:
    for rel in ("bin/wine", "files/bin/wine", "dist/bin/wine"):
        if (d / rel).is_file():
            return d / rel
    return None


def find_umu(home: Path) -> Optional[Path]:
    """umu-launcher's entry point, preferring the copy Lutris manages."""
    for d in LUTRIS_DIRS:
        for name in ("umu-run", "umu_run.py"):
            p = _expand(home, d) / "runtime" / "umu" / name
            if p.is_file():
                return p
    found = shutil.which("umu-run")
    if found:
        return Path(found)
    for base in ("/usr/share", "/usr/local/share", "/opt"):
        for name in ("umu-run", "umu_run.py"):
            if (Path(base) / "umu" / name).is_file():
                return Path(base) / "umu" / name
    return None


LUTRIS_DIRS = ("~/.local/share/lutris", "~/.config/lutris",
               "~/.var/app/net.lutris.Lutris/data/lutris", "~/.var/app/net.lutris.Lutris/config/lutris")


def _lutris_names(data_dir: Path) -> Dict[str, str]:
    db = data_dir / "pga.db"
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return {cfg: name for name, cfg in con.execute("SELECT name, configpath FROM games") if cfg}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _lutris_runner(version: str, data_dir: Path, home: Path, why: str) -> Tuple[Optional[Runner], List[str]]:
    """Lutris' choice of Wine runner -> how to start programs the same way."""
    notes: List[str] = []
    umu = find_umu(home)
    if version.lower() == "ge-proton":  # Lutris' "GE-Proton (Latest)": umu picks and fetches it
        if umu:
            return Runner("umu", umu, "GE-Proton (latest)", why, protonpath="GE-Proton"), notes
        return None, ["Lutris uses GE-Proton through umu, but umu-launcher was not found."]
    runner_dir = data_dir / "runners" / "wine" / version
    if (runner_dir / "proton").is_file():  # a Proton build: Lutris runs it through umu
        if umu:
            return Runner("umu", umu, version, why, protonpath=str(runner_dir)), notes
        notes.append("umu-launcher was not found; starting Proton directly instead.")
        return Runner("proton", runner_dir, version, why), notes
    wine = _wine_binary(runner_dir)
    if wine:
        return Runner("wine", wine, version, why), notes
    return None, [f"Lutris runner '{version}' was not found in {data_dir / 'runners' / 'wine'}."]


def detect_lutris(home: Path) -> List[Install]:
    data_dirs = [_expand(home, d) for d in LUTRIS_DIRS]
    names: Dict[str, str] = {}
    for d in data_dirs:
        names.update(_lutris_names(d))
    # Several Lutris entries can share a prefix (Battle.net and the game itself).
    # Prefer the game's own entry: its runner settings are what the game starts with.
    by_prefix: Dict[Path, Tuple[int, Path, Path, Path, Dict[str, Dict[str, str]]]] = {}
    for d in data_dirs:
        for cfg in sorted((d / "games").glob("*.yml")):
            y = read_simple_yaml(_read(cfg))
            prefix_s = y.get("game", {}).get("prefix") or y.get("wine", {}).get("prefix")
            if not prefix_s:
                continue
            prefix = _expand(home, prefix_s)
            title = " ".join([cfg.stem, y[""].get("name", ""), y[""].get("game_slug", ""), names.get(cfg.stem, "")])
            rank = 0 if GAME_TITLE_RE.search(title) else 1
            key = prefix.resolve()
            if key not in by_prefix or rank < by_prefix[key][0]:
                by_prefix[key] = (rank, cfg, d, prefix, y)
    installs = []
    for rank, cfg, data_dir, prefix, y in by_prefix.values():
        game_dir = game_in_prefix(prefix)
        if not game_dir:
            continue
        inst = Install("lutris", f"Lutris: {names.get(cfg.stem) or y[''].get('name') or cfg.stem}", prefix,
                       game_dir=game_dir, flatpak_app=_flatpak_for(data_dir, home))
        version = y.get("wine", {}).get("version", "")
        why = f"Lutris config {cfg.name}"
        if not version:
            version = read_simple_yaml(_read(data_dir / "runners" / "wine.yml")).get("wine", {}).get("version", "")
            why = "Lutris default Wine runner (runners/wine.yml)"
        if version:
            inst.runner, notes = _lutris_runner(version, data_dir, home, why)
            inst.notes.extend(notes)
        else:
            inst.notes.append("Could not tell which Wine runner Lutris uses for this game; pass --runner.")
        # Settings that change the sync primitive must match the running game.
        wine_cfg = y.get("wine", {})
        if wine_cfg.get("fsync", "").lower() == "false":
            inst.env.update({"PROTON_NO_FSYNC": "1", "WINEFSYNC": "0"})
        if wine_cfg.get("esync", "").lower() == "false":
            inst.env.update({"PROTON_NO_ESYNC": "1", "WINEESYNC": "0"})
        inst.env.update({k: v for k, v in y.get("system", {}).items() if PROPAGATE_ENV.match(k)})
        if inst.runner and inst.runner.kind == "umu":
            inst.env.setdefault("GAMEID", inst.env.get("UMU_ID", UMU_DEFAULT_GAMEID))
        if inst.runner and inst.runner.kind == "proton":
            inst.compat_data = prefix
        installs.append(inst)
    return installs


HEROIC_DIRS = ("~/.config/heroic", "~/.var/app/com.heroicgameslauncher.hgl/config/heroic")


def detect_heroic(home: Path) -> List[Install]:
    installs = []
    for d in (_expand(home, x) for x in HEROIC_DIRS):
        titles: Dict[str, str] = {}
        try:
            for g in json.loads(_read(d / "sideload_apps" / "library.json") or "{}").get("games", []):
                titles[g.get("app_name", "")] = g.get("title", "")
        except (ValueError, AttributeError):
            pass
        for cfg in sorted((d / "GamesConfig").glob("*.json")):
            try:
                data = json.loads(_read(cfg) or "{}")
            except ValueError:
                continue
            for app, conf in data.items():
                if not isinstance(conf, dict) or not conf.get("winePrefix"):
                    continue
                base = _expand(home, conf["winePrefix"])
                wv = conf.get("wineVersion") or {}
                prefix = base / "pfx" if (base / "pfx" / "drive_c").is_dir() and (base / "pfx").resolve() != base.resolve() else base
                game_dir = game_in_prefix(prefix)
                if not game_dir:
                    continue
                inst = Install("heroic", f"Heroic: {titles.get(app) or app}", prefix, game_dir=game_dir,
                               flatpak_app=_flatpak_for(d, home))
                binp = Path(wv["bin"]) if wv.get("bin") else None
                if binp and wv.get("type") == "proton" and binp.name == "proton":
                    inst.runner = Runner("proton", binp.parent, wv.get("name", ""), f"Heroic config {cfg.name}")
                    inst.compat_data = base
                elif binp:
                    inst.runner = Runner("wine", binp, wv.get("name", ""), f"Heroic config {cfg.name}")
                installs.append(inst)
    return installs


BOTTLES_DIRS = ("~/.local/share/bottles", "~/.var/app/com.usebottles.bottles/data/bottles")


def detect_bottles(home: Path) -> List[Install]:
    installs = []
    for d in (_expand(home, x) for x in BOTTLES_DIRS):
        for yml in sorted((d / "bottles").glob("*/bottle.yml")):
            prefix = yml.parent
            game_dir = game_in_prefix(prefix)
            if not game_dir:
                continue
            top = read_simple_yaml(_read(yml)).get("", {})
            inst = Install("bottles", f"Bottles: {top.get('Name', prefix.name)}", prefix, game_dir=game_dir,
                           flatpak_app=_flatpak_for(d, home))
            runner = top.get("Runner", "")
            wine = _wine_binary(d / "runners" / runner) if runner else None
            if wine:  # Bottles runs every runner, Proton builds included, as plain wine on the bottle
                inst.runner = Runner("wine", wine, runner, "bottle.yml")
            elif runner:
                inst.notes.append(f"Bottles runner '{runner}' was not found on disk.")
            installs.append(inst)
    return installs


# --------------------------------------------------------------------------
# The running game
# --------------------------------------------------------------------------

@dataclass
class RunningGame:
    pid: int
    env: Dict[str, str]
    exe_link: str = ""


def find_running_game(proc: Path = Path("/proc")) -> Optional[RunningGame]:
    """The Black Ops Cold War process, if it is running as this user."""
    try:
        pids = sorted((p for p in proc.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return None
    for p in pids:
        try:
            argv = [a for a in (p / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        # Wine shows a Windows program's own path as argv[0]; wrappers (proton,
        # umu, steam.exe, the reaper) only carry it as an argument.
        if not argv or not argv[0].replace(b"\\", b"/").lower().endswith(GAME_EXE.encode()):
            continue
        env: Dict[str, str] = {}
        try:
            for item in (p / "environ").read_bytes().split(b"\0"):
                k, sep, v = item.partition(b"=")
                if sep:
                    env[k.decode(errors="replace")] = v.decode(errors="replace")
        except OSError:
            pass
        try:
            link = os.readlink(p / "exe")
        except OSError:
            link = ""
        return RunningGame(int(p.name), env, link)
    return None


def install_from_running(game: RunningGame, known: Sequence[Install], home: Path) -> Optional[Install]:
    """Everything needed to join the running game's Wine session, read from the game itself."""
    env = game.env
    prefix_s = env.get("WINEPREFIX") or (env["STEAM_COMPAT_DATA_PATH"] + "/pfx" if env.get("STEAM_COMPAT_DATA_PATH") else "")
    if not prefix_s:
        return None
    prefix = Path(prefix_s.rstrip("/"))
    match = next((i for i in known if i.prefix.resolve() == prefix.resolve()), None)
    flatpak = env.get("FLATPAK_ID") or None
    inst = Install(match.source if match else "running", f"the running game (pid {game.pid})", prefix,
                   game_dir=match.game_dir if match else None, flatpak_app=flatpak,
                   steam_root=Path(env["STEAM_COMPAT_CLIENT_INSTALL_PATH"]) if env.get("STEAM_COMPAT_CLIENT_INSTALL_PATH")
                   else (match.steam_root if match else None),
                   env={k: v for k, v in env.items() if PROPAGATE_ENV.match(k)})
    inst.env.pop("PROTON_VERB", None)
    umu = find_umu(home)
    if env.get("PROTONPATH") and umu:  # started through umu (Lutris, Heroic, umu-run)
        inst.runner = Runner("umu", umu, Path(env["PROTONPATH"]).name, "the running game", protonpath=env["PROTONPATH"])
        inst.env.setdefault("GAMEID", UMU_DEFAULT_GAMEID)
    else:
        for tool in env.get("STEAM_COMPAT_TOOL_PATHS", "").split(":"):
            if tool and (Path(tool) / "proton").is_file():
                inst.runner = Runner("proton", Path(tool), proton_version(Path(tool)), "the running game")
                break
    if inst.runner and inst.runner.kind == "proton":
        inst.compat_data = Path(env["STEAM_COMPAT_DATA_PATH"]) if env.get("STEAM_COMPAT_DATA_PATH") else (
            prefix if (prefix / "pfx").exists() else prefix.parent if prefix.name == "pfx" else None)
    if not inst.runner and match and match.runner:
        inst.runner, inst.compat_data = match.runner, match.compat_data
        inst.env = {**match.env, **inst.env}
    if not inst.runner and game.exe_link:
        loader = Path(game.exe_link)
        if loader.name.startswith(("wine", "wine64")):
            wine = next((loader.parent / n for n in ("wine", "wine64") if (loader.parent / n).is_file()), None)
            if wine:
                inst.runner = Runner("wine", wine, "", "the running game's Wine")
    return inst


# --------------------------------------------------------------------------
# T9 Mod Manager inside a prefix
# --------------------------------------------------------------------------

def launcher_exes(folder: Path) -> List[Path]:
    try:
        exes = [p for p in folder.iterdir() if p.suffix.lower() == ".exe" and p.is_file()]
    except OSError:
        return []
    exes = [p for p in exes if not NOT_A_LAUNCHER_EXE.search(p.name)]
    return sorted(exes, key=lambda p: (not T9MM_RE.search(p.stem), "t9" not in p.name.lower(),
                                       "manager" not in p.name.lower(), p.name.lower()))


def _user_dirs(prefix: Path) -> List[Path]:
    users = ci_resolve(prefix / "drive_c", ["users"])
    try:
        return [u for u in users.iterdir() if u.is_dir() and u.name.lower() != "public"] if users else []
    except OSError:
        return []


def find_t9mm(prefix: Path) -> List[Path]:
    """T9 Mod Manager executables inside a prefix, most likely first."""
    found: List[Path] = []
    for values in uninstall_entries(prefix, T9MM_RE):
        icon = values.get("displayicon", "").split(",")[0].strip().strip('"')
        if icon.lower().endswith(".exe") and not NOT_A_LAUNCHER_EXE.search(re.split(r"[\\/]", icon)[-1]):
            p = win_to_linux(prefix, icon)
            if p and p.is_file():
                found.append(p)
        for folder in (values.get("installlocation", ""), values.get("inno setup: app path", ""),
                       exe_from_command(values.get("uninstallstring", "")).rpartition("\\")[0]):
            d = win_to_linux(prefix, folder) if folder else None
            if d and d.is_dir():
                found.extend(launcher_exes(d))
    if not found:
        roots = [prefix / "drive_c"]
        for base in ("Program Files", "Program Files (x86)", "Games"):
            d = ci_resolve(prefix / "drive_c", [base])
            if d:
                roots.append(d)
        for u in _user_dirs(prefix):
            for rel in (["AppData", "Local", "Programs"], ["AppData", "Local"], ["AppData", "Roaming"],
                        ["Desktop"], ["Documents"], ["Downloads"]):
                d = ci_resolve(u, rel)
                if d:
                    roots.append(d)
        for r in roots:
            try:
                for child in r.iterdir():
                    if child.is_dir() and T9MM_RE.search(child.name):
                        found.extend(launcher_exes(child))
            except OSError:
                continue
    out, seen = [], set()
    for p in found:
        if p.resolve() not in seen:
            seen.add(p.resolve())
            out.append(p)
    return out


# --------------------------------------------------------------------------
# Starting programs in the prefix
# --------------------------------------------------------------------------

def build_command(inst: Install, program: str, args: Sequence[str] = ()) -> Tuple[List[str], Dict[str, str]]:
    """Command line and extra environment that start `program` in the game's prefix.

    Every runner uses Proton's `runinprefix` verb where Proton is involved: it
    applies the settings the game got (sync primitive, per-game fixes) but
    never takes the prefix lock or re-initialises the prefix, so it is safe
    while the game runs. Lutris does the same for a second program.

    When the game runs in a Flatpak sandbox, the command runs in that app's
    sandbox too: Flatpak gives each app its own /tmp, and the wineserver socket
    lives there. Started outside, Wine would not find the game's wineserver and
    would start a second one on the same prefix.
    """
    r = inst.runner
    if not r:
        raise LookupError("no Wine or Proton build is known for this install")
    env = dict(inst.env)
    if r.kind == "proton":
        compat = inst.compat_data or (inst.prefix.parent if inst.prefix.name == "pfx" else None)
        if compat is None:
            raise LookupError(f"{inst.prefix} is not a Proton prefix (no pfx folder)")
        env["STEAM_COMPAT_DATA_PATH"] = str(compat)
        env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(inst.steam_root or ""))
        if inst.source.startswith("steam"):
            for k in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID"):
                env.setdefault(k, APP_ID)
        cmd = [str(r.path / "proton"), "runinprefix", program, *args]
    elif r.kind == "umu":
        env["WINEPREFIX"] = str(inst.prefix)
        env["PROTONPATH"] = env.get("PROTONPATH") or r.protonpath
        env.setdefault("GAMEID", UMU_DEFAULT_GAMEID)
        env.setdefault("WINEARCH", "win64")
        env["PROTON_VERB"] = "runinprefix"
        cmd = [str(r.path), program, *args]
    else:
        env["WINEPREFIX"] = str(inst.prefix)
        cmd = [str(r.path), program, *args]
    if inst.flatpak_app:
        cmd = (["flatpak", "run", f"--command={cmd[0]}"] + [f"--env={k}={v}" for k, v in sorted(env.items())]
               + [inst.flatpak_app] + cmd[1:])
        env = {}
    return cmd, env


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def detect_all(home: Path, extra_roots: Sequence[Path] = ()) -> List[Install]:
    return detect_steam(home, extra_roots) + detect_lutris(home) + detect_heroic(home) + detect_bottles(home)


def check_launcher_path(prefix: Path, exe: Path) -> List[str]:
    win = linux_to_win(prefix, exe)
    if " " in win:
        return [f"T9 Mod Manager is installed in a folder with spaces ({win}). Under Proton this stops "
                f"mods from loading ({SPACES_ISSUE}). Reinstall it into a folder without spaces, for "
                "example C:\\T9ModManager, with: t9mm_linux.py install <installer.exe>"]
    return []


def _home(args) -> Path:
    return Path(args.home).expanduser() if args.home else Path.home()


def pick_install(args, installs: List[Install], game: Optional[RunningGame]) -> Install:
    if args.prefix:
        prefix = Path(args.prefix).expanduser()
        if (prefix / "pfx" / "drive_c").is_dir() and (prefix / "pfx").resolve() != prefix.resolve():
            prefix = prefix / "pfx"
        match = next((i for i in installs if i.prefix.resolve() == prefix.resolve()), None)
        inst = match or Install("manual", f"prefix {prefix}", prefix, game_dir=game_in_prefix(prefix))
        if args.runner:
            rp = Path(args.runner).expanduser()
            if (rp / "proton").is_file():
                inst.runner = Runner("proton", rp, rp.name, "--runner")
                inst.compat_data = prefix.parent if prefix.name == "pfx" else prefix
            elif rp.is_file():
                inst.runner = Runner("wine", rp, "", "--runner")
            else:
                raise SystemExit(f"error: {rp} is neither a Proton folder nor a wine program.")
        if args.flatpak:
            inst.flatpak_app = args.flatpak
        return inst
    if game:
        inst = install_from_running(game, installs, _home(args))
        if inst and inst.runner:
            return inst
    chosen = [i for i in installs if not args.source or i.source == args.source]
    if not chosen:
        raise SystemExit(
            "error: Black Ops Cold War was not found.\n"
            "  - Steam: install the game, start it once and quit, so its Proton prefix exists.\n"
            "  - Lutris, Heroic or Bottles: install it through Battle.net in that prefix.\n"
            "  - Anything else: pass --prefix /path/to/prefix --runner /path/to/proton-folder-or-wine")
    if len(chosen) > 1:
        lines = "\n".join(f"  --source {i.source:<14} {i.label}" for i in chosen)
        raise SystemExit(f"error: the game is installed in more than one place; choose one:\n{lines}")
    return chosen[0]


def cmd_detect(args) -> int:
    home = _home(args)
    installs = detect_all(home, [Path(p).expanduser() for p in args.steam_root])
    game = find_running_game()
    if args.json:
        out = []
        for i in installs:
            j = i.to_json()
            j["t9_mod_manager"] = [str(p) for p in find_t9mm(i.prefix)]
            out.append(j)
        print(json.dumps({"version": __version__, "installs": out, "game_running": bool(game),
                          "game_pid": game.pid if game else None}, indent=2))
        return 0 if installs else 1
    print(f"T9 Mod Manager Linux helper {__version__}")
    if not installs:
        print("\nBlack Ops Cold War was not found in Steam, Lutris, Heroic or Bottles.")
        print("If it is installed somewhere else, use: run --prefix PREFIX --runner PROTON_FOLDER_OR_WINE")
        return 1
    for n, i in enumerate(installs, 1):
        print(f"\n[{n}] {i.label}   (--source {i.source})")
        print(f"    game folder    : {i.game_dir or 'not found'}")
        print(f"    prefix         : {i.prefix}{'' if (i.prefix / 'drive_c').is_dir() else '  (does not exist yet)'}")
        print(f"    runs with      : {i.runner.describe() if i.runner else 'unknown'}")
        if i.flatpak_app:
            print(f"    sandbox        : Flatpak {i.flatpak_app}")
        exes = find_t9mm(i.prefix) if (i.prefix / "drive_c").is_dir() else []
        if exes:
            print(f"    T9 Mod Manager : {linux_to_win(i.prefix, exes[0])}")
            i.notes.extend(check_launcher_path(i.prefix, exes[0]))
        else:
            print("    T9 Mod Manager : not installed here. Install it with: t9mm_linux.py install <installer.exe>")
        for note in i.notes:
            print(f"    NOTE: {note}")
    print(f"\nGame running: {'yes, pid ' + str(game.pid) if game else 'no'}")
    return 0


def _launch(args, program_for: Callable[[Install], Tuple[str, Optional[str]]], what: str,
            wait_for_game: bool) -> int:
    home = _home(args)
    installs = detect_all(home, [Path(p).expanduser() for p in args.steam_root])
    game = find_running_game()
    if wait_for_game and not game and not args.prefix and not args.dry_run:
        print("Waiting for Black Ops Cold War to start. Start it from Steam, Lutris or Heroic now.\n"
              "(T9 Mod Manager has to start after the game: Proton waits for every program in the\n"
              " prefix to close before it starts the game. Ctrl+C cancels; --no-wait starts it now.)",
              flush=True)
        try:
            while not game:
                time.sleep(2)
                game = find_running_game()
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 130
        print(f"Game found (pid {game.pid}).", flush=True)
        time.sleep(3)  # let the game's Wine session settle
    inst = pick_install(args, installs, game)
    if not (inst.prefix / "drive_c").is_dir():
        raise SystemExit(f"error: the prefix {inst.prefix} does not exist yet. Start Black Ops Cold War "
                         "once, quit it, and try again.")
    if not inst.runner:
        raise SystemExit("error: could not tell which Proton or Wine build the game uses.\n"
                         "Pass it with --prefix PREFIX --runner /path/to/proton-folder-or-wine")
    program, cwd = program_for(inst)
    cmd, extra_env = build_command(inst, program, args.args)
    print(f"{what} in {inst.label}")
    print(f"  prefix    : {inst.prefix}")
    print(f"  runs with : {inst.runner.describe()}")
    if inst.flatpak_app:
        print(f"  sandbox   : Flatpak {inst.flatpak_app}")
    if game and inst.label.startswith("the running game"):
        print("  joining the running game's Wine session")
    elif wait_for_game:
        print("  NOTE: the game is not running. Close T9 Mod Manager again before you start the game,\n"
              "  or the game will wait for it to exit.")
    if args.dry_run:
        prefix_env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(extra_env.items()))
        print("\n" + (prefix_env + " \\\n  " if prefix_env else "") + " ".join(shlex.quote(c) for c in cmd)
              + (f"\n  (working folder: {cwd})" if cwd else ""))
        return 0
    env = dict(os.environ)
    env.update(extra_env)
    sys.stdout.flush()  # keep our header above the program's own output
    try:
        return subprocess.call(cmd, env=env, cwd=cwd)
    except OSError as e:
        raise SystemExit(f"error: could not start {cmd[0]}: {e}")


def cmd_run(args) -> int:
    def program_for(inst: Install) -> Tuple[str, Optional[str]]:
        if args.exe:
            p = Path(args.exe).expanduser()
            if p.is_file():
                return str(p.resolve()), str(p.resolve().parent)
            mapped = win_to_linux(inst.prefix, args.exe)
            if mapped and mapped.is_file():
                return str(mapped), str(mapped.parent)
            return args.exe, None  # e.g. a program Wine knows by name, like winecfg
        exes = find_t9mm(inst.prefix)
        if not exes:
            raise SystemExit("error: T9 Mod Manager is not installed in this prefix. Install it with:\n"
                             "  t9mm_linux.py install /path/to/t9_mod_manager_installer.exe\n"
                             "or point at the program with --exe.")
        for warning in check_launcher_path(inst.prefix, exes[0]):
            print(f"WARNING: {warning}\n", file=sys.stderr)
        return str(exes[0]), str(exes[0].parent)
    return _launch(args, program_for, "Starting T9 Mod Manager", wait_for_game=not args.no_wait)


def cmd_install(args) -> int:
    setup = Path(args.installer).expanduser()
    if not setup.is_file():
        raise SystemExit(f"error: {setup} does not exist. Download the installer from {RELEASES}")
    print("When the installer asks where to install, choose a folder WITHOUT spaces,\n"
          f"for example C:\\T9ModManager. Folders with spaces break mod loading ({SPACES_ISSUE}).\n")
    rc = _launch(args, lambda inst: (str(setup.resolve()), str(setup.resolve().parent)),
                 "Running the installer", wait_for_game=False)
    if rc == 0 and not args.dry_run:
        for inst in detect_all(_home(args), [Path(p).expanduser() for p in args.steam_root]):
            for exe in find_t9mm(inst.prefix)[:1]:
                print(f"\nInstalled: {linux_to_win(inst.prefix, exe)}")
                for w in check_launcher_path(inst.prefix, exe):
                    print(f"WARNING: {w}", file=sys.stderr)
    return rc


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="t9mm_linux.py", description=(__doc__ or "").split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--steam-root", action="append", default=[], metavar="DIR",
                        help="an extra Steam folder to search (repeatable)")
    common.add_argument("--home", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("detect", parents=[common], help="list Black Ops Cold War installs and how they run")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    launch = argparse.ArgumentParser(add_help=False, parents=[common])
    launch.add_argument("--source", choices=["steam", "steam-flatpak", "steam-snap", "lutris", "heroic", "bottles"],
                        help="which install to use when the game is found in more than one place")
    launch.add_argument("--prefix", help="use this Wine prefix instead of detecting one")
    launch.add_argument("--runner", help="with --prefix: the Proton folder or wine program the game runs with")
    launch.add_argument("--flatpak", metavar="APP_ID", help="with --prefix: run inside this Flatpak app's sandbox")
    launch.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    r = sub.add_parser("run", parents=[launch], help="wait for the game, then start T9 Mod Manager in its prefix")
    r.add_argument("--exe", help="the program to start, if T9 Mod Manager cannot be found automatically")
    r.add_argument("--no-wait", action="store_true", help="start now, even if the game is not running")
    r.add_argument("args", nargs="*", help="arguments for the program (after --)")
    i = sub.add_parser("install", parents=[launch], help="run the T9 Mod Manager installer in the game's prefix")
    i.add_argument("installer", help="path to t9_mod_manager_installer.exe")
    i.add_argument("args", nargs="*", help="arguments for the installer (after --)")
    args = ap.parse_args(argv)
    if args.cmd == "detect":
        return cmd_detect(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "install":
        return cmd_install(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
