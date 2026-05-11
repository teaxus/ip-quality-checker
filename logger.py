"""Persistent logging — writes to file + optional callback (for UI display)."""
from __future__ import annotations

import threading
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
