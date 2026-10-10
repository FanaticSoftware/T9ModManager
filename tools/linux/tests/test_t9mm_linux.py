"""Tests for tools/linux/t9mm_linux.py.

Every test builds a throwaway fake home (Steam, Lutris, Heroic, Bottles,
Wine prefixes, /proc) in a temporary folder, so they run anywhere.
Run: python3 -m unittest discover -s tools/linux/tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "t9mm_linux.py"
spec = importlib.util.spec_from_file_location("t9mm_linux", SCRIPT)
assert spec is not None and spec.loader is not None
t9 = importlib.util.module_from_spec(spec)
sys.modules["t9mm_linux"] = t9
spec.loader.exec_module(t9)


def write(path: Path, text: str = "", mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def make_proton(folder: Path, version: str = "") -> Path:
    write(folder / "proton", "#!/bin/sh\n", 0o755)
    write(folder / "files" / "bin" / "wine", "#!/bin/sh\n", 0o755)
    if version:
        write(folder / "version", version + "\n")
    return folder


def make_prefix(prefix: Path, user: str = "steamuser") -> Path:
    (prefix / "drive_c" / "windows" / "system32").mkdir(parents=True, exist_ok=True)
    (prefix / "drive_c" / "users" / user).mkdir(parents=True, exist_ok=True)
    (prefix / "dosdevices").mkdir(parents=True, exist_ok=True)
    os.symlink("../drive_c", prefix / "dosdevices" / "c:")
    os.symlink("/", prefix / "dosdevices" / "z:")
    write(prefix / "system.reg", "WINE REGISTRY Version 2\n")
    write(prefix / "user.reg", "WINE REGISTRY Version 2\n")
    return prefix


def install_game_battlenet(prefix: Path, folder: str = "Call of Duty Black Ops Cold War") -> Path:
    game = prefix / "drive_c" / "Program Files (x86)" / folder
    write(game / "BlackOpsColdWar.exe")
    write(prefix / "system.reg", "WINE REGISTRY Version 2\n\n"
          "[Software\\\\Wow6432Node\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\"
          "Call of Duty Black Ops Cold War] 1700000000\n"
          "\"DisplayName\"=\"Call of Duty: Black Ops Cold War\"\n"
          f"\"InstallLocation\"=\"C:\\\\Program Files (x86)\\\\{folder}\"\n")
    return game


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self):
        self._tmp.cleanup()


class FormatTests(unittest.TestCase):
    def test_vdf_nested_escapes_comments_case(self):
        vdf = t9.parse_vdf('// comment\n"LibraryFolders"\n{\n "0"\n {\n  "path" "/a b/\\"q\\"\\\\x"\n'
                           '  "apps" { "1985810" "123" }\n }\n}\n')
        self.assertEqual(t9.dig(vdf, "libraryfolders", "0", "path"), '/a b/"q"\\x')
        self.assertEqual(t9.dig(vdf, "LIBRARYFOLDERS", "0", "apps", "1985810"), "123")
        self.assertIsNone(t9.dig(vdf, "libraryfolders", "missing", "path"))

    def test_wine_registry(self):
        reg = t9.parse_wine_reg(
            "WINE REGISTRY Version 2\n;; comment\n\n"
            "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\{ABC}] 1700000000\n"
            "#time=1d9\n"
            "\"DisplayName\"=\"T9 Mod Manager 1.0.4\"\n"
            "\"DisplayIcon\"=\"C:\\\\Program Files\\\\T9\\\\T9.exe,0\"\n"
            "\"Quoted\"=\"say \\\"hi\\\"\"\n"
            "\"Accent\"=\"caf\\x00e9\"\n"
            "\"Expand\"=str(2):\"%SystemRoot%\\\\x\"\n"
            "\"Number\"=dword:00000001\n")
        key = "software\\microsoft\\windows\\currentversion\\uninstall\\{abc}"
        self.assertIn(key, reg)
        v = reg[key]
        self.assertEqual(v["displayname"], "T9 Mod Manager 1.0.4")
        self.assertEqual(v["displayicon"], "C:\\Program Files\\T9\\T9.exe,0")
        self.assertEqual(v["quoted"], 'say "hi"')
        self.assertEqual(v["accent"], "caf\u00e9")
        self.assertEqual(v["expand"], "%SystemRoot%\\x")
        self.assertNotIn("number", v)

    def test_simple_yaml(self):
        y = t9.read_simple_yaml("game:\n  exe: drive_c/x y.exe\n  prefix: '/home/u/Games/bn'\n"
                                "name: 'Call of Duty: Black Ops Cold War'\nsystem:\n  env:\n    PROTON_NO_FSYNC: '1'\n"
                                "wine:\n  version: ge-proton\n  fsync: false\n")
        self.assertEqual(y["game"]["prefix"], "/home/u/Games/bn")
        self.assertEqual(y["game"]["exe"], "drive_c/x y.exe")
        self.assertEqual(y[""]["name"], "Call of Duty: Black Ops Cold War")
        self.assertEqual(y["system"]["PROTON_NO_FSYNC"], "1")
        self.assertEqual(y["wine"]["version"], "ge-proton")
        self.assertEqual(y["wine"]["fsync"], "false")

    def test_exe_from_command(self):
        self.assertEqual(t9.exe_from_command('"C:\\a b\\Uninstall T9.exe" /currentuser'), "C:\\a b\\Uninstall T9.exe")
        self.assertEqual(t9.exe_from_command("C:\\x\\unins000.exe /SILENT"), "C:\\x\\unins000.exe")


class PathTests(Case):
    def test_case_insensitive_mapping(self):
        prefix = make_prefix(self.home / "pfx")
        target = write(prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Programs" / "T9" / "T9.exe")
        mapped = t9.win_to_linux(prefix, "c:\\USERS\\SteamUser\\appdata\\local\\programs\\t9\\t9.EXE")
        self.assertEqual(mapped, target)
        self.assertEqual(t9.linux_to_win(prefix, target), "C:\\users\\steamuser\\AppData\\Local\\Programs\\T9\\T9.exe")

    def test_other_drive_letters(self):
        prefix = make_prefix(self.home / "pfx")
        other = self.home / "games"
        write(other / "Game" / "a.exe")
        os.symlink(other, prefix / "dosdevices" / "d:")
        self.assertEqual(t9.win_to_linux(prefix, "D:\\game\\A.exe"), other / "Game" / "a.exe")
        self.assertEqual(t9.win_to_linux(prefix, "Z:" + str(other / "Game" / "a.exe").replace("/", "\\")),
                         other / "Game" / "a.exe")
        self.assertIsNone(t9.win_to_linux(prefix, "Q:\\nothing"))
        self.assertIsNone(t9.win_to_linux(prefix, "not a path"))
        self.assertTrue(t9.linux_to_win(prefix, other / "Game" / "a.exe").startswith("Z:\\"))


class SteamTests(Case):
    def steam(self):
        root = self.home / ".local" / "share" / "Steam"
        (root / "steamapps").mkdir(parents=True)
        (self.home / ".steam").mkdir()
        os.symlink(root, self.home / ".steam" / "root")  # the usual duplicate path
        return root

    def library_with_game(self, lib: Path) -> Path:
        write(lib / "steamapps" / f"appmanifest_{t9.APP_ID}.acf",
              '"AppState"\n{\n "appid" "1985810"\n "installdir" "Call of Duty Black Ops Cold War"\n}\n')
        (lib / "steamapps" / "common" / "Call of Duty Black Ops Cold War").mkdir(parents=True)
        compat = lib / "steamapps" / "compatdata" / t9.APP_ID
        make_prefix(compat / "pfx")
        return compat

    def test_second_library_and_recorded_proton(self):
        root = self.steam()
        lib2 = self.home / "mnt" / "Steam Library"
        write(root / "steamapps" / "libraryfolders.vdf",
              f'"libraryfolders"\n{{\n "0" {{ "path" "{root}" }}\n "1" {{ "path" "{lib2}" }}\n}}\n')
        compat = self.library_with_game(lib2)
        proton = make_proton(root / "compatibilitytools.d" / "GE-Proton Latest")
        write(compat / "config_info", f"GE-Proton11-7\n{proton}/files/share/fonts/\n{proton}/files/lib/\n")
        found = t9.detect_steam(self.home)
        self.assertEqual(len(found), 1, "the ~/.steam/root symlink must not produce a duplicate")
        i = found[0]
        self.assertEqual(i.source, "steam")
        self.assertEqual(i.prefix, compat / "pfx")
        self.assertEqual(i.game_dir, lib2 / "steamapps" / "common" / "Call of Duty Black Ops Cold War")
        self.assertEqual((i.runner.kind, i.runner.path, i.runner.version), ("proton", proton, "GE-Proton11-7"))
        self.assertIsNone(i.flatpak_app)
        self.assertEqual(i.notes, [])

    def test_runner_from_steam_settings(self):
        root = self.steam()
        compat = self.library_with_game(root)
        tool = make_proton(root / "compatibilitytools.d" / "proton-cachyos")
        write(tool / "compatibilitytool.vdf", '"compatibilitytools" { "compat_tools" { "proton-cachyos-11" '
                                              '{ "install_path" "." "display_name" "CachyOS" } } }')
        cfg = ('"InstallConfigStore" { "Software" { "Valve" { "Steam" { "CompatToolMapping" { '
               '"1985810" { "name" "proton-cachyos-11" } } } } } }')
        write(root / "config" / "config.vdf", cfg)
        i = t9.detect_steam(self.home)[0]
        self.assertEqual(i.runner.path, tool.resolve())
        self.assertEqual(i.runner.source, "Steam compatibility setting for the game")
        # Valve's own builds are referred to by internal name.
        experimental = make_proton(root / "steamapps" / "common" / "Proton - Experimental")
        write(root / "config" / "config.vdf", cfg.replace("proton-cachyos-11", "proton_experimental"))
        self.assertEqual(t9.detect_steam(self.home)[0].runner.path, experimental)
        # Nothing recorded and nothing configured: guess, and say so.
        write(root / "config" / "config.vdf", "")
        i = t9.detect_steam(self.home)[0]
        self.assertTrue(i.runner.source.startswith("guess"))
        self.assertTrue(any("guessed" in n for n in i.notes))
        self.assertIsNotNone(compat)

    def test_prefix_missing_and_flatpak(self):
        root = self.home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
        write(root / "steamapps" / f"appmanifest_{t9.APP_ID}.acf", '"AppState" { "installdir" "BOCW" }')
        i = t9.detect_steam(self.home)[0]
        self.assertEqual(i.source, "steam-flatpak")
        self.assertEqual(i.flatpak_app, "com.valvesoftware.Steam")
        self.assertTrue(any("does not exist yet" in n for n in i.notes))
        self.assertTrue(any("Protontricks" in n for n in i.notes))


class LauncherTests(Case):
    """Lutris, Heroic and Bottles, which install the game through Battle.net."""

    def lutris(self, flatpak=True):
        base = self.home / (".var/app/net.lutris.Lutris/data/lutris" if flatpak else ".local/share/lutris")
        prefix = make_prefix(self.home / "Games" / "battlenet", user="nic")
        os.symlink(".", prefix / "pfx")  # umu-managed prefixes carry this
        install_game_battlenet(prefix)
        write(base / "games" / "battlenet-standard-1.yml",
              f"game:\n  exe: drive_c/Program Files (x86)/Battle.net/Battle.net Launcher.exe\n  prefix: {prefix}\n"
              "name: Battle.net\ngame_slug: battlenet\nwine:\n  battleye: false\n")
        write(base / "runners" / "wine.yml", "wine:\n  version: proton-cachyos-slr\n")
        make_proton(base / "runners" / "wine" / "proton-cachyos-slr")
        write(base / "runtime" / "umu" / "umu-run", "#!/usr/bin/env python3\n", 0o755)
        return base, prefix

    def test_lutris_prefers_the_game_entry_and_umu(self):
        base, prefix = self.lutris()
        # Only the Battle.net entry: falls back to Lutris' default runner, a Proton build run through umu.
        i = t9.detect_lutris(self.home)
        self.assertEqual(len(i), 1)
        self.assertEqual(i[0].label, "Lutris: Battle.net")
        self.assertEqual((i[0].runner.kind, i[0].runner.protonpath),
                         ("umu", str(base / "runners" / "wine" / "proton-cachyos-slr")))
        self.assertIn("runners/wine.yml", i[0].runner.source)
        # The game's own entry, sharing the prefix, wins and brings its settings.
        write(base / "games" / "call-of-duty-black-ops-cold-war-2.yml",
              f"game:\n  args: --exec=\"launch ZEUS\"\n  prefix: {prefix}\n"
              "name: 'Call of Duty: Black Ops Cold War'\nsystem:\n  env:\n    DXVK_HUD: compiler\n"
              "    PROTON_NO_NTSYNC: '1'\nwine:\n  version: ge-proton\n  fsync: false\n")
        i = t9.detect_lutris(self.home)
        self.assertEqual(len(i), 1)
        inst = i[0]
        self.assertEqual(inst.label, "Lutris: Call of Duty: Black Ops Cold War")
        self.assertEqual((inst.runner.kind, inst.runner.protonpath), ("umu", "GE-Proton"))
        self.assertEqual(inst.runner.path, base / "runtime" / "umu" / "umu-run")
        self.assertEqual(inst.flatpak_app, "net.lutris.Lutris")
        self.assertEqual(inst.game_dir, prefix / "drive_c" / "Program Files (x86)" / "Call of Duty Black Ops Cold War")
        self.assertEqual(inst.env.get("PROTON_NO_FSYNC"), "1")
        self.assertEqual(inst.env.get("PROTON_NO_NTSYNC"), "1")
        self.assertEqual(inst.env.get("GAMEID"), t9.UMU_DEFAULT_GAMEID)
        self.assertNotIn("DXVK_HUD", inst.env)
        cmd, env = t9.build_command(inst, "/x/T9.exe")
        self.assertEqual(cmd[:3], ["flatpak", "run", f"--command={base / 'runtime' / 'umu' / 'umu-run'}"])
        self.assertIn("--env=PROTON_VERB=runinprefix", cmd)
        self.assertIn(f"--env=WINEPREFIX={prefix}", cmd)
        self.assertIn("--env=PROTONPATH=GE-Proton", cmd)
        self.assertEqual(cmd[-2:], ["net.lutris.Lutris", "/x/T9.exe"])
        self.assertEqual(env, {})

    def test_lutris_native_plain_wine_and_names_from_db(self):
        base, prefix = self.lutris(flatpak=False)
        wine = write(base / "runners" / "wine" / "wine-ge-8-26" / "bin" / "wine", "", 0o755)
        write(base / "games" / "battlenet-standard-1.yml",
              f"game:\n  prefix: {prefix}\nwine:\n  version: wine-ge-8-26\n")
        con = sqlite3.connect(str(base / "pga.db"))
        con.execute("CREATE TABLE games (name TEXT, configpath TEXT)")
        con.execute("INSERT INTO games VALUES ('My Battle.net', 'battlenet-standard-1')")
        con.commit()
        con.close()
        inst = t9.detect_lutris(self.home)[0]
        self.assertEqual(inst.label, "Lutris: My Battle.net")
        self.assertEqual((inst.runner.kind, inst.runner.path), ("wine", wine))
        self.assertIsNone(inst.flatpak_app)
        cmd, env = t9.build_command(inst, "C:\\T9\\T9.exe", ["--flag"])
        self.assertEqual(cmd, [str(wine), "C:\\T9\\T9.exe", "--flag"])
        self.assertEqual(env["WINEPREFIX"], str(prefix))

    def test_lutris_ignores_prefixes_without_the_game(self):
        base = self.home / ".local/share/lutris"
        other = make_prefix(self.home / "Games" / "other")
        write(base / "games" / "other-1.yml", f"game:\n  prefix: {other}\nwine:\n  version: x\n")
        self.assertEqual(t9.detect_lutris(self.home), [])

    def test_heroic_proton_and_bottles(self):
        hero = self.home / ".config" / "heroic"
        base = self.home / "Games" / "Heroic" / "Prefixes" / "bnet"
        make_prefix(base / "pfx")
        install_game_battlenet(base / "pfx")
        proton = make_proton(self.home / "protons" / "GE-Proton9-1")
        write(hero / "GamesConfig" / "abc.json", json.dumps({"abc": {
            "winePrefix": str(base), "wineVersion": {"bin": str(proton / "proton"), "name": "GE-Proton9-1",
                                                     "type": "proton"}}}))
        write(hero / "sideload_apps" / "library.json", json.dumps({"games": [{"app_name": "abc", "title": "Battle.net"}]}))
        i = t9.detect_heroic(self.home)[0]
        self.assertEqual((i.label, i.prefix, i.compat_data), ("Heroic: Battle.net", base / "pfx", base))
        self.assertEqual((i.runner.kind, i.runner.path), ("proton", proton))
        cmd, env = t9.build_command(i, "T9.exe")
        self.assertEqual(cmd, [str(proton / "proton"), "runinprefix", "T9.exe"])
        self.assertEqual(env["STEAM_COMPAT_DATA_PATH"], str(base))

        bottles = self.home / ".var/app/com.usebottles.bottles/data/bottles"
        bottle = make_prefix(bottles / "bottles" / "BNet")
        install_game_battlenet(bottle)
        write(bottle / "bottle.yml", "Name: BNet\nRunner: soda-9.0\n")
        wine = write(bottles / "runners" / "soda-9.0" / "bin" / "wine", "", 0o755)
        b = t9.detect_bottles(self.home)[0]
        self.assertEqual((b.label, b.runner.path, b.flatpak_app), ("Bottles: BNet", wine, "com.usebottles.bottles"))


class T9mmTests(Case):
    def test_registry_lookup_and_spaces_warning(self):
        prefix = make_prefix(self.home / "pfx")
        folder = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "Programs" / "T9 Mod Manager"
        exe = write(folder / "T9 Mod Manager.exe")
        write(folder / "Uninstall T9 Mod Manager.exe")
        write(prefix / "user.reg", "WINE REGISTRY Version 2\n\n"
              "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\t9-mod-manager] 1\n"
              "\"DisplayName\"=\"T9 Mod Manager 1.0.4\"\n"
              "\"DisplayIcon\"=\"C:\\\\users\\\\STEAMUSER\\\\AppData\\\\Local\\\\Programs\\\\T9 Mod Manager\\\\T9 Mod Manager.exe,0\"\n"
              "\"UninstallString\"=\"\\\"C:\\\\users\\\\steamuser\\\\AppData\\\\Local\\\\Programs\\\\T9 Mod Manager\\\\Uninstall T9 Mod Manager.exe\\\" /currentuser\"\n")
        self.assertEqual(t9.find_t9mm(prefix), [exe])
        warning = t9.check_launcher_path(prefix, exe)
        self.assertEqual(len(warning), 1)
        self.assertIn("issues/1", warning[0])
        good = write(prefix / "drive_c" / "T9ModManager" / "T9ModManager.exe")
        self.assertEqual(t9.check_launcher_path(prefix, good), [])

    def test_scan_fallback_skips_uninstallers(self):
        prefix = make_prefix(self.home / "pfx")
        folder = prefix / "drive_c" / "T9ModManager"
        write(folder / "unins000.exe")
        write(folder / "Updater.exe")
        exe = write(folder / "T9ModManager.exe")
        write(folder / "helper.exe")
        self.assertEqual(t9.find_t9mm(prefix)[0], exe)
        self.assertNotIn(folder / "unins000.exe", t9.find_t9mm(prefix))


class RunningGameTests(Case):
    def proc(self, pid: int, argv, env) -> None:
        d = self.home / "proc" / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")

    def test_lutris_flatpak_game_is_joined_with_its_own_settings(self):
        umu = write(self.home / ".var/app/net.lutris.Lutris/data/lutris/runtime/umu/umu-run", "", 0o755)
        game_env = {"WINEPREFIX": "/home/u/Games/battlenet", "PROTONPATH": "/x/GE-Proton11-5", "GAMEID": "umu-default",
                    "FLATPAK_ID": "net.lutris.Lutris", "PROTON_NO_FSYNC": "1", "WINEESYNC": "1",
                    "PROTON_VERB": "waitforexitandrun", "LD_PRELOAD": "/bad.so", "PATH": "/usr/lib/pressure-vessel"}
        self.proc(10, ["python3", "/proton", "waitforexitandrun", "C:\\Games\\BlackOpsColdWar.exe"], {})  # a wrapper
        self.proc(42, ["C:\\Program Files (x86)\\Call of Duty Black Ops Cold War\\BlackOpsColdWar.exe"], game_env)
        game = t9.find_running_game(self.home / "proc")
        self.assertEqual(game.pid, 42)
        inst = t9.install_from_running(game, [], self.home)
        self.assertEqual((inst.runner.kind, inst.runner.path, inst.runner.protonpath), ("umu", umu, "/x/GE-Proton11-5"))
        self.assertEqual(inst.flatpak_app, "net.lutris.Lutris")
        self.assertEqual(inst.prefix, Path("/home/u/Games/battlenet"))
        for kept in ("PROTON_NO_FSYNC", "WINEESYNC", "GAMEID", "PROTONPATH"):
            self.assertIn(kept, inst.env)
        for dropped in ("LD_PRELOAD", "PATH", "PROTON_VERB", "FLATPAK_ID"):
            self.assertNotIn(dropped, inst.env)
        cmd, _ = t9.build_command(inst, "T9.exe")
        self.assertIn("--env=PROTON_VERB=runinprefix", cmd)
        self.assertIn("--env=PROTON_NO_FSYNC=1", cmd)

    def test_steam_game_uses_the_tool_steam_started_it_with(self):
        proton = make_proton(self.home / "Proton - Experimental", "1790839227 experimental-11.0")
        compat = self.home / "compatdata" / "1985810"
        self.proc(7, ["Z:\\games\\BlackOpsColdWar.exe", "-arg"], {
            "STEAM_COMPAT_DATA_PATH": str(compat), "STEAM_COMPAT_CLIENT_INSTALL_PATH": "/home/u/.local/share/Steam",
            "STEAM_COMPAT_TOOL_PATHS": f"{proton}:/runtime/SteamLinuxRuntime_4", "SteamGameId": "1985810",
            "STEAM_COMPAT_CONFIG": "nofsync"})
        inst = t9.install_from_running(t9.find_running_game(self.home / "proc"), [], self.home)
        self.assertEqual((inst.runner.kind, inst.runner.path, inst.runner.version), ("proton", proton, "experimental-11.0"))
        self.assertEqual((inst.prefix, inst.compat_data), (compat / "pfx", compat))
        cmd, env = t9.build_command(inst, "T9.exe")
        self.assertEqual(cmd, [str(proton / "proton"), "runinprefix", "T9.exe"])
        self.assertEqual(env["STEAM_COMPAT_CONFIG"], "nofsync")
        self.assertEqual(env["SteamGameId"], "1985810")

    def test_no_game(self):
        self.proc(5, ["/usr/bin/bash"], {})
        self.assertIsNone(t9.find_running_game(self.home / "proc"))
        self.assertIsNone(t9.find_running_game(self.home / "no-proc"))


class CliTests(Case):
    def cli(self, *args):
        env = dict(os.environ, HOME=str(self.home))
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--home", str(self.home)],
                              capture_output=True, text=True, env=env)

    def test_detect_json_nothing_found(self):
        r = self.cli("detect", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["installs"], [])

    def test_run_dry_run_manual_prefix(self):
        compat = self.home / "compat"
        prefix = make_prefix(compat / "pfx")
        exe = write(prefix / "drive_c" / "T9ModManager" / "T9ModManager.exe")
        proton = make_proton(self.home / "Proton 11.0")
        r = self.cli("run", "--dry-run", "--prefix", str(compat), "--runner", str(proton))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("runinprefix", r.stdout)
        self.assertIn(f"STEAM_COMPAT_DATA_PATH={compat}", r.stdout)
        self.assertIn(str(exe), r.stdout)

    def test_run_errors_are_actionable(self):
        r = self.cli("run", "--dry-run", "--no-wait")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("was not found", r.stderr)
        r = self.cli("install", str(self.home / "missing.exe"))
        self.assertIn("releases", r.stderr)
        r = self.cli("run", "--dry-run", "--prefix", str(self.home), "--runner", str(self.home / "nope"))
        self.assertIn("neither a Proton folder nor a wine program", r.stderr)


if __name__ == "__main__":
    unittest.main()
