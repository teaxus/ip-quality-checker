"""Configuration management — API keys and settings stored in user home dir."""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".ip-quality-checker"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "api_keys": {
        "ipinfo": "",
        "ipqualityscore": "",
        "abuseipdb": "",
        "iphub": "",
        "ip2location": "",
    },
    "settings": {
        "request_timeout": 10,
        "max_workers": 12,
        "language": "zh",
        "auto_check_on_launch": True,
        # Periodic re-check. 0 disables.
        "auto_refresh_seconds": 120,
        # Watch for IP/gateway/interface changes and re-check on change.
        "network_change_detection": True,
        # Was 5 — bumped to 15 to reduce the long-run handle/subprocess churn
        # on Windows that contributed to a "未响应" hang after many hours.
        "network_poll_seconds": 15,
        # Flash widget red when score drops below this.
        "low_score_threshold": 40,
        # Significant drop = old - new >= this value (one-time alert burst).
        "score_drop_threshold": 20,
        # When the overall score drops below `low_score_threshold`, look for
        # any running process whose name/command line mentions "claude" and
        # force-kill it. Treats "claude" as a marker for the proxy/agent
        # process that needs to die when the network goes bad.
        "kill_claude_on_low_score": True,
        # Cap the overall score (currently 40) if the egress IP resolves to
        # mainland China. Highest-priority override — bypasses the per-check
        # tally. The cap value lives in checkers.CN_SCORE_CAP.
        "force_cn_score_cap": True,
        # Register a system-level autostart entry on save (LaunchAgent on
        # macOS, registry Run key on Windows, .desktop file on Linux).
        "auto_start_on_boot": False,
    },
    # Per-session UI state — restored on next launch
    "ui": {
        "main_geometry": "",     # e.g. "1240x900+100+80"
        "widget_pos": "",        # e.g. "+1720+80"
        "widget_snap": "",       # "" | "left" | "right" | "top" | "bottom"
        "mode": "main",          # "main" or "widget"
    },
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # merge defaults so new keys always exist
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in data.items():
            if isinstance(v, dict) and k in merged:
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass


def get_api_key(name: str) -> str:
    return load_config().get("api_keys", {}).get(name, "") or ""
