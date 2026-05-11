"""Cross-platform system actions: kill Claude-related processes and toggle
auto-start on boot.

Why this exists:
  · When the network goes south, the user wants any local Claude proxy /
    helper / desktop process killed immediately so it can't keep talking
    to a now-tainted IP.
  · The user also wants the IP Quality Checker itself to come up on login
    so monitoring is always running.

All functions return (ok: bool, message: str) and never raise.
"""
from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

APP_NAME = "IPQualityChecker"
LAUNCH_AGENT_LABEL = "com.tom.ipqualitychecker"

# Domains we treat as "Claude-related" for the connection-based kill. We
# resolve these to live IPs at kill-time and match any active socket whose
# remote endpoint is one of those IPs.
CLAUDE_DOMAINS = (
    "claude.ai",
    "api.anthropic.com",
    "anthropic.com",
    "console.anthropic.com",
    "claude.com",
)


# ---------------------------------------------------------------------------
# 1) Kill any process that looks like a Claude desktop/proxy/helper
# ---------------------------------------------------------------------------
# These matchers are case-insensitive and intentionally broad — "宁可杀错
# 不可放过" per user policy. Catches the Claude desktop app (Anthropic),
# claude-cli, claude-code, anthropic-api-* helpers, any user-named
# "claude_proxy.py" / "anthropic_relay.go" and so on. We deliberately
# exclude this very app's name even though it has "claude" in its path
# because `IPQualityChecker` is what's running this code.
_CLAUDE_KEYWORDS = ("claude", "anthropic")
# Processes / paths we must NEVER kill, even if they match.
_EXCLUDE_KEYWORDS = (
    "ipqualitychecker",       # this app's binary
    "ip-quality-checker",     # this app's source dir
    "ip_quality_checker",
    "ipqc",                   # this app's CLI
)


def _looks_like_claude(name: str, cmdline: str = "") -> bool:
    haystack = f"{name}\n{cmdline}".lower()
    if not any(k in haystack for k in _CLAUDE_KEYWORDS):
        return False
    if any(k in haystack for k in _EXCLUDE_KEYWORDS):
        return False
    return True


def _list_claude_pids_unix() -> list[tuple[int, str]]:
    """ps -axo pid,comm,args style — works on macOS and Linux. Returns
    [(pid, label)] for processes whose name OR command line mentions claude
    but isn't this app."""
    out: list[tuple[int, str]] = []
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,comm=,args="],
            capture_output=True, text=True, timeout=4)
    except Exception:
        return out
    me = os.getpid()
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # split: pid  comm  args...
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == me:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) >= 3 else ""
        if _looks_like_claude(comm, args):
            label = comm
            if args:
                label = f"{comm} ({args[:80]})"
            out.append((pid, label))
    return out


