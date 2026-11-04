#!/usr/bin/env python3
"""Build the Sumie plugin and install it, with BepInEx, into Draw & Guess.

    python tools/install.py                  # auto-detect the game
    python tools/install.py --game PATH      # folder that holds the game app/exe
    python tools/install.py --check          # only report what it would do
    python tools/install.py --uninstall      # remove the plugin (keeps BepInEx)

Works on macOS (Intel and Apple Silicon, the latter through Rosetta 2), Windows (x64) and
Linux (x86_64, experimental: not yet verified on a real machine). Needs a .NET SDK (6 or newer) on PATH and the
Python environment from `python tools/setup.py`.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
PLUGIN_NAME = "DrawGuessBridge"
CONFIG_NAME = "com.clarke.drawguess.bridge.cfg"
GAME_NAME = "Draw & Guess"

BEPINEX_BUILD = "790"
BEPINEX_COMMIT = "4e98359"
BEPINEX_URL = ("https://builds.bepinex.dev/projects/bepinex_be/{build}/"
               "BepInEx-Unity.Mono-{platform}-6.0.0-be.{build}%2B{commit}.zip")
BEPINEX = {
    "win": ("win-x64", "5f67c366302521e36785dc5e0fcdc088c1714384907288dc49ad7787a90a6d5a"),
    "mac": ("macos-x64", "222d19879825dd4d81b18e95e3774e95b7b0ed3bd3530ed30b6f7c511a7b3e11"),
    "linux": ("linux-x64", "e4c7f318e64b0db2642ac49efb4e082c827ce757934d92692eda0933117ad030"),
}

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
PLATFORM = "win" if IS_WINDOWS else "mac" if IS_MAC else "linux" if IS_LINUX else None


class InstallError(SystemExit):
    pass


def fail(message: str) -> "InstallError":
    return InstallError("\nERROR: " + message)


def step(text: str) -> None:
    print(f"==> {text}", flush=True)


# --------------------------------------------------------------------------
# finding the game
# --------------------------------------------------------------------------

def steam_libraries() -> list[Path]:
    """Every Steam library folder we can find on this machine."""
    roots: list[Path] = []
    if IS_WINDOWS:
        for key in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(key):
                roots.append(Path(os.environ[key]) / "Steam")
        try:
            import winreg
            for hive, sub, name in (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, sub) as handle:
                        roots.append(Path(winreg.QueryValueEx(handle, name)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
    else:
        roots.append(Path.home() / "Library" / "Application Support" / "Steam")
        roots.append(Path.home() / ".steam" / "steam")
        roots.append(Path.home() / ".local" / "share" / "Steam")
        roots.append(Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam")

    libraries = list(roots)
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                libraries.append(Path(match.group(1).replace("\\\\", "\\")))
    seen, unique = set(), []
    for library in libraries:
        if library not in seen:
            seen.add(library)
            unique.append(library)
    return unique


def game_layout(folder: Path):
    """Return (launcher, managed_dir) if `folder` holds the game, else None.

    macOS:   Draw&Guess.app/Contents/Resources/Data/Managed
    Windows: <name>.exe next to <name>_Data/Managed
    Linux:   <name>.x86_64 next to <name>_Data/Managed
    """
    if not folder.is_dir():
        return None
    if IS_WINDOWS:
        for exe in sorted(folder.glob("*.exe")):
            data = folder / (exe.stem + "_Data")
            if (data / "Managed").is_dir() or (data / "il2cpp_data").is_dir():
                return exe, data / "Managed"
    else:
        for app in sorted(folder.glob("*.app")):
            managed = app / "Contents" / "Resources" / "Data" / "Managed"
            if managed.is_dir():
                return app, managed
        if IS_LINUX:
            for exe in sorted(folder.glob("*.x86_64")):
                data = folder / (exe.stem + "_Data")
                if (data / "Managed").is_dir():
                    return exe, data / "Managed"
    return None


def find_game(explicit: str | None):
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        env = os.environ.get("DG_GAME_DIR")
        candidates = [Path(env).expanduser()] if env else []
        for library in steam_libraries():
            candidates.append(library / "steamapps" / "common" / GAME_NAME)
    for candidate in candidates:
        # the game may sit directly in the folder or in a branch subfolder (e.g. "beta")
        for folder in [candidate] + (sorted(p for p in candidate.iterdir() if p.is_dir())
                                     if candidate.is_dir() else []):
            layout = game_layout(folder)
            if layout:
                return folder, layout[0], layout[1]
    where = ", ".join(str(c) for c in candidates) or "the usual Steam folders"
    raise fail(f"could not find {GAME_NAME} (looked in {where}).\n"
               "Pass the folder that contains the game with  --game \"<folder>\".")


def check_mono(managed: Path) -> None:
    if (managed / "Assembly-CSharp.dll").is_file():
        return
    if (managed.parent / "il2cpp_data").is_dir() or (managed.parent.parent / "GameAssembly.dll").exists():
        raise fail("this build of the game uses IL2CPP. The Mono build of BepInEx cannot load "
                   "the plugin there, so Sumie cannot be installed on it.")
    raise fail(f"Assembly-CSharp.dll not found in {managed}. Is this the right game folder?")


# --------------------------------------------------------------------------
# tools: .NET SDK and BepInEx
# --------------------------------------------------------------------------

def find_dotnet() -> str:
    candidates = [shutil.which("dotnet")]
    if os.environ.get("DOTNET_ROOT"):
        candidates.append(str(Path(os.environ["DOTNET_ROOT"]) / ("dotnet.exe" if IS_WINDOWS else "dotnet")))
    candidates.append(str(Path.home() / ".dotnet" / ("dotnet.exe" if IS_WINDOWS else "dotnet")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            try:
                out = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            match = re.match(r"(\d+)\.", out.stdout.strip())
            if out.returncode == 0 and match and int(match.group(1)) >= 6:
                return candidate
    raise fail("a .NET SDK (version 6 or newer) is required to build the plugin.\n"
               "Install it from https://dotnet.microsoft.com/download and open a new terminal.")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_bepinex() -> Path:
    """Download and unpack the pinned BepInEx 6 build; return the unpacked folder."""
    label, expected = BEPINEX[PLATFORM]
    target = CACHE / "bepinex" / label
    marker = target / ".verified"
    if marker.is_file() and (target / "BepInEx" / "core").is_dir():
        return target
    CACHE.joinpath("bepinex").mkdir(parents=True, exist_ok=True)
    archive = CACHE / "bepinex" / f"{label}.zip"
    if not archive.is_file() or sha256_of(archive) != expected:
        url = BEPINEX_URL.format(build=BEPINEX_BUILD, platform=label, commit=BEPINEX_COMMIT)
        step(f"downloading BepInEx 6 build {BEPINEX_BUILD} ({label})")
        request = urllib.request.Request(url, headers={"User-Agent": "sumie-install"})
        with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out)
    if sha256_of(archive) != expected:
        archive.unlink(missing_ok=True)
        raise fail("the BepInEx download does not match the pinned checksum and was discarded.")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    marker.write_text(expected)
    return target


def build_plugin(dotnet: str, bepinex: Path, managed: Path) -> Path:
    step("building the plugin")
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    project = ROOT / "src" / PLUGIN_NAME / f"{PLUGIN_NAME}.csproj"
    command = [dotnet, "build", str(project), "-c", "Release",
               f"-p:BepInExDir={bepinex / 'BepInEx' / 'core'}", f"-p:GameManaged={managed}"]
    result = subprocess.run(command, env=env)
    dll = ROOT / "dist" / f"{PLUGIN_NAME}.dll"
    if result.returncode != 0 or not dll.is_file():
        raise fail("the plugin build failed; see the compiler output above.")
    return dll.parent


# --------------------------------------------------------------------------
# installing into the game
# --------------------------------------------------------------------------

def install_bepinex(bepinex: Path, game: Path, launcher: Path) -> None:
    preloader = game / "BepInEx" / "core" / "BepInEx.Unity.Mono.Preloader.dll"
    if preloader.is_file():
        step("BepInEx already installed in the game folder; leaving it as it is")
    else:
        step(f"installing BepInEx into {game}")
        shutil.copytree(bepinex / "BepInEx", game / "BepInEx", dirs_exist_ok=True)
        if IS_WINDOWS:
            extra = ["winhttp.dll", "doorstop_config.ini", ".doorstop_version"]
        elif IS_LINUX:
            extra = ["libdoorstop.so", ".doorstop_version", "run_bepinex.sh"]
        else:
            extra = ["libdoorstop.dylib", ".doorstop_version", "run_bepinex.sh"]
        for name in extra:
            shutil.copy2(bepinex / name, game / name)
    if IS_MAC or IS_LINUX:
        script = game / "run_bepinex.sh"
        # macOS: the .app; Linux: the Unity executable. Both are looked up relative to the script's folder.
        wanted = str(launcher) if IS_MAC else launcher.name
        lines, replaced = [], False
        for line in script.read_text(encoding="utf-8").splitlines():
            if line.startswith("executable_name="):
                line, replaced = f'executable_name="{wanted}"', True
            lines.append(line)
        if not replaced:
            lines.insert(0, f'executable_name="{wanted}"')
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o755)


def copy_plugin(dist: Path, game: Path) -> Path:
    target = game / "BepInEx" / "plugins" / PLUGIN_NAME
    target.mkdir(parents=True, exist_ok=True)
    for name in (f"{PLUGIN_NAME}.dll", "ui.html"):
        shutil.copy2(dist / name, target / name)
    return target


def project_python() -> Path:
    python = ROOT / ".venv" / ("Scripts" if IS_WINDOWS else "bin") / ("python.exe" if IS_WINDOWS else "python")
    if not python.is_file():
        raise fail("the Python environment is missing. Run  python tools/setup.py  first.")
    return python


def write_studio_config(game: Path) -> Path:
    """Point the plugin's [Studio] section at this checkout and its virtualenv."""
    config = game / "BepInEx" / "config" / CONFIG_NAME
    config.parent.mkdir(parents=True, exist_ok=True)
    wanted = {
        "Python": str(project_python()),
        "Script": str(ROOT / "client" / "studio_server.py"),
        "WorkingDirectory": str(ROOT),
    }
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    if "[Studio]" not in text:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[Studio]\n\n"
    head, _, tail = text.partition("[Studio]")
    body, sep, rest = tail.partition("\n[")
    for key, value in wanted.items():
        line = f"{key} = {value}"
        if re.search(rf"^{key}\s*=", body, flags=re.M):
            body = re.sub(rf"^{key}\s*=.*$", lambda _m, line=line: line, body, flags=re.M)
        else:
            body = body.rstrip("\n") + f"\n{line}\n"
    config.write_text(head + "[Studio]" + body + (sep + rest if sep else ""), encoding="utf-8")
    return config


