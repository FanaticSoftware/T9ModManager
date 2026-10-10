# T9 Mod Manager on Linux (Proton / Wine)

> **Community-supported.** T9 Mod Manager is a Windows program. This helper only
> starts it in the right place; it does not change the launcher, the game,
> Steam, Lutris or any of their settings.

> **Read the launcher's Terms of Service first.** It works by writing a script
> into the running game's memory, and using it may get your Call of Duty,
> Battle.net or Steam account banned. Running it on Linux does not change that.

T9 Mod Manager works on the **running** game, so on Linux it has to run:

- in the **same Wine prefix** as Black Ops Cold War,
- with the **same Proton/Wine build**, and
- inside the **same sandbox** (Steam Linux Runtime, the Lutris or Bottles Flatpak).

If any of these differ, Wine starts a second `wineserver` and the launcher simply
cannot see the game. `t9mm_linux.py` works out that combination for you.

| Where the game is installed | Supported |
| --- | --- |
| Steam: native, Flatpak (`com.valvesoftware.Steam`), Snap | ✓ |
| Battle.net through Lutris: native or Flatpak (`net.lutris.Lutris`), including umu | ✓ |
| Battle.net through Heroic (sideloaded) | ✓ |
| Battle.net through Bottles: native or Flatpak | ✓ |

Requirements: Python 3.8 or newer (standard library only). Flatpak installs also need `flatpak` on the host.

## 1. Check what the helper finds

```sh
python3 t9mm_linux.py detect
```

This lists every Black Ops Cold War install, its prefix and the Proton/Wine build
it runs with. Nothing is started. Add `--json` for machine-readable output.

## 2. Install T9 Mod Manager into the game's prefix

Download `t9_mod_manager_installer.exe` from the [Releases page](https://github.com/FanaticSoftware/T9ModManager/releases), then:

```sh
python3 t9mm_linux.py install ~/Downloads/t9_mod_manager_installer.exe
```

**Pick an install folder without spaces** (for example `C:\T9ModManager`).
Under Proton the installer suggests
`C:\users\steamuser\AppData\Local\Programs\T9 Mod Manager`, so change it.
The launcher currently fails to start the game from a path with spaces
([#1](https://github.com/FanaticSoftware/T9ModManager/issues/1)), and the helper warns you if it finds one.

## 3. Play

1. Start Black Ops Cold War normally from Steam, Lutris, Heroic or Bottles.
2. Run:

   ```sh
   python3 t9mm_linux.py run
   ```

The helper waits for the game, reads the environment of the **actual game
process** (prefix, Proton build, sync settings, sandbox), and starts T9 Mod
Manager next to it with Proton's `runinprefix` verb, so it joins the game's
wineserver instead of starting a new one.

## Options worth knowing

| Option | What it does |
| --- | --- |
| `--dry-run` | Print the exact command instead of running it. |
| `--source steam\|lutris\|…` | Choose an install when the game is in more than one place. |
| `--prefix DIR --runner DIR` | Skip detection: use this prefix and this Proton folder or `wine` program. |
| `--flatpak APP_ID` | With `--prefix`: run inside that Flatpak's sandbox. |
| `--exe 'C:\path\T9ModManager.exe'` | Use this program if T9 Mod Manager is not found automatically. |
| `--no-wait` | Start now, even if the game is not running yet. |
| `--steam-root DIR` | An extra Steam folder to search (repeatable). |

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "Black Ops Cold War was not found" | Run `detect`. For an unusual Steam location use `--steam-root`; otherwise pass `--prefix` and `--runner`. |
| The prefix "does not exist yet" | Start the game once so Proton creates it. |
| The launcher opens but sees no game | It is in a different prefix or sandbox. Start it with `run` *while the game is running*, not with `--no-wait`. |
| Flatpak Steam | Install [Protontricks](https://github.com/Matoking/protontricks) or use `flatpak run --command=…` as printed by `--dry-run`. |

## Tests

```sh
python3 -m unittest discover -s tools/linux/tests -v
```

The tests build fake Steam, Lutris, Heroic and Bottles setups in a temporary
folder, so they run on any machine and never touch your real installs.
Installing and starting v1.0.4 was tested on Fedora 44 with the game's
Battle.net prefix under the Lutris Flatpak (umu, GE-Proton11-7).
The join-the-running-game logic was also checked against real Proton
Experimental + Steam Linux Runtime 4 and the Lutris Flatpak with umu, using a
stand-in Windows program in a throwaway prefix in place of the game.