def _list_claude_pids_windows() -> list[tuple[int, str]]:
    """tasklist /V /FO CSV — gives us image name + window title. Returns
    [(pid, label)]."""
    out: list[tuple[int, str]] = []
    try:
        r = subprocess.run(
            ["tasklist", "/V", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return out
    me = os.getpid()
    import csv, io
    reader = csv.reader(io.StringIO(r.stdout or ""))
    for row in reader:
        # Image, PID, Session, Sess#, Mem, Status, User, CPU, Title
        if len(row) < 2:
            continue
        image = row[0]
        try:
            pid = int(row[1])
        except ValueError:
            continue
        if pid == me:
            continue
        title = row[-1] if len(row) >= 9 else ""
        if _looks_like_claude(image, title):
            out.append((pid, f"{image} ({title})" if title else image))
    return out


# ---------------------------------------------------------------------------
# Connection-based detection — find PIDs holding sockets to Claude IPs even
# when the process name has nothing to do with Claude (e.g. python, curl,
# chrome, an MITM proxy, etc.)
# ---------------------------------------------------------------------------
def _resolve_claude_ips() -> set[str]:
    """Resolve every Claude-related host to its current IPv4 + IPv6 set."""
    ips: set[str] = set()
    for host in CLAUDE_DOMAINS:
        for fam in (socket.AF_INET, socket.AF_INET6):
            try:
                for info in socket.getaddrinfo(host, None, fam,
                                               socket.SOCK_STREAM):
                    ips.add(info[4][0])
            except Exception:
                pass
    return ips


def _extract_ip(addr: str) -> str:
    """'192.168.1.5:50231' → '192.168.1.5'; '[2001::1]:443' → '2001::1'."""
    addr = (addr or "").strip()
    if not addr:
        return ""
    if addr.startswith("["):
        end = addr.find("]")
        return addr[1:end] if end > 0 else addr
    # IPv4
    return addr.rsplit(":", 1)[0] if ":" in addr else addr


def _pids_connecting_to_unix(target_ips: set[str]) -> list[tuple[int, str]]:
    """Use lsof's -F output to find PIDs with sockets connecting to any of
    the supplied IPs. Returns [(pid, label)]."""
    if not target_ips:
        return []
    out: list[tuple[int, str]] = []
    try:
        r = subprocess.run(
            ["lsof", "-i", "-n", "-P", "-Fpcn"],
            capture_output=True, text=True, timeout=8)
    except Exception:
        return out
    me = os.getpid()
    cur_pid: int | None = None
    cur_cmd = ""
    for line in (r.stdout or "").splitlines():
        if not line:
            continue
        t, v = line[0], line[1:]
        if t == "p":
            try:
                cur_pid = int(v)
            except ValueError:
                cur_pid = None
            cur_cmd = ""
        elif t == "c":
            cur_cmd = v
        elif t == "n" and cur_pid is not None and cur_pid != me:
            # 'name' field. For an established connection looks like
            # "10.0.0.5:50231->1.2.3.4:443"; for a listener "*:8080".
            if "->" not in v:
                continue
            remote = v.split("->", 1)[1]
            ip = _extract_ip(remote)
            if ip in target_ips:
                # exclude this app's own processes
                hay = f"{cur_cmd}".lower()
                if any(k in hay for k in _EXCLUDE_KEYWORDS):
                    continue
                out.append((cur_pid, f"{cur_cmd} → {remote}"))
    return out


def _pids_connecting_to_windows(target_ips: set[str]) -> list[tuple[int, str]]:
    """netstat -ano + tasklist to map PID → image name. Matches any TCP/UDP
    connection whose foreign IP is in the target set."""
    if not target_ips:
        return []
    out: list[tuple[int, str]] = []
    try:
        r = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return out
    me = os.getpid()
    pid_remote: dict[int, str] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0] not in ("TCP", "UDP"):
            continue
        foreign = parts[2] if parts[0] == "TCP" else parts[2]
        ip = _extract_ip(foreign)
        if ip not in target_ips:
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid == me or pid <= 4:
            continue
        # keep first foreign endpoint seen per pid (de-dup)
        pid_remote.setdefault(pid, foreign)
    if not pid_remote:
        return out
    # Resolve PIDs to image names
    pid_to_name: dict[int, str] = {}
    try:
        r2 = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for row in csv.reader(io.StringIO(r2.stdout or "")):
            if len(row) >= 2:
                try:
                    pid_to_name[int(row[1])] = row[0]
                except ValueError:
                    pass
    except Exception:
        pass
    for pid, foreign in pid_remote.items():
        name = pid_to_name.get(pid, "?")
        hay = name.lower()
        if any(k in hay for k in _EXCLUDE_KEYWORDS):
            continue
        out.append((pid, f"{name} → {foreign}"))
    return out


def list_claude_connections() -> list[tuple[int, str]]:
    """Public: list any process with an active socket to a Claude domain."""
    ips = _resolve_claude_ips()
    if platform.system() == "Windows":
        return _pids_connecting_to_windows(ips)
    return _pids_connecting_to_unix(ips)


def list_claude_processes() -> list[tuple[int, str]]:
    """Public: everything we'd kill — name-based matches AND any process
    holding a socket to a Claude domain. PIDs are de-duplicated."""
    if platform.system() == "Windows":
        by_name = _list_claude_pids_windows()
    else:
        by_name = _list_claude_pids_unix()
    by_conn = list_claude_connections()
    seen: dict[int, str] = {}
    for pid, label in by_name:
        seen[pid] = label
    for pid, label in by_conn:
        if pid in seen:
            # enrich existing entry with the connection info
            if "→" not in seen[pid]:
                seen[pid] = f"{seen[pid]} | {label}"
        else:
            seen[pid] = f"⚡ 连接 Claude: {label}"
    return sorted(seen.items(), key=lambda x: x[0])


def kill_claude_processes() -> tuple[bool, str, list[str]]:
    """Find all Claude-flavored processes and force-kill them.

    Returns (ok, message, killed_labels).
    `ok` is True even when the list is empty — nothing to do is fine.
    """
    procs = list_claude_processes()
    if not procs:
        return True, "未发现 Claude 相关进程", []

    killed: list[str] = []
    failed: list[str] = []
    is_win = platform.system() == "Windows"
    for pid, label in procs:
        try:
            if is_win:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid), "/T"],
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                # SIGKILL — proxy processes often ignore SIGTERM during
                # active sockets, and the user wants them dead now.
                os.kill(pid, 9)
            killed.append(f"{pid} {label}")
        except ProcessLookupError:
            killed.append(f"{pid} {label} (已退出)")
        except PermissionError:
            failed.append(f"{pid} {label} (权限不足)")
        except Exception as e:
            failed.append(f"{pid} {label} ({e})")

    msg_parts = []
    if killed:
        msg_parts.append(f"已结束 {len(killed)} 个进程")
    if failed:
        msg_parts.append(f"{len(failed)} 个失败")
    return (len(failed) == 0, " · ".join(msg_parts) or "无操作", killed + failed)


