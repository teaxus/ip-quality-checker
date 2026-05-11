"""Cross-platform packaging via PyInstaller.

Usage
-----
    python build.py                       # build for current OS/arch
    python build.py --arch arm64          # macOS only: target Apple Silicon
    python build.py --arch x86_64         # macOS only: target Intel
    python build.py --arch universal2     # macOS only: single fat binary
    python build.py --onedir              # force folder layout
    python build.py --onefile             # force single-file
    python build.py --clean               # purge build/ and dist/ first
    python build.py --cli                 # also build the CLI binary
    python build.py --out-suffix=arm64    # rename output (dist/IPQualityChecker-arm64.app)

About cross-compilation
-----------------------
PyInstaller does NOT support cross-OS compilation. To produce all four
targets (macOS arm64 + macOS x86_64 + Windows arm64 + Windows x86_64) you
must run it on each platform. The companion scripts:

  · scripts/build-all.sh   — drives macOS bulk build (arm64 + x86_64)
  · scripts/build-all.ps1  — drives Windows bulk build (arm64 + x86_64)
  · .github/workflows/build.yml — GitHub Actions matrix; pushes a tag and
    you get 4 artifacts attached to a release

Defaults
--------
- macOS  → --onedir (sub-second .app launch; no self-extract)
- Win/Linux → --onefile (single .exe / binary)
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
APP_NAME = "IPQualityChecker"
CLI_NAME = "ipqc"


def _separator() -> str:
    return ";" if platform.system() == "Windows" else ":"


def _common_args(onedir: bool, target_arch: str | None) -> list[str]:
    """Common PyInstaller flags shared by both GUI and CLI builds."""
    args = [
        "--noconfirm",
        "--onedir" if onedir else "--onefile",
        # Pull in the entire customtkinter package — its theme JSON, fonts,
        # and dynamic imports are not auto-detected by PyInstaller.
        "--collect-all", "customtkinter",
        "--collect-all", "darkdetect",
    ]
    # macOS-only: PyInstaller's --target-arch produces an arm64-only,
    # x86_64-only, or universal2 binary. Requires Python itself to be
    # built with the same arch (universal2 Python from python.org works).
    if target_arch and platform.system() == "Darwin":
        args += ["--target-arch", target_arch]
    return args


def _icon_arg() -> list[str]:
    """Pick the right icon file for the current OS, if it exists."""
    sysname = platform.system()
    if sysname == "Darwin":
        path = ROOT / "icon.icns"
    elif sysname == "Windows":
        path = ROOT / "icon.ico"
    else:
        path = ROOT / "icon.png"
    if path.exists():
        return ["--icon", str(path)]
    return []


def _icon_data_arg() -> list[str]:
    """Bundle icon.png into the app so the runtime can set window icon."""
    sep = _separator()
    arg = []
    for f in ("icon.png", "icon.ico", "icon.icns"):
        p = ROOT / f
        if p.exists():
            arg += ["--add-data", f"{p}{sep}."]
    return arg


def build_gui(onedir: bool, target_arch: str | None,
              out_suffix: str | None) -> None:
    name = APP_NAME + (f"-{out_suffix}" if out_suffix else "")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--name", name,
           "--windowed"]
    cmd += _common_args(onedir, target_arch)
    cmd += _icon_arg()
    cmd += _icon_data_arg()
    cmd.append("main.py")
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def build_cli(onedir: bool, target_arch: str | None,
              out_suffix: str | None) -> None:
    name = CLI_NAME + (f"-{out_suffix}" if out_suffix else "")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--name", name,
           "--console"]
    cmd += _common_args(onedir, target_arch)
    cmd += _icon_arg()
    cmd.append("cli.py")
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--onedir", action="store_true",
                        help="folder layout (default on macOS — fast launch)")
    parser.add_argument("--onefile", action="store_true",
                        help="force single-file (slow first launch on macOS)")
    parser.add_argument("--arch", choices=("arm64", "x86_64", "universal2"),
                        default=None,
                        help="macOS only: target architecture")
    parser.add_argument("--out-suffix", default=None,
                        help="append a suffix to output names "
                             "(e.g. IPQualityChecker-arm64.app)")
    parser.add_argument("--clean", action="store_true",
                        help="purge build/ and dist/ first")
    parser.add_argument("--cli", action="store_true",
                        help="also build the CLI binary")
    parser.add_argument("--cli-only", action="store_true",
                        help="only build CLI, skip GUI")
    args = parser.parse_args()

    # ── Smart default ──
    if not args.onedir and not args.onefile:
        args.onedir = (platform.system() == "Darwin")
    layout = "onedir" if args.onedir else "onefile"
    arch = args.arch or platform.machine()
    suffix = f" [suffix={args.out_suffix}]" if args.out_suffix else ""
    print(f"build layout: {layout} | arch: {arch} | "
          f"platform: {platform.system()}{suffix}")

    if args.arch and platform.system() != "Darwin":
        print("[WARN] --arch is only honoured on macOS. On Windows / Linux the "
              "arch is determined by which Python interpreter you invoke.")

    if args.clean:
        for d in ("build", "dist",
                   f"{APP_NAME}.spec", f"{CLI_NAME}.spec",
                   f"{APP_NAME}-{args.out_suffix}.spec" if args.out_suffix else "",
                   f"{CLI_NAME}-{args.out_suffix}.spec" if args.out_suffix else ""):
            if not d:
                continue
            p = ROOT / d
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)
        print("[OK] cleaned build/ dist/ *.spec")

    if not args.cli_only:
        build_gui(args.onedir, args.arch, args.out_suffix)
    if args.cli or args.cli_only:
        build_cli(args.onedir, args.arch, args.out_suffix)

    print(f"\n[OK] build complete. Output in {ROOT/'dist'}")
    sys_name = platform.system()
    gui_name = APP_NAME + (f"-{args.out_suffix}" if args.out_suffix else "")
    cli_name = CLI_NAME + (f"-{args.out_suffix}" if args.out_suffix else "")
    if sys_name == "Darwin":
        print(f"  GUI:  {ROOT/'dist'/f'{gui_name}.app'}")
    elif sys_name == "Windows":
        print(f"  GUI:  {ROOT/'dist'/f'{gui_name}.exe'}")
    else:
        print(f"  GUI:  {ROOT/'dist'/gui_name}")
    if args.cli or args.cli_only:
        ext = ".exe" if sys_name == "Windows" else ""
        print(f"  CLI:  {ROOT/'dist'/(cli_name + ext)}")


if __name__ == "__main__":
    main()