def launch_help(game: Path) -> str:
    if IS_WINDOWS:
        return ("Start the game from Steam as usual. BepInEx loads automatically.\n"
                "Then open  http://127.0.0.1:8765/  in your browser.")
    return ("In Steam: right-click the game -> Properties -> Launch Options, and paste:\n\n"
            f'    "{game / "run_bepinex.sh"}" %command%\n\n'
            "Then start the game from Steam and open  http://127.0.0.1:8765/")


def uninstall(game: Path) -> None:
    target = game / "BepInEx" / "plugins" / PLUGIN_NAME
    if target.is_dir():
        shutil.rmtree(target)
        step(f"removed {target}")
    else:
        step("the plugin was not installed")
    print("BepInEx itself was left in place; delete its files by hand if you also want it gone.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--game", help="folder that contains the game app/exe (auto-detected by default)")
    parser.add_argument("--check", action="store_true", help="report what would be done and exit")
    parser.add_argument("--uninstall", action="store_true", help="remove the plugin from the game")
    args = parser.parse_args()

    if PLATFORM is None:
        raise fail("only macOS, Windows and Linux are supported by this installer.")

    game, launcher, managed = find_game(args.game)
    step(f"game folder: {game}")
    check_mono(managed)
    if args.uninstall:
        uninstall(game)
        return 0
    if args.check:
        step(f"launcher: {launcher}")
        step(f"managed assemblies: {managed}")
        step(f".NET SDK: {find_dotnet()}")
        step(f"Python environment: {project_python()}")
        print("\nEverything needed was found. Run again without --check to install.")
        return 0

    dotnet = find_dotnet()
    project_python()
    bepinex = fetch_bepinex()
    dist = build_plugin(dotnet, bepinex, managed)
    install_bepinex(bepinex, game, launcher)
    plugin = copy_plugin(dist, game)
    config = write_studio_config(game)
    print(f"\nInstalled plugin:  {plugin}\nStudio config:     {config}\n")
    print(launch_help(game))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