# ---------------------------------------------------------------------------
# 2) Auto-start on boot
# ---------------------------------------------------------------------------
def _current_launch_target() -> tuple[str, list[str]]:
    """Return (executable, args) tuple to invoke this app on boot.

    Three cases:
      · PyInstaller-frozen single-file → sys.executable (the binary)
      · PyInstaller-frozen .app bundle (macOS) → the inner Mach-O binary
      · running from source → python interpreter + path to main.py
    """
    if getattr(sys, "frozen", False):
        # When the app is launched from a .app bundle on macOS, sys.executable
        # is .../IPQualityChecker.app/Contents/MacOS/IPQualityChecker which
        # is exactly what we want.
        return sys.executable, []
    main_py = Path(__file__).parent / "main.py"
    return sys.executable, [str(main_py)]


# ---- macOS: LaunchAgent plist -----------------------------------------------
def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _macos_set_autostart(enabled: bool) -> tuple[bool, str]:
    plist = _macos_plist_path()
    if not enabled:
        try:
            if plist.exists():
                # try to unload first; ignore errors (it may not be loaded)
                subprocess.run(
                    ["launchctl", "unload", str(plist)],
                    capture_output=True, timeout=4)
                plist.unlink()
            return True, "已移除 LaunchAgent"
        except Exception as e:
            return False, f"移除失败: {e}"

    exe, args = _current_launch_target()
    program_args = [exe] + args
    program_xml = "\n".join(
        f"        <string>{a}</string>" for a in program_args)
    plist_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(plist_xml, encoding="utf-8")
        # reload (unload existing first to pick up new plist)
        subprocess.run(["launchctl", "unload", str(plist)],
                       capture_output=True, timeout=4)
        r = subprocess.run(["launchctl", "load", str(plist)],
                           capture_output=True, text=True, timeout=4)
        if r.returncode != 0 and r.stderr:
            return True, f"plist 已写入，但 launchctl 提示: {r.stderr.strip()}"
        return True, f"已写入 {plist.name}"
    except Exception as e:
        return False, f"写入失败: {e}"


# ---- Windows: HKCU\...\Run registry key -------------------------------------
def _windows_set_autostart(enabled: bool) -> tuple[bool, str]:
    try:
        import winreg  # type: ignore
    except ImportError:
        return False, "winreg 不可用"
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as k:
            if not enabled:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
                return True, "已删除注册表自启动项"
            exe, args = _current_launch_target()
            # Quote the executable path; pass any extra args un-quoted
            # (script paths get their own quoting below).
            cmd = f'"{exe}"'
            for a in args:
                cmd += f' "{a}"'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
            return True, "已写入注册表自启动项"
    except Exception as e:
        return False, f"注册表写入失败: {e}"


# ---- Linux: ~/.config/autostart/*.desktop -----------------------------------
def _linux_desktop_path() -> Path:
    return Path.home() / ".config" / "autostart" / f"{APP_NAME}.desktop"


def _linux_set_autostart(enabled: bool) -> tuple[bool, str]:
    p = _linux_desktop_path()
    if not enabled:
        try:
            if p.exists():
                p.unlink()
            return True, "已移除 .desktop"
        except Exception as e:
            return False, f"移除失败: {e}"
    exe, args = _current_launch_target()
    cmd = " ".join(['"%s"' % exe] + ['"%s"' % a for a in args])
    body = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={cmd}\n"
        "Hidden=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Terminal=false\n"
    )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        try:
            os.chmod(p, 0o755)
        except Exception:
            pass
        return True, f"已写入 {p.name}"
    except Exception as e:
        return False, f"写入失败: {e}"


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Enable or disable launch-on-boot for this app. Cross-platform."""
    sysname = platform.system()
    if sysname == "Darwin":
        return _macos_set_autostart(enabled)
    if sysname == "Windows":
        return _windows_set_autostart(enabled)
    return _linux_set_autostart(enabled)


def is_autostart_enabled() -> bool:
    """Best-effort: check whether the autostart entry exists right now."""
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            return _macos_plist_path().exists()
        if sysname == "Windows":
            import winreg  # type: ignore
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
                try:
                    winreg.QueryValueEx(k, APP_NAME)
                    return True
                except FileNotFoundError:
                    return False
        return _linux_desktop_path().exists()
    except Exception:
        return False
