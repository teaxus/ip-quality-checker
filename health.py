"""Process-resource health probe.

Returns a snapshot of indicators that grow when something is leaking:
- Windows GDI / USER object counts (the primary suspect for the "未响应"
  hang reported in 2026-05; default per-process limit is 10,000 each).
- Process handle count (Windows) / open file descriptors (POSIX).
- Resident memory (RSS).
- Live thread count.
- Live Tk widget count (recursive).

Pure stdlib — no psutil dependency. Each platform-specific probe is wrapped
in try/except and returns None on failure, so the function never raises.
"""
from __future__ import annotations

import ctypes
import os
import platform
import resource
import threading


def _win_gui_resources() -> tuple[int | None, int | None]:
    """Return (gdi_count, user_count) on Windows; (None, None) elsewhere or on
    error. Uses user32.GetGuiResources — the same number Task Manager shows
    in the "GDI objects" / "USER objects" columns."""
    if platform.system() != "Windows":
        return None, None
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetCurrentProcess()
        # 0 = GR_GDIOBJECTS, 1 = GR_USEROBJECTS
        gdi = int(user32.GetGuiResources(h, 0))
        usr = int(user32.GetGuiResources(h, 1))
        return gdi, usr
    except Exception:
        return None, None


def _win_handle_count() -> int | None:
    """Total kernel handle count for the current process (Windows)."""
    if platform.system() != "Windows":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        count = ctypes.c_ulong(0)
        ok = kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(), ctypes.byref(count))
        if ok:
            return int(count.value)
    except Exception:
        pass
    return None


def _posix_fd_count() -> int | None:
    """Open file descriptors on POSIX (Linux: /proc/self/fd, macOS: /dev/fd)."""
    if platform.system() == "Windows":
        return None
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path))
        except Exception:
            continue
    return None


def _rss_bytes() -> int | None:
    """Resident memory in bytes."""
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes. Heuristic: if the number is
        # small, assume KB.
        if platform.system() == "Darwin":
            return int(ru)
        return int(ru) * 1024
    except Exception:
        pass
    # Windows: use PROCESS_MEMORY_COUNTERS via psapi
    if platform.system() == "Windows":
        try:
            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            psapi = ctypes.WinDLL("Psapi.dll")
            kernel32 = ctypes.windll.kernel32
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(),
                ctypes.byref(pmc), ctypes.sizeof(PMC))
            if ok:
                return int(pmc.WorkingSetSize)
        except Exception:
            pass
    return None


def _tk_widget_count(root) -> int | None:
    """Count every live Tk widget under `root` (recursive)."""
    if root is None:
        return None
    try:
        total = 0
        stack = [root]
        while stack:
            w = stack.pop()
            try:
                kids = w.winfo_children()
            except Exception:
                continue
            total += len(kids)
            stack.extend(kids)
        return total
    except Exception:
        return None


def snapshot(tk_root=None) -> dict:
    """Return a dict of current resource indicators. All keys present;
    values are int or None (None = probe failed / not applicable here)."""
    gdi, usr = _win_gui_resources()
    return {
        "gdi": gdi,
        "user": usr,
        "handles": _win_handle_count(),
        "fds": _posix_fd_count(),
        "rss_mb": (_rss_bytes() or 0) // (1024 * 1024) or None,
        "threads": threading.active_count(),
        "widgets": _tk_widget_count(tk_root),
    }


def summary_line(snap: dict) -> str:
    """One-line human-readable rendering of a snapshot, skipping None fields."""
    parts: list[str] = []
    order = [
        ("rss_mb", "RSS", "MB"),
        ("threads", "线程", ""),
        ("widgets", "Tk", ""),
        ("gdi", "GDI", ""),
        ("user", "USER", ""),
        ("handles", "句柄", ""),
        ("fds", "fd", ""),
    ]
    for key, label, unit in order:
        v = snap.get(key)
        if v is None:
            continue
        parts.append(f"{label}={v}{unit}")
    return "  ".join(parts) if parts else "(无可用指标)"
