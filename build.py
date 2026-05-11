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

Remote build (via GitHub Actions)
----------------------------------
    python build.py --remote                      # build all 4 targets on CI
    python build.py --remote --platform windows   # only Windows targets
    python build.py --remote --platform macos     # only macOS targets

    Requires: gh CLI installed and authenticated (`gh auth login`).
    Artifacts are downloaded and extracted into dist/ automatically.

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
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
APP_NAME = "IPQualityChecker"
CLI_NAME = "ipqc"
REPO = "teaxus/ip-quality-checker"
WORKFLOW = "build.yml"


def _separator() -> str:
    return ";" if platform.system() == "Windows" else ":"


def _display_path(path: Path) -> str:
    """Render path relative to project root when possible."""
    root_resolved = ROOT.resolve()
    p = path.resolve()
    try:
        return str(p.relative_to(root_resolved))
    except ValueError:
        return str(p)


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
              out_suffix: str | None,
              out_dir: Path) -> None:
    name = APP_NAME + (f"-{out_suffix}" if out_suffix else "")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--name", name,
            "--distpath", str(out_dir),
           "--windowed"]
    cmd += _common_args(onedir, target_arch)
    cmd += _icon_arg()
    cmd += _icon_data_arg()
    cmd.append("main.py")
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


def build_cli(onedir: bool, target_arch: str | None,
              out_suffix: str | None,
              out_dir: Path) -> None:
    name = CLI_NAME + (f"-{out_suffix}" if out_suffix else "")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--name", name,
            "--distpath", str(out_dir),
           "--console"]
    cmd += _common_args(onedir, target_arch)
    cmd += _icon_arg()
    cmd.append("cli.py")
    print(">>>", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)


# ── Remote build helpers ──────────────────────────────────────────────────

def _gh(*args: str, capture: bool = False) -> "subprocess.CompletedProcess[str]":
    """Run a gh CLI command, raising on non-zero exit."""
    cmd = ["gh"] + list(args)
    if capture:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    return subprocess.run(cmd, check=True)


def remote_build(target_platform: str, out_dir: Path) -> None:
    """Trigger GitHub Actions, wait for completion, extract artifacts to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Trigger workflow ──────────────────────────────────────────────────
    print(f"[remote] Triggering {WORKFLOW} on GitHub (platform={target_platform}) ...")
    _gh("workflow", "run", WORKFLOW,
        "--repo", REPO,
        "-f", f"platform={target_platform}")

    # Give GitHub a moment to register the new run
    time.sleep(5)

    # ── Get the latest run ID ─────────────────────────────────────────────
    result = _gh("run", "list",
                 "--repo", REPO,
                 "--workflow", WORKFLOW,
                 "--limit", "1",
                 "--json", "databaseId,status,event",
                 capture=True)
    runs = json.loads(result.stdout)
    if not runs:
        sys.exit("[remote] ERROR: no run found after triggering workflow")
    run_id = str(runs[0]["databaseId"])
    print(f"[remote] Run ID: {run_id}  https://github.com/{REPO}/actions/runs/{run_id}")

    # ── Poll until all jobs finish ────────────────────────────────────────
    label_filter = target_platform  # 'windows' / 'macos' / 'all'
    print("[remote] Waiting for build to complete (polling every 15s) ...")
    for attempt in range(120):  # up to 30 minutes
        r = _gh("run", "view", run_id,
                "--repo", REPO,
                "--json", "status,conclusion,jobs",
                capture=True)
        data = json.loads(r.stdout)
        jobs = data["jobs"]

        if label_filter != "all":
            jobs = [j for j in jobs if j["name"].startswith(label_filter)]

        pending = [j for j in jobs if j["status"] != "completed"]
        failed  = [j for j in jobs if j["conclusion"] == "failure"]

        status_line = ", ".join(
            f"{j['name']}:{j['status']}({j['conclusion'] or '...'})" for j in jobs
        )
        print(f"  [{attempt * 15:>4}s] {status_line}")

        if failed:
            sys.exit(f"[remote] ERROR: jobs failed — {[j['name'] for j in failed]}")
        if not pending:
            print("[remote] All target jobs completed successfully.")
            break
        time.sleep(15)
    else:
        sys.exit("[remote] Timed out waiting for workflow to finish.")

    # ── Download + extract artifacts ──────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        print(f"[remote] Downloading artifacts to {_display_path(out_dir)} ...")
        _gh("run", "download", run_id,
            "--repo", REPO,
            "-D", str(tmp_path))

        for artifact_dir in tmp_path.iterdir():
            if not artifact_dir.is_dir():
                continue
            # Filter by platform if needed
            if label_filter != "all" and not artifact_dir.name.lower().startswith(
                    "ipqualitychecker-" + label_filter):
                continue
            for zip_file in artifact_dir.glob("*.zip"):
                print(f"  Extracting {zip_file.name} -> {_display_path(out_dir)}/")
                with zipfile.ZipFile(zip_file) as zf:
                    zf.extractall(out_dir)

    print(f"\n[OK] Remote build complete. Output in {_display_path(out_dir)}")
    for item in sorted(out_dir.iterdir()):
        print(f"  {item.name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", action="store_true",
                        help="build on GitHub Actions and download artifacts to dist/")
    parser.add_argument("--platform", choices=("windows", "macos", "all"),
                        default="all",
                        help="remote build only: which platform to target (default: all)")
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
    parser.add_argument("--out-dir", default="dist",
                        help="output directory (default: dist)")
    parser.add_argument("--clean", action="store_true",
                        help="purge build/ and dist/ first")
    parser.add_argument("--cli", action="store_true",
                        help="also build the CLI binary")
    parser.add_argument("--cli-only", action="store_true",
                        help="only build CLI, skip GUI")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir = out_dir.resolve()

    # Safety guard: never allow output directory to be the project root.
    if out_dir == ROOT.resolve():
        sys.exit("[ERROR] --out-dir cannot be project root. Please use a subdir like dist/.")

    # ── Remote build path ──
    if args.remote:
        remote_build(args.platform, out_dir)
        return

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
        for d in ("build",
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
        if out_dir.exists() and out_dir.is_dir():
            shutil.rmtree(out_dir, ignore_errors=True)
        print(f"[OK] cleaned build/ {_display_path(out_dir)}/ *.spec")

    if not args.cli_only:
        build_gui(args.onedir, args.arch, args.out_suffix, out_dir)
    if args.cli or args.cli_only:
        build_cli(args.onedir, args.arch, args.out_suffix, out_dir)

    print(f"\n[OK] build complete. Output in {_display_path(out_dir)}")
    sys_name = platform.system()
    gui_name = APP_NAME + (f"-{args.out_suffix}" if args.out_suffix else "")
    cli_name = CLI_NAME + (f"-{args.out_suffix}" if args.out_suffix else "")
    if sys_name == "Darwin":
        print(f"  GUI:  {out_dir/f'{gui_name}.app'}")
    elif sys_name == "Windows":
        print(f"  GUI:  {out_dir/f'{gui_name}.exe'}")
    else:
        print(f"  GUI:  {out_dir/gui_name}")
    if args.cli or args.cli_only:
        ext = ".exe" if sys_name == "Windows" else ""
        print(f"  CLI:  {out_dir/(cli_name + ext)}")


if __name__ == "__main__":
    main()
