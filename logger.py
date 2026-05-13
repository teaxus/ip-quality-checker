"""Persistent logging — writes to file + optional callback (for UI display)."""
from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".ip-quality-checker" / "logs"
LOG_FILE = LOG_DIR / f"{datetime.now():%Y%m%d}.log"


class Logger:
    """Thread-safe logger that writes to file and calls a callback (for UI).
    
    Auto-rotates logs daily. Keeps file handle open for performance.
    """
    
    def __init__(self, on_line_callback=None):
        self.callback = on_line_callback
        self._lock = threading.Lock()
        self._file = None
        self._ensure_file()
    
    def _ensure_file(self):
        """Create/open log file if needed."""
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            # Re-check date in case we rolled over
            today_file = LOG_DIR / f"{datetime.now():%Y%m%d}.log"
            if self._file and self._file.name != today_file.name:
                # Day changed — close old file and open new one
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            if not self._file or self._file.closed:
                self._file = today_file.open("a", encoding="utf-8")
        except Exception:
            self._file = None
    
    def log(self, line: str):
        """Write a line to both file and callback (if provided)."""
        ts = datetime.now().strftime("%H:%M:%S")
        full_line = f"[{ts}] {line}"
        
        # File write (thread-safe)
        with self._lock:
            try:
                self._ensure_file()
                if self._file:
                    self._file.write(full_line + "\n")
                    self._file.flush()
            except Exception:
                pass
        
        # UI callback (outside lock to avoid deadlock)
        if self.callback:
            try:
                self.callback(full_line)
            except Exception:
                pass
    
    def close(self):
        """Close the log file."""
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None


# Global logger instance
_global_logger = None


def get_logger() -> Logger:
    """Get or create the global logger."""
    global _global_logger
    if _global_logger is None:
        _global_logger = Logger()
    return _global_logger


def set_logger_callback(callback):
    """Set the callback for UI display (called after file write)."""
    logger = get_logger()
    logger.callback = callback


def log(line: str):
    """Log a line to file and UI."""
    get_logger().log(line)


def get_log_file_path() -> Path:
    """Return path to today's log file."""
    return LOG_FILE


def get_log_files_list() -> list[Path]:
    """Return all available log files (newest first)."""
    try:
        return sorted(LOG_DIR.glob("*.log"), reverse=True)
    except Exception:
        return []


def install_crash_handlers() -> None:
    """Install global handlers so any uncaught exception — in main thread or
    worker threads — is written to the log file before the process exits."""

    def _write_crash(header: str, exc_type, exc_value, exc_tb) -> None:
        try:
            tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            log(f"{'=' * 60}")
            log(f"[CRASH] {header}")
            log(f"[CRASH] {exc_type.__name__}: {exc_value}")
            for line in tb_str.splitlines():
                log(f"[CRASH] {line}")
            log(f"{'=' * 60}")
        except Exception:
            pass  # never let the crash handler itself crash

    # Main thread uncaught exceptions
    _orig_excepthook = sys.excepthook
    def _main_excepthook(exc_type, exc_value, exc_tb):
        _write_crash("主线程未捕获异常", exc_type, exc_value, exc_tb)
        _orig_excepthook(exc_type, exc_value, exc_tb)
    sys.excepthook = _main_excepthook

    # Worker thread uncaught exceptions (Python 3.8+)
    _orig_thread_hook = threading.excepthook
    def _thread_excepthook(args):
        _write_crash(
            f"子线程 {args.thread.name!r} 未捕获异常",
            args.exc_type, args.exc_value, args.exc_traceback,
        )
        _orig_thread_hook(args)
    threading.excepthook = _thread_excepthook

    # Tee sys.stderr → log file so Python warnings / tracebacks printed
    # directly to stderr (e.g. from C extensions) are also captured.
    class _StderrTee:
        """Forward writes to the original stderr AND the log file."""
        def __init__(self, original):
            self._orig = original
            self._buf = ""

        def write(self, text: str):
            try:
                self._orig.write(text)
            except Exception:
                pass
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                stripped = line.strip()
                if stripped:
                    try:
                        log(f"[STDERR] {line}")
                    except Exception:
                        pass

        def flush(self):
            try:
                self._orig.flush()
            except Exception:
                pass

        def fileno(self):
            return self._orig.fileno()

        def isatty(self):
            try:
                return self._orig.isatty()
            except Exception:
                return False

        def __getattr__(self, name):
            return getattr(self._orig, name)

    sys.stderr = _StderrTee(sys.stderr)
