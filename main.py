"""IP & Network Quality Checker — cross-platform GUI (Windows / macOS).

Aggregates IP risk, fraud, geolocation, AI service unlock (Claude/ChatGPT/
Gemini), streaming unlock, DNS, latency, speed probes — plus a Claude-focused
tab that mirrors https://ip.net.coffee/claude/.

UI: forced dark "tech" theme with neon accents. Includes a 360-style floating
widget (frameless, always-on-top, draggable) showing a live score gauge.
"""
from __future__ import annotations

import json
import math
import os
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import checkers
from config import load_config, save_config
import system_actions


# ── Resource path resolver — works both in dev and when packaged ──────────
def resource_path(name: str) -> str:
    """Return the absolute path to a bundled resource. Works when frozen by
    PyInstaller (resources land in sys._MEIPASS) and when running from
    source."""
    if hasattr(sys, "_MEIPASS"):
        candidate = Path(sys._MEIPASS) / name
        if candidate.exists():
            return str(candidate)
    here = Path(__file__).parent / name
    return str(here)


# ── Multi-monitor bounds: returns (x, y, w, h) of the monitor containing
#    the given point. Falls back to whole virtual desktop, then primary. ──
def monitor_bounds_at(x: int, y: int) -> tuple[int, int, int, int]:
    try:
        from screeninfo import get_monitors
        for m in get_monitors():
            if m.x <= x < m.x + m.width and m.y <= y < m.y + m.height:
                return (m.x, m.y, m.width, m.height)
        # No monitor contains the point — pick the primary
        for m in get_monitors():
            if getattr(m, "is_primary", False):
                return (m.x, m.y, m.width, m.height)
        # Fallback: union of all monitors
        ms = get_monitors()
        if ms:
            xs = [m.x for m in ms]
            ys = [m.y for m in ms]
            xe = [m.x + m.width for m in ms]
            ye = [m.y + m.height for m in ms]
            return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))
    except Exception:
        pass
    # Last-resort: assume single primary monitor based on Tk root
    return (0, 0, 1920, 1080)


def all_monitors_rect() -> tuple[int, int, int, int]:
    """Bounding box of the entire virtual desktop."""
    try:
        from screeninfo import get_monitors
        ms = get_monitors()
        if ms:
            xs = [m.x for m in ms]
            ys = [m.y for m in ms]
            xe = [m.x + m.width for m in ms]
            ye = [m.y + m.height for m in ms]
            return (min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys))
    except Exception:
        pass
    return (0, 0, 1920, 1080)

APP_NAME = "IP 网络质量评估"
APP_VERSION = "1.2.0"

# ── Tech-style dark palette ────────────────────────────────────────────────
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

PALETTE = {
    "bg":          "#0a0e1a",   # deepest background
    "panel":       "#111726",   # card background
    "panel_hi":    "#172033",   # raised card
    "border":      "#1f2a44",
    "text":        "#e6edf3",
    "muted":       "#8b95a8",
    "accent":      "#00d4ff",   # neon cyan
    "accent_dim":  "#0fa8cc",
    "ok":          "#3ddc97",   # neon green
    "warn":        "#ffc857",   # amber
    "fail":        "#ff5e5b",   # neon red
    "manual":      "#a78bfa",   # purple
    "error":       "#7d8896",
}

STATUS_COLOR = {
    "ok": PALETTE["ok"], "warn": PALETTE["warn"], "fail": PALETTE["fail"],
    "manual": PALETTE["manual"], "error": PALETTE["error"],
    "running": PALETTE["accent"],
}
STATUS_ICONS = {
    "ok": "✓", "warn": "⚠", "fail": "✗",
    "manual": "?", "error": "!", "running": "…",
}
SCORE_BANDS = [
    # (min_score_inclusive, color, label)
    (90, PALETTE["ok"],     "极佳"),
    (80, PALETTE["ok"],     "优秀"),
    (60, PALETTE["warn"],   "良好"),
    (40, "#ff8c4a",         "一般"),
    (0,  PALETTE["fail"],   "较差"),
]


def score_color_label(score: int) -> tuple[str, str]:
    for thresh, color, label in SCORE_BANDS:
        if score >= thresh:
            return color, label
    return PALETTE["fail"], "无数据"


# ============================================================================
# A glowing score ring drawn on a tk.Canvas — used by both main and floating
# widget. Renders an arc whose color matches the score band.
# ============================================================================
class ScoreRing(tk.Canvas):
    def __init__(self, master, size: int = 160, ring_w: int = 10, **kw):
        super().__init__(master, width=size, height=size, bg=PALETTE["bg"],
                         highlightthickness=0, **kw)
        self.size = size
        self.ring_w = ring_w
        self._score: int | None = None
        self._draw(0, PALETTE["muted"])

    def set_score(self, score: int | None):
        self._score = score
        if score is None:
            color = PALETTE["muted"]
            pct = 0
        else:
            color, _ = score_color_label(score)
            pct = max(0, min(100, score))
        self._draw(pct, color)

    def _draw(self, pct: int, color: str):
        self.delete("all")
        s = self.size
        pad = self.ring_w + 2
        # background ring (faint)
        self.create_arc(
            pad, pad, s - pad, s - pad,
            start=90, extent=-359.999,
            outline=PALETTE["border"], width=self.ring_w, style=tk.ARC,
        )
        # foreground arc proportional to score
        if pct > 0:
            extent = -3.6 * pct  # tk start=90 (top), negative = clockwise
            self.create_arc(
                pad, pad, s - pad, s - pad,
                start=90, extent=extent,
                outline=color, width=self.ring_w, style=tk.ARC,
            )
        # subtle inner ring for depth
        inner = pad + self.ring_w + 4
        self.create_oval(
            inner, inner, s - inner, s - inner,
            outline=PALETTE["panel_hi"], width=1,
        )
        # center text
        text = "—" if self._score is None else str(self._score)
        font_size = int(s * 0.32)
        self.create_text(s / 2, s / 2 - 4,
                         text=text, fill=color,
                         font=("SF Pro Display", font_size, "bold"))
        if self._score is not None:
            _, label = score_color_label(self._score)
            self.create_text(s / 2, s / 2 + font_size * 0.55,
                             text=label, fill=PALETTE["muted"],
                             font=("SF Pro Display", int(s * 0.09)))
        else:
            self.create_text(s / 2, s / 2 + font_size * 0.55,
                             text="待检测",
                             fill=PALETTE["muted"],
                             font=("SF Pro Display", int(s * 0.09)))


# ============================================================================
# Result card with verify-URL + detail buttons (tech-style)
# ============================================================================
class ResultCard(ctk.CTkFrame):
    def __init__(self, master, title: str, **kwargs):
        super().__init__(master, corner_radius=8,
                         fg_color=PALETTE["panel"],
                         border_color=PALETTE["border"], border_width=1,
                         **kwargs)
        self.title = title
        self.detail_data: dict | None = None
        self.verify_url: str = ""
        self.request_url: str = ""

        self.grid_columnconfigure(1, weight=1)

        self.icon_label = ctk.CTkLabel(
            self, text="…", width=28,
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=PALETTE["accent"])
        self.icon_label.grid(row=0, column=0, rowspan=2,
                             padx=(12, 6), pady=10, sticky="ns")

        self.title_label = ctk.CTkLabel(
            self, text=title, anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(size=13, weight="bold"))
        self.title_label.grid(row=0, column=1, sticky="ew",
                              padx=4, pady=(10, 0))

        self.summary_label = ctk.CTkLabel(
            self, text="待检测…", anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=PALETTE["muted"],
            wraplength=560, justify="left")
        self.summary_label.grid(row=1, column=1, sticky="ew",
                                padx=4, pady=(0, 10))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=0, column=2, rowspan=2, padx=(4, 12),
                       pady=8, sticky="e")
        self.verify_btn = ctk.CTkButton(
            btn_frame, text="对照 ↗", width=84, height=24,
            font=ctk.CTkFont(size=11),
            command=self._open_verify, state="disabled",
            fg_color=PALETTE["accent_dim"],
            hover_color=PALETTE["accent"],
            text_color="#000")
        self.verify_btn.pack(pady=(0, 4))
        self.detail_btn = ctk.CTkButton(
            btn_frame, text="详情", width=84, height=24,
            font=ctk.CTkFont(size=11),
            command=self._show_detail, state="disabled",
            fg_color=PALETTE["panel_hi"],
            hover_color=PALETTE["border"])
        self.detail_btn.pack()

    def set_result(self, res: dict) -> None:
        self.detail_data = res
        self.verify_url = res.get("verify_url", "") or ""
        self.request_url = res.get("request_url", "") or ""
        status = res.get("status", "error")
        summary = res.get("summary", "")
        if res.get("error"):
            summary = f"{summary} — {res['error']}" if summary else res["error"]
        self.icon_label.configure(
            text=STATUS_ICONS.get(status, "·"),
            text_color=STATUS_COLOR.get(status, PALETTE["muted"]))
        self.summary_label.configure(text=summary,
                                     text_color=PALETTE["text"])
        self.detail_btn.configure(state="normal")
        if self.verify_url:
            self.verify_btn.configure(state="normal")

    def _open_verify(self):
        if self.verify_url:
            webbrowser.open(self.verify_url)

    def _show_detail(self):
        if self.detail_data is None:
            return
        DetailWindow(self, self.title, self.detail_data)


class DetailWindow(ctk.CTkToplevel):
    def __init__(self, parent, title: str, res: dict):
        super().__init__(parent)
        self.title(f"{title} — 详情")
        self.geometry("760x600")
        self.configure(fg_color=PALETTE["bg"])
        self.transient(parent)
        try:
            self.grab_set()
        except Exception:
            pass
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        url_frame = ctk.CTkFrame(self, corner_radius=8,
                                 fg_color=PALETTE["panel"],
                                 border_color=PALETTE["border"],
                                 border_width=1)
        url_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        url_frame.grid_columnconfigure(1, weight=1)
        if res.get("request_url"):
            ctk.CTkLabel(url_frame, text="请求 URL", anchor="w",
                         text_color=PALETTE["accent"],
                         font=ctk.CTkFont(size=11, weight="bold")).grid(
                row=0, column=0, sticky="w", padx=(12, 4), pady=(10, 2))
            ctk.CTkLabel(url_frame, text=res["request_url"], anchor="w",
                         font=ctk.CTkFont(family="Menlo", size=11),
                         text_color=PALETTE["muted"], wraplength=620,
                         justify="left").grid(
                row=0, column=1, sticky="ew", padx=4, pady=(10, 2))
        if res.get("verify_url"):
            ctk.CTkLabel(url_frame, text="网站对照", anchor="w",
                         text_color=PALETTE["ok"],
                         font=ctk.CTkFont(size=11, weight="bold")).grid(
                row=1, column=0, sticky="w", padx=(12, 4), pady=(2, 10))
            link = ctk.CTkLabel(
                url_frame, text=res["verify_url"], anchor="w",
                font=ctk.CTkFont(family="Menlo", size=11, underline=True),
                text_color=PALETTE["accent"],
                cursor="hand2", wraplength=620, justify="left")
            link.grid(row=1, column=1, sticky="ew", padx=4, pady=(2, 10))
            link.bind("<Button-1>",
                      lambda _e, u=res["verify_url"]: webbrowser.open(u))

        ctk.CTkLabel(
            self,
            text=(f"状态: {STATUS_ICONS.get(res.get('status'),'·')} "
                  f"{res.get('status','?')}    摘要: {res.get('summary','')}"),
            anchor="w", text_color=PALETTE["text"],
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 4))

        # Highlights table — populated when the checker stashed a
        # `highlights` dict in res["data"]. Renders as a clean two-column
        # key/value layout above the raw JSON dump.
        data = res.get("data") or {}
        highlights = data.get("highlights") if isinstance(data, dict) else None

        text = ctk.CTkTextbox(self, wrap="word",
                              fg_color=PALETTE["panel"],
                              border_color=PALETTE["border"],
                              border_width=1,
                              text_color=PALETTE["text"],
                              font=ctk.CTkFont(family="Menlo", size=12))
        text.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 12))
        body = ""
        if isinstance(highlights, dict) and highlights:
            body += "── 关键字段 ──\n"
            # Pad keys for tidy alignment (display width)
            try:
                key_w = max(self._display_width(str(k)) for k in highlights)
            except Exception:
                key_w = 16
            for k, v in highlights.items():
                if v in (None, "", "—", "?", "AS"):
                    continue
                pad = " " * max(1, key_w - self._display_width(str(k)))
                body += f"  {k}{pad}  : {v}\n"
            body += "\n"
        body += "── 原始数据 ──\n"
        try:
            body += json.dumps(data, indent=2, ensure_ascii=False)
        except Exception:
            body += str(data)
        text.insert("1.0", body)
        text.configure(state="disabled")

    @staticmethod
    def _display_width(s: str) -> int:
        """Approximate display columns — CJK / fullwidth chars count as 2."""
        w = 0
        for ch in s:
            o = ord(ch)
            if o >= 0x1100 and (
                0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0x9FFF or
                0xA000 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or
                0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or
                0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6):
                w += 2
            else:
                w += 1
        return w


class ResultGroup(ctk.CTkFrame):
    def __init__(self, master, title: str, subtitle: str = "", **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=f"▎ {title}", anchor="w",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="ew", padx=4, pady=(8, 0))
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, anchor="w",
                         font=ctk.CTkFont(size=11),
                         text_color=PALETTE["muted"],
                         wraplength=900, justify="left").grid(
                row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)
        self._cards: dict[str, ResultCard] = {}

    def add(self, key: str, title: str) -> ResultCard:
        if key in self._cards:
            return self._cards[key]
        card = ResultCard(self.body, title)
        card.grid(row=len(self._cards), column=0, sticky="ew",
                  pady=3, padx=2)
        self._cards[key] = card
        return card

    def clear(self):
        for child in list(self.body.winfo_children()):
            child.destroy()
        self._cards.clear()


# ============================================================================
# Claude hero — score ring + 3-column data
# ============================================================================
class ClaudeHero(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, corner_radius=12,
                         fg_color=PALETTE["panel"],
                         border_color=PALETTE["border"], border_width=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=1)

        # left: score ring
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, padx=16, pady=14, sticky="ns")
        ctk.CTkLabel(left, text="Claude 信任评分",
                     text_color=PALETTE["muted"],
                     font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
        self.ring = ScoreRing(left, size=160, ring_w=10)
        self.ring.pack(pady=4)
        self.score_sub = ctk.CTkLabel(
            left, text="等待检测",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=11))
        self.score_sub.pack(anchor="w")

        # mid: egress IPs
        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=0, column=1, padx=14, pady=14, sticky="nsew")
        ctk.CTkLabel(mid, text="出口 IP 多视角",
                     text_color=PALETTE["muted"],
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
            anchor="w", pady=(0, 6))
        self.cf_label = ctk.CTkLabel(
            mid, text="◆ Cloudflare: —", anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Menlo", size=12))
        self.cf_label.pack(anchor="w", pady=2)
        self.claude_label = ctk.CTkLabel(
            mid, text="◆ Claude:     —", anchor="w",
            text_color=PALETTE["accent"],
            font=ctk.CTkFont(family="Menlo", size=12, weight="bold"))
        self.claude_label.pack(anchor="w", pady=2)
        self.cn_label = ctk.CTkLabel(
            mid, text="◆ 本机 IPv4:  —", anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Menlo", size=12))
        self.cn_label.pack(anchor="w", pady=2)

        # right: verdict pills
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, padx=14, pady=14, sticky="nsew")
        ctk.CTkLabel(right, text="Claude 综合判定",
                     text_color=PALETTE["muted"],
                     font=ctk.CTkFont(size=11, weight="bold")).pack(
            anchor="w", pady=(0, 6))
        self.reach_label = ctk.CTkLabel(
            right, text="可达性: —", anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(size=12))
        self.reach_label.pack(anchor="w", pady=2)
        self.status_label = ctk.CTkLabel(
            right, text="服务状态: —", anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(size=12))
        self.status_label.pack(anchor="w", pady=2)
        self.region_label = ctk.CTkLabel(
            right, text="地区状态: —", anchor="w",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(size=12))
        self.region_label.pack(anchor="w", pady=2)

    def reset(self):
        self.ring.set_score(None)
        self.score_sub.configure(text="等待检测")
        self.cf_label.configure(text="◆ Cloudflare: —")
        self.claude_label.configure(text="◆ Claude:     —")
        self.cn_label.configure(text="◆ 本机 IPv4:  —")
        self.reach_label.configure(text="可达性: —", text_color=PALETTE["text"])
        self.status_label.configure(text="服务状态: —", text_color=PALETTE["text"])
        self.region_label.configure(text="地区状态: —", text_color=PALETTE["text"])

    def set_iprisk(self, res: dict):
        d = res.get("data") or {}
        score = d.get("trust_score")
        cc = (d.get("countryCode") or "").upper()
        if cc in checkers.RESTRICTED_REGIONS:
            self.ring.set_score(0)
            self.score_sub.configure(
                text=f"⚠ {checkers.RESTRICTED_REGIONS[cc]} 限制区",
                text_color=PALETTE["fail"])
            return
        self.ring.set_score(score)
        flags = [k.replace("is_", "") for k in
                 ("is_vpn", "is_proxy", "is_tor", "is_abuser", "is_crawler")
                 if d.get(k)]
        residence = ""
        if d.get("isResidential") is True:
            residence = "家庭住宅"
        elif d.get("isResidential") is False:
            residence = "机房 IP"
        bits = []
        if residence:
            bits.append(residence)
        if flags:
            bits.append("⚠" + ",".join(flags))
        self.score_sub.configure(text=" · ".join(bits) or "已评分",
                                 text_color=PALETTE["muted"])

    def set_egress(self, res: dict):
        d = res.get("data") or {}
        self.cf_label.configure(
            text=f"◆ Cloudflare: {d.get('cloudflare_egress','—')}")
        claude_ip = d.get("claude_egress", "—")
        loc = d.get("claude_loc", "")
        # truncate IPv6 for display
        disp = claude_ip if len(claude_ip) <= 32 else claude_ip[:30] + "…"
        self.claude_label.configure(text=f"◆ Claude:     {disp} ({loc or '?'})")
        self.cn_label.configure(
            text=f"◆ 本机 IPv4:  {d.get('cn_visible_ipv4','—')}")
        cc = (loc or "").upper()
        if cc in checkers.RESTRICTED_REGIONS:
            self.region_label.configure(
                text=f"地区状态: ✗ {checkers.RESTRICTED_REGIONS[cc]} 不可访问",
                text_color=PALETTE["fail"])
        elif cc:
            self.region_label.configure(
                text=f"地区状态: ✓ {cc} 支持",
                text_color=PALETTE["ok"])

    def set_reach(self, res: dict):
        c = STATUS_COLOR.get(res.get("status"), PALETTE["text"])
        self.reach_label.configure(text=f"可达性: {res.get('summary','—')}",
                                   text_color=c)

    def set_status(self, res: dict):
        c = STATUS_COLOR.get(res.get("status"), PALETTE["text"])
        self.status_label.configure(text=f"服务状态: {res.get('summary','—')}",
                                    text_color=c)


# ============================================================================
# Floating widget — 72×72 rounded square fully filled with the score-band
# color. No black frame, no canvas circle layered on a black background:
# the whole window IS the colored tile. Drag to move · double-click to
# restore main · right-click for menu.
# ============================================================================
class FloatingWidget(ctk.CTkToplevel):
    SIZE = 72            # full size when floating
    SNAP_THICKNESS = 16  # narrow dimension when docked to an edge
    SNAP_DISTANCE = 24   # px from edge that triggers snap on drag-end

    def __init__(self, parent_app: "App"):
        super().__init__(parent_app)
        self.app = parent_app

        # frameless
        self.overrideredirect(True)
        # always on top — set both -topmost and macOS splash style
        self.attributes("-topmost", True)
        try:
            # macOS: this style floats over normal windows even in fullscreen
            # space; on Linux it's a hint to the WM to keep us above panels.
            self.attributes("-type", "splash")
        except Exception:
            pass
        # macOS: try to mark as "auxiliary" so the window joins all spaces
        # (including fullscreen apps' spaces). This requires Tk 8.6+ on macOS.
        try:
            self.tk.call("::tk::unsupported::MacWindowStyle", "style",
                         self._w, "help", "noActivates")
        except Exception:
            pass

        # Initial position + snap state — validated against actual monitor
        # bounds so a widget saved on a now-disconnected secondary monitor
        # doesn't end up off-screen.
        cfg = load_config()
        ui_cfg = cfg.get("ui") or {}
        pos = ui_cfg.get("widget_pos") or ""
        x, y = None, None
        if pos:
            m = re.match(r"\+(-?\d+)\+(-?\d+)", pos)
            if m:
                x, y = int(m.group(1)), int(m.group(2))
        # Validate: is (x+size/2, y+size/2) on any visible monitor?
        if x is not None and y is not None:
            mx, my, mw, mh = monitor_bounds_at(x + self.SIZE // 2,
                                               y + self.SIZE // 2)
            if not (mx <= x + self.SIZE // 2 < mx + mw and
                    my <= y + self.SIZE // 2 < my + mh):
                # saved monitor is gone — fall back to primary top-right
                x = y = None
        if x is None or y is None:
            mx, my, mw, mh = monitor_bounds_at(0, 0)
            x = mx + mw - self.SIZE - 24
            y = my + 80
        self.geometry(f"{self.SIZE}x{self.SIZE}+{x}+{y}")

        self._snapped_edge: str | None = ui_cfg.get("widget_snap") or None
        self._score: int | None = None
        self._color = PALETTE["error"]

        # Flash-animation state
        self._flash_active = False
        self._flash_burst = 0
        self._flash_phase = False
        self._flash_after_id = None

        # Periodic lift() to stay above other windows even when they
        # request topmost. Cheap belt-and-braces in case -topmost loses.
        self._lift_after_id: str | None = None

        self.configure(fg_color=self._color)

        self.tile = ctk.CTkFrame(
            self, corner_radius=18,
            fg_color=self._color,
            border_color=self._color,
            border_width=0)
        self.tile.pack(fill="both", expand=True)

        self.score_label = ctk.CTkLabel(
            self.tile, text="—",
            text_color="#ffffff",
            font=ctk.CTkFont(family="SF Pro Display",
                             size=int(self.SIZE * 0.46),
                             weight="bold"))
        self.score_label.place(relx=0.5, rely=0.5, anchor="center")

        self._bind_events()
        self._start_topmost_loop()

        # If we were snapped on previous run, re-apply the snap geometry
        # after the window is mapped
        if self._snapped_edge:
            self.after(50, lambda: self._apply_snap(self._snapped_edge))

    def _start_topmost_loop(self):
        """Periodically re-assert topmost + raise to stay above fullscreen
        apps and other always-on-top windows on macOS."""
        try:
            self.attributes("-topmost", True)
            self.lift()
        except Exception:
            return
        self._lift_after_id = self.after(1500, self._start_topmost_loop)

    # ─ data ────────────────────────────────────────────────────────────
    def set_score(self, score: int | None, prev: int | None = None):
        """Update score + manage flash state.
        - If score < low_threshold → continuous flashing red
        - If score dropped by >= drop_threshold (compared to `prev`) → 3-burst flash
        """
        self._score = score
        if score is None:
            self._color = PALETTE["error"]
            text = "—"
        else:
            self._color, _ = score_color_label(score)
            text = str(score)
        # When snapped to an edge, the strip is too narrow for digits — only
        # show the colored bar.
        self.score_label.configure(text="" if self._snapped_edge else text)

        cfg = (load_config().get("settings") or {})
        low = int(cfg.get("low_score_threshold", 40))
        drop = int(cfg.get("score_drop_threshold", 20))

        # decide flash mode
        if score is not None and score < low:
            self._flash_active = True
            self._flash_burst = 0
            self._start_flash()
        else:
            self._flash_active = False
            if (score is not None and prev is not None
                    and prev - score >= drop):
                # one-time alert burst (3 blinks)
                self._flash_burst = 6  # 3 on/off pairs
                self._start_flash()
            else:
                self._stop_flash()
                # apply the steady color
                self.configure(fg_color=self._color)
                self.tile.configure(fg_color=self._color)

    def reset(self):
        self.set_score(None)

    # ─ flash animation ─────────────────────────────────────────────────
    def _start_flash(self):
        if self._flash_after_id is not None:
            return  # already running
        self._flash_phase = False
        self._flash_tick()

    def _stop_flash(self):
        if self._flash_after_id is not None:
            try:
                self.after_cancel(self._flash_after_id)
            except Exception:
                pass
        self._flash_after_id = None

    def _flash_tick(self):
        # toggle between alert color and steady color
        self._flash_phase = not self._flash_phase
        c = "#ff1a1a" if self._flash_phase else self._color
        try:
            self.configure(fg_color=c)
            self.tile.configure(fg_color=c)
        except Exception:
            return

        # decide whether to keep going
        if self._flash_active:
            self._flash_after_id = self.after(450, self._flash_tick)
            return
        if self._flash_burst > 0:
            self._flash_burst -= 1
            self._flash_after_id = self.after(220, self._flash_tick)
            return
        # done — settle on steady color
        self._flash_after_id = None
        try:
            self.configure(fg_color=self._color)
            self.tile.configure(fg_color=self._color)
        except Exception:
            pass

    # ─ events ──────────────────────────────────────────────────────────
    def _bind_events(self):
        targets = [self, self.tile, self.score_label]
        for t in targets:
            t.bind("<ButtonPress-1>", self._start_drag)
            t.bind("<B1-Motion>", self._on_drag)
            t.bind("<ButtonRelease-1>", self._end_drag)
            t.bind("<Double-Button-1>", lambda _e: self._restore())
            # right-click: macOS Button-2, others Button-3, also Ctrl+click
            t.bind("<Button-3>", self._show_menu)
            t.bind("<Button-2>", self._show_menu)
            t.bind("<Control-Button-1>", self._show_menu)

    def _start_drag(self, e):
        # If currently snapped to an edge, "pop out" first — expand to full
        # size and re-center on the cursor so the user can drag freely.
        if self._snapped_edge:
            self._unsnap(at_x=e.x_root - self.SIZE // 2,
                         at_y=e.y_root - self.SIZE // 2)
        self._drag_origin = (e.x_root - self.winfo_x(),
                             e.y_root - self.winfo_y())

    def _on_drag(self, e):
        if not getattr(self, "_drag_origin", None):
            return
        dx, dy = self._drag_origin
        self.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _end_drag(self, _e):
        # Use the bounds of THE monitor the widget is currently on, not the
        # primary screen. This is what fixes the multi-monitor "lost widget"
        # bug — snapping to the right edge while on a secondary monitor
        # used to push the widget off-screen of the primary monitor.
        x, y = self.winfo_x(), self.winfo_y()
        cx = x + self.SIZE // 2
        cy = y + self.SIZE // 2
        mx, my, mw, mh = monitor_bounds_at(cx, cy)
        edge: str | None = None
        if x < mx + self.SNAP_DISTANCE:
            edge = "left"
        elif x + self.SIZE > mx + mw - self.SNAP_DISTANCE:
            edge = "right"
        elif y < my + self.SNAP_DISTANCE:
            edge = "top"
        elif y + self.SIZE > my + mh - self.SNAP_DISTANCE:
            edge = "bottom"
        if edge:
            self._apply_snap(edge, mx, my, mw, mh)
        else:
            self._snapped_edge = None
            self.score_label.configure(
                text="—" if self._score is None else str(self._score))
            try:
                cfg = load_config()
                cfg.setdefault("ui", {})
                cfg["ui"]["widget_pos"] = f"+{x}+{y}"
                cfg["ui"]["widget_snap"] = ""
                save_config(cfg)
            except Exception:
                pass

    # ─ snap / unsnap geometry helpers ─────────────────────────────────
    def _apply_snap(self, edge: str,
                    mx: int | None = None, my: int | None = None,
                    mw: int | None = None, mh: int | None = None):
        """Resize + reposition into a thin strip flush to the named edge of
        the monitor the widget is currently on."""
        self._snapped_edge = edge
        x, y = self.winfo_x(), self.winfo_y()
        if mx is None:
            cx, cy = x + self.SIZE // 2, y + self.SIZE // 2
            mx, my, mw, mh = monitor_bounds_at(cx, cy)
        t = self.SNAP_THICKNESS
        if edge == "left":
            geo = f"{t}x{self.SIZE}+{mx}+{y}"
            new_x, new_y = mx, y
        elif edge == "right":
            geo = f"{t}x{self.SIZE}+{mx + mw - t}+{y}"
            new_x, new_y = mx + mw - t, y
        elif edge == "top":
            geo = f"{self.SIZE}x{t}+{x}+{my}"
            new_x, new_y = x, my
        else:  # bottom
            geo = f"{self.SIZE}x{t}+{x}+{my + mh - t}"
            new_x, new_y = x, my + mh - t
        self.geometry(geo)
        self.score_label.configure(text="")
        try:
            cfg = load_config()
            cfg.setdefault("ui", {})
            cfg["ui"]["widget_pos"] = f"+{new_x}+{new_y}"
            cfg["ui"]["widget_snap"] = edge
            save_config(cfg)
        except Exception:
            pass

    def _unsnap(self, at_x: int | None = None, at_y: int | None = None):
        """Restore full SIZE×SIZE square; place at given point if provided."""
        self._snapped_edge = None
        if at_x is None or at_y is None:
            at_x = self.winfo_x()
            at_y = self.winfo_y()
        # clamp to the monitor we're on
        mx, my, mw, mh = monitor_bounds_at(
            at_x + self.SIZE // 2, at_y + self.SIZE // 2)
        at_x = max(mx, min(at_x, mx + mw - self.SIZE))
        at_y = max(my, min(at_y, my + mh - self.SIZE))
        self.geometry(f"{self.SIZE}x{self.SIZE}+{at_x}+{at_y}")
        if self._score is not None:
            self.score_label.configure(text=str(self._score))
        else:
            self.score_label.configure(text="—")

    def _show_menu(self, e):
        m = tk.Menu(self, tearoff=0,
                    bg=PALETTE["panel"], fg=PALETTE["text"],
                    activebackground=PALETTE["accent"],
                    activeforeground=PALETTE["bg"])
        m.add_command(label="刷新检测", command=self.app.start_check)
        m.add_command(label="设置 …", command=self._open_settings_here)
        m.add_command(label="复位浮窗位置", command=self.app.reset_widget_position)
        m.add_command(label="展开主窗口", command=self._restore)
        m.add_separator()
        m.add_command(label="退出", command=self.app.destroy)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _open_settings_here(self):
        """Open the settings dialog without leaving widget mode."""
        SettingsWindow(self.app)

    def _restore(self):
        self.app.show_main_from_widget()

    # legacy no-op API for orchestrator
    def set_claude_status(self, *_a, **_kw): pass
    def set_egress(self, *_a, **_kw): pass
    def set_latency(self, *_a, **_kw): pass


# ============================================================================
# Settings dialog
# ============================================================================
class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("设置")
        self.geometry("620x720")
        self.configure(fg_color=PALETTE["bg"])
        self.transient(parent)
        try:
            self.grab_set()
        except Exception:
            pass
        self.cfg = load_config()
        self.entries: dict[str, ctk.CTkEntry] = {}

        # ── Pin the save/cancel bar to the bottom FIRST so it always
        # stays visible no matter how tall the body grows. Then put all
        # the configurable sections inside a scrollable body above it. ──
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(side="bottom", fill="x", padx=16, pady=12)
        ctk.CTkButton(bf, text="保存", command=self._save,
                      fg_color=PALETTE["accent_dim"],
                      hover_color=PALETTE["accent"],
                      text_color="#000",
                      width=120, height=36,
                      font=ctk.CTkFont(size=13, weight="bold")
                      ).pack(side="right", padx=4)
        ctk.CTkButton(bf, text="取消", command=self.destroy,
                      fg_color=PALETTE["panel_hi"],
                      hover_color=PALETTE["border"],
                      width=100, height=36).pack(side="right", padx=4)

        body = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=PALETTE["border"],
            scrollbar_button_hover_color=PALETTE["accent_dim"])
        body.pack(side="top", fill="both", expand=True,
                  padx=4, pady=(8, 0))

        ctk.CTkLabel(body,
                     text="API Keys（可选 — 留空使用免费层 / 公开页面解析）",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=16, pady=(16, 8))

        keys_info = [
            ("ipinfo", "IPinfo Token", "https://ipinfo.io/signup（免费 50k/月）"),
            ("ipqualityscore", "IPQualityScore Key",
             "https://www.ipqualityscore.com/create-account（免费 5000/月）"),
            ("abuseipdb", "AbuseIPDB Key",
             "https://www.abuseipdb.com/register（免费 1000/天）"),
            ("iphub", "IPHub X-Key",
             "https://iphub.info/register（免费 1000/天）"),
            ("ip2location", "IP2Location Key",
             "https://www.ip2location.io/sign-up（免费 30k/月）"),
        ]
        api_frame = ctk.CTkFrame(body, fg_color=PALETTE["panel"],
                                 border_color=PALETTE["border"],
                                 border_width=1)
        api_frame.pack(fill="x", padx=16, pady=8)
        api_frame.grid_columnconfigure(1, weight=1)
        for i, (k, label, hint) in enumerate(keys_info):
            ctk.CTkLabel(api_frame, text=label, anchor="w",
                         text_color=PALETTE["text"]).grid(
                row=i*2, column=0, sticky="w", padx=8, pady=(8, 2))
            e = ctk.CTkEntry(api_frame, show="•", width=320)
            e.insert(0, self.cfg.get("api_keys", {}).get(k, ""))
            e.grid(row=i*2, column=1, sticky="ew", padx=8, pady=(8, 2))
            self.entries[k] = e
            link = ctk.CTkLabel(
                api_frame, text=hint, font=ctk.CTkFont(size=10),
                text_color=PALETTE["accent"],
                cursor="hand2", anchor="w")
            link.grid(row=i*2+1, column=0, columnspan=2, sticky="w",
                      padx=8, pady=(0, 4))
            link.bind("<Button-1>",
                      lambda _e, h=hint: webbrowser.open(h.split("（")[0]))

        ctk.CTkLabel(body, text="超时与并发",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=16, pady=(12, 4))
        opt_frame = ctk.CTkFrame(body, fg_color=PALETTE["panel"],
                                 border_color=PALETTE["border"],
                                 border_width=1)
        opt_frame.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(opt_frame, text="请求超时（秒）",
                     text_color=PALETTE["text"]).grid(
            row=0, column=0, padx=8, pady=8, sticky="w")
        self.timeout_entry = ctk.CTkEntry(opt_frame, width=80)
        self.timeout_entry.insert(0,
            str(self.cfg.get("settings", {}).get("request_timeout", 10)))
        self.timeout_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")
        ctk.CTkLabel(opt_frame, text="并发数",
                     text_color=PALETTE["text"]).grid(
            row=1, column=0, padx=8, pady=8, sticky="w")
        self.workers_entry = ctk.CTkEntry(opt_frame, width=80)
        self.workers_entry.insert(0,
            str(self.cfg.get("settings", {}).get("max_workers", 12)))
        self.workers_entry.grid(row=1, column=1, padx=8, pady=8, sticky="w")

        # ── auto-refresh + monitoring ──
        ctk.CTkLabel(body, text="自动监测",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=16, pady=(12, 4))
        mon_frame = ctk.CTkFrame(body, fg_color=PALETTE["panel"],
                                 border_color=PALETTE["border"],
                                 border_width=1)
        mon_frame.pack(fill="x", padx=16, pady=8)
        mon_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(mon_frame, text="定时刷新（秒，0=关闭）",
                     text_color=PALETTE["text"]).grid(
            row=0, column=0, padx=8, pady=8, sticky="w")
        self.refresh_entry = ctk.CTkEntry(mon_frame, width=80)
        self.refresh_entry.insert(0,
            str(self.cfg.get("settings", {}).get("auto_refresh_seconds", 120)))
        self.refresh_entry.grid(row=0, column=1, padx=8, pady=8, sticky="w")

        self.netwatch_var = tk.BooleanVar(
            value=bool(self.cfg.get("settings", {}).get(
                "network_change_detection", True)))
        ctk.CTkCheckBox(mon_frame,
                        text="检测到 IP / 网关变化时立即重检",
                        variable=self.netwatch_var,
                        text_color=PALETTE["text"]).grid(
            row=1, column=0, columnspan=2, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(mon_frame, text="网络轮询间隔（秒）",
                     text_color=PALETTE["text"]).grid(
            row=2, column=0, padx=8, pady=8, sticky="w")
        self.poll_entry = ctk.CTkEntry(mon_frame, width=80)
        self.poll_entry.insert(0,
            str(self.cfg.get("settings", {}).get("network_poll_seconds", 5)))
        self.poll_entry.grid(row=2, column=1, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(mon_frame, text="低分阈值 (浮窗持续闪烁)",
                     text_color=PALETTE["text"]).grid(
            row=3, column=0, padx=8, pady=8, sticky="w")
        self.low_entry = ctk.CTkEntry(mon_frame, width=80)
        self.low_entry.insert(0,
            str(self.cfg.get("settings", {}).get("low_score_threshold", 40)))
        self.low_entry.grid(row=3, column=1, padx=8, pady=8, sticky="w")

        ctk.CTkLabel(mon_frame, text="评分骤降阈值 (短暂闪烁)",
                     text_color=PALETTE["text"]).grid(
            row=4, column=0, padx=8, pady=8, sticky="w")
        self.drop_entry = ctk.CTkEntry(mon_frame, width=80)
        self.drop_entry.insert(0,
            str(self.cfg.get("settings", {}).get("score_drop_threshold", 20)))
        self.drop_entry.grid(row=4, column=1, padx=8, pady=8, sticky="w")

        # ── 风控 / 系统行为 ──
        ctk.CTkLabel(body, text="风控与系统行为",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=16, pady=(12, 4))
        sys_frame = ctk.CTkFrame(body, fg_color=PALETTE["panel"],
                                 border_color=PALETTE["border"],
                                 border_width=1)
        sys_frame.pack(fill="x", padx=16, pady=8)
        sys_frame.grid_columnconfigure(0, weight=1)

        cap_val = checkers.CN_SCORE_CAP
        # honour both new and legacy key for one release
        cn_cap_enabled = self.cfg.get("settings", {}).get(
            "force_cn_score_cap",
            self.cfg.get("settings", {}).get("force_cn_cap_20", True))
        self.cn_cap_var = tk.BooleanVar(value=bool(cn_cap_enabled))
        ctk.CTkCheckBox(sys_frame,
                        text=f"出口在中国大陆 → 综合评分强制封顶 {cap_val} (最高优先级)",
                        variable=self.cn_cap_var,
                        text_color=PALETTE["text"]).grid(
            row=0, column=0, padx=8, pady=(10, 4), sticky="w")

        self.kill_claude_var = tk.BooleanVar(
            value=bool(self.cfg.get("settings", {}).get(
                "kill_claude_on_low_score", True)))
        ctk.CTkCheckBox(sys_frame,
                        text="评分低于阈值 → 强制结束所有 Claude / Anthropic 相关进程与连接",
                        variable=self.kill_claude_var,
                        text_color=PALETTE["text"]).grid(
            row=1, column=0, padx=8, pady=4, sticky="w")
        ctk.CTkLabel(
            sys_frame,
            text=("    策略：宁可杀错不可放过。扫描三类目标 ——"
                  "\n    (a) 进程名 / 命令行含 “claude” 或 “anthropic”"
                  "\n    (b) 持有 socket 连接到 claude.ai / api.anthropic.com / "
                  "anthropic.com / console.anthropic.com / claude.com"
                  "\n    (c) 自动跳过本程序自身。阈值取上面的“低分阈值”，可动态调整。"),
            text_color=PALETTE["muted"], justify="left",
            font=ctk.CTkFont(size=10)).grid(
            row=2, column=0, padx=8, pady=(0, 6), sticky="w")

        kill_now_btn = ctk.CTkButton(
            sys_frame, text="立即扫描并结束 Claude 进程",
            fg_color=PALETTE["panel_hi"],
            hover_color=PALETTE["fail"],
            command=self._kill_claude_now)
        kill_now_btn.grid(row=3, column=0, padx=8, pady=(0, 10), sticky="w")

        self.autostart_var = tk.BooleanVar(
            value=system_actions.is_autostart_enabled())
        ctk.CTkCheckBox(sys_frame,
                        text=f"开机自动启动（{platform.system()}）",
                        variable=self.autostart_var,
                        text_color=PALETTE["text"]).grid(
            row=4, column=0, padx=8, pady=(8, 4), sticky="w")
        hint_path = ""
        sysname = platform.system()
        if sysname == "Darwin":
            hint_path = "~/Library/LaunchAgents/com.tom.ipqualitychecker.plist"
        elif sysname == "Windows":
            hint_path = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
        else:
            hint_path = "~/.config/autostart/IPQualityChecker.desktop"
        ctk.CTkLabel(sys_frame, text=f"    将写入: {hint_path}",
                     text_color=PALETTE["muted"],
                     font=ctk.CTkFont(size=10)).grid(
            row=5, column=0, padx=8, pady=(0, 10), sticky="w")

    def _save(self):
        cfg = self.cfg
        cfg.setdefault("api_keys", {})
        for k, e in self.entries.items():
            cfg["api_keys"][k] = e.get().strip()
        cfg.setdefault("settings", {})
        try:
            cfg["settings"]["request_timeout"] = max(1, int(self.timeout_entry.get()))
        except Exception:
            cfg["settings"]["request_timeout"] = 10
        try:
            cfg["settings"]["max_workers"] = max(1, min(64, int(self.workers_entry.get())))
        except Exception:
            cfg["settings"]["max_workers"] = 12
        try:
            cfg["settings"]["auto_refresh_seconds"] = max(0, int(self.refresh_entry.get()))
        except Exception:
            cfg["settings"]["auto_refresh_seconds"] = 120
        cfg["settings"]["network_change_detection"] = bool(self.netwatch_var.get())
        try:
            cfg["settings"]["network_poll_seconds"] = max(2, int(self.poll_entry.get()))
        except Exception:
            cfg["settings"]["network_poll_seconds"] = 5
        try:
            cfg["settings"]["low_score_threshold"] = max(0, min(100, int(self.low_entry.get())))
        except Exception:
            cfg["settings"]["low_score_threshold"] = 40
        try:
            cfg["settings"]["score_drop_threshold"] = max(1, int(self.drop_entry.get()))
        except Exception:
            cfg["settings"]["score_drop_threshold"] = 20
        cfg["settings"]["force_cn_score_cap"] = bool(self.cn_cap_var.get())
        # legacy key — keep mirrored for one release for back-compat
        cfg["settings"].pop("force_cn_cap_20", None)
        cfg["settings"]["kill_claude_on_low_score"] = bool(self.kill_claude_var.get())
        cfg["settings"]["auto_start_on_boot"] = bool(self.autostart_var.get())
        save_config(cfg)
        # Apply the autostart toggle now (creates/removes plist or registry).
        autostart_msg = ""
        try:
            ok, msg = system_actions.set_autostart(
                bool(self.autostart_var.get()))
            autostart_msg = ("\n开机自启: " + msg) if msg else ""
            if not ok:
                autostart_msg = "\n⚠ 开机自启失败: " + msg
        except Exception as e:
            autostart_msg = f"\n⚠ 开机自启异常: {e}"
        # Re-arm the parent App's timers using the new settings
        try:
            parent = self.master
            if hasattr(parent, "_schedule_auto_refresh"):
                parent._schedule_auto_refresh()
        except Exception:
            pass
        messagebox.showinfo("已保存", "设置已保存到本地配置文件" + autostart_msg)
        self.destroy()

    def _kill_claude_now(self):
        """Manual button — show preview list, ask, then kill."""
        try:
            procs = system_actions.list_claude_processes()
        except Exception as e:
            messagebox.showerror("扫描失败", str(e))
            return
        if not procs:
            messagebox.showinfo("结果", "未发现包含 “claude” 的进程")
            return
        preview = "\n".join(f"PID {pid}: {label}" for pid, label in procs[:12])
        if len(procs) > 12:
            preview += f"\n…还有 {len(procs) - 12} 个"
        if not messagebox.askyesno(
                "确认强制结束以下进程？",
                f"找到 {len(procs)} 个进程：\n\n{preview}\n\n确定全部结束？"):
            return
        ok, msg, items = system_actions.kill_claude_processes()
        head = "完成" if ok else "部分失败"
        body = msg + ("\n\n" + "\n".join(items[:20]) if items else "")
        (messagebox.showinfo if ok else messagebox.showwarning)(head, body)


# ============================================================================
# Main App
# ============================================================================
class App(ctk.CTk):
    GROUP_MAP = {
        # Claude-focused IP/risk views live with the AI unlock cards now
        "egress_ips": "ai_claude", "iprisk": "ai_claude",
        "claude_reach": "ai_claude", "claude_status": "ai_claude",

        "ipinfo": "ip", "ip-api": "ip", "ipapi.is": "ip",
        "ip2location": "ip", "dbip": "ip",

        "scamalytics": "risk", "ipqs": "risk", "abuseipdb": "risk",
        "iphub": "risk", "ping0": "risk",

        "claude": "ai_unlock", "chatgpt": "ai_unlock", "gemini": "ai_unlock",

        "netflix": "streaming", "disney": "streaming",
        "youtube_premium": "streaming", "tiktok": "streaming",
        "spotify": "streaming",

        "speed": "speed", "traceroute": "speed",
        "dns_leak": "dns", "dns_resolvers": "dns",
    }

    def __init__(self):
        super().__init__()
        # Hide while we build — we put a Toplevel splash up in front and
        # reveal ourselves only once the UI tree is finished. Splash uses
        # the same Tk root (self) — never call tk.Tk() twice, that creates
        # two interpreters and the second root's events stop pumping on
        # macOS (= "页面卡死" symptom).
        self.withdraw()
        self._splash = self._make_splash()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.minsize(1000, 700)
        self.configure(fg_color=PALETTE["bg"])

        # Set window icon (uses bundled icon.png — works on Win/Linux; on
        # macOS the .icns set by PyInstaller is what shows in Dock/Finder).
        try:
            png_path = resource_path("icon.png")
            if os.path.exists(png_path):
                self._icon_photo = tk.PhotoImage(file=png_path)
                self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        # Restore saved window geometry, fall back to default
        cfg = load_config()
        ui = cfg.get("ui") or {}
        saved_geo = ui.get("main_geometry") or ""
        if saved_geo:
            try:
                self.geometry(saved_geo)
            except Exception:
                self.geometry("1240x900")
        else:
            self.geometry("1240x900")

        self.queue: queue.Queue = queue.Queue()
        self.results: list[dict] = []
        self.running = False
        self.current_ip = ""
        self.widget: FloatingWidget | None = None
        self.last_score: int | None = None

        self._set_splash_status("正在构建界面…")
        self._build_ui()
        self._set_splash_status("即将就绪…")
        self.after(100, self._drain_queue)

        # Persist geometry + mode on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Restore floating-widget mode if user was in it last session;
        # otherwise just show the (already-built) main window. This is
        # the only place that deiconifies after __init__'s self.withdraw().
        if ui.get("mode") == "widget":
            self.after(200, self.show_widget)
        else:
            self.after(0, self._reveal_main)

        # ── periodic auto-refresh ──
        self._refresh_after_id: str | None = None

        # ── network-change monitor ──
        self._net_signature = self._capture_net_signature()
        self._net_after_id: str | None = None
        self._start_network_monitor()

        # Auto-run a detection on launch (configurable)
        if cfg.get("settings", {}).get("auto_check_on_launch", True):
            self.after(600, self.start_check)

    def _make_splash(self) -> tk.Toplevel:
        """Create a borderless splash window on top of *this* App's Tk root.
        Must NOT be a separate tk.Tk() — see comment in __init__."""
        s = tk.Toplevel(self)
        s.overrideredirect(True)
        sw, sh = s.winfo_screenwidth(), s.winfo_screenheight()
        w, h = 360, 180
        s.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")
        s.configure(bg="#0a0e1a")
        try:
            s.attributes("-topmost", True)
            s.attributes("-alpha", 0.97)
        except Exception:
            pass
        tk.Label(s, text="⌬  IPQualityChecker",
                 fg="#00d4ff", bg="#0a0e1a",
                 font=("SF Pro Display", 18, "bold")).pack(pady=(28, 4))
        tk.Label(s, text=f"v{APP_VERSION}",
                 fg="#8b95a8", bg="#0a0e1a",
                 font=("Menlo", 10)).pack()
        from tkinter import ttk
        style = ttk.Style(s)
        try:
            style.theme_use("clam")
            style.configure("Splash.Horizontal.TProgressbar",
                            troughcolor="#181f33", background="#00d4ff",
                            bordercolor="#181f33",
                            lightcolor="#00d4ff", darkcolor="#0fa8cc")
        except Exception:
            pass
        pb = ttk.Progressbar(s, mode="indeterminate", length=280,
                             style="Splash.Horizontal.TProgressbar")
        pb.pack(pady=14)
        pb.start(12)
        s._status = tk.Label(s, text="正在初始化…",
                             fg="#e6edf3", bg="#0a0e1a",
                             font=("SF Pro Display", 11))
        s._status.pack()
        # Force first paint NOW so the user sees us
        s.update_idletasks()
        s.update()
        return s

    def _set_splash_status(self, text: str) -> None:
        try:
            if self._splash and self._splash.winfo_exists():
                self._splash._status.configure(text=text)
                self._splash.update_idletasks()
        except Exception:
            pass

    def _reveal_main(self):
        """Deiconify + lift the main window after init. Called from after()
        so the UI tree has a tick to settle before being mapped."""
        try:
            if self._splash and self._splash.winfo_exists():
                self._splash.destroy()
            self._splash = None
        except Exception:
            pass
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _on_close(self):
        try:
            cfg = load_config()
            cfg.setdefault("ui", {})
            in_widget = (self.widget is not None
                         and self.widget.winfo_exists()
                         and self.widget.state() != "withdrawn")
            cfg["ui"]["mode"] = "widget" if in_widget else "main"
            # Save main window geometry only if main is currently visible
            if self.state() != "withdrawn":
                cfg["ui"]["main_geometry"] = self.geometry()
            if self.widget is not None and self.widget.winfo_exists():
                cfg["ui"]["widget_pos"] = (
                    f"+{self.widget.winfo_x()}+{self.widget.winfo_y()}")
            save_config(cfg)
        except Exception:
            pass
        self.destroy()

    # ── ui ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ── Top bar ──
        top = ctk.CTkFrame(self, corner_radius=0, height=72,
                           fg_color=PALETTE["panel"])
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(top, text=f"⌬  {APP_NAME}",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=20, weight="bold")).grid(
            row=0, column=0, padx=18, pady=14, sticky="w")
        ctk.CTkLabel(top, text=f"v{APP_VERSION} · {platform.system()}",
                     text_color=PALETTE["muted"],
                     font=ctk.CTkFont(family="Menlo", size=11)).grid(
            row=0, column=1, padx=4, pady=14, sticky="w")
        self.run_btn = ctk.CTkButton(
            top, text="▸ 开始全面检测", width=150, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=PALETTE["accent_dim"],
            hover_color=PALETTE["accent"],
            text_color="#000",
            command=self.start_check)
        self.run_btn.grid(row=0, column=3, padx=8, pady=14)
        ctk.CTkButton(top, text="⊟ 缩为浮窗", width=110, height=36,
                      fg_color=PALETTE["panel_hi"],
                      hover_color=PALETTE["border"],
                      command=self.show_widget).grid(
            row=0, column=4, padx=4, pady=14)
        ctk.CTkButton(top, text="↺ 复位浮窗", width=110, height=36,
                      fg_color=PALETTE["panel_hi"],
                      hover_color=PALETTE["border"],
                      command=self.reset_widget_position).grid(
            row=0, column=5, padx=4, pady=14)
        ctk.CTkButton(top, text="导出", width=72, height=36,
                      fg_color=PALETTE["panel_hi"],
                      hover_color=PALETTE["border"],
                      command=self.export_report).grid(
            row=0, column=6, padx=4, pady=14)
        ctk.CTkButton(top, text="设置", width=72, height=36,
                      fg_color=PALETTE["panel_hi"],
                      hover_color=PALETTE["border"],
                      command=self.open_settings).grid(
            row=0, column=7, padx=(4, 18), pady=14)

        # ── IP/score banner ──
        info = ctk.CTkFrame(self, corner_radius=0, height=78,
                            fg_color=PALETTE["bg"])
        info.grid(row=1, column=0, sticky="ew", pady=(1, 0))
        info.grid_columnconfigure(0, weight=1)
        info.grid_columnconfigure(1, weight=1)
        info.grid_columnconfigure(2, weight=1)
        self.ip_label = ctk.CTkLabel(
            info, text="IP: —", anchor="w",
            text_color=PALETTE["accent"],
            font=ctk.CTkFont(family="Menlo", size=14, weight="bold"))
        self.ip_label.grid(row=0, column=0, padx=18, pady=(10, 0), sticky="w")
        self.geo_label = ctk.CTkLabel(
            info, text="位置: —", anchor="w",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=12))
        self.geo_label.grid(row=1, column=0, padx=18, pady=(0, 10), sticky="w")
        self.score_label = ctk.CTkLabel(
            info, text="综合评分  —", anchor="center",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=18, weight="bold"))
        self.score_label.grid(row=0, column=1, rowspan=2, padx=8, pady=8)
        self.progress = ctk.CTkProgressBar(
            info, mode="determinate",
            progress_color=PALETTE["accent"])
        self.progress.set(0)
        self.progress.grid(row=0, column=2, rowspan=2,
                           padx=18, pady=24, sticky="ew")

        # ── Tabs ──
        self.tabs = ctk.CTkTabview(
            self, anchor="nw",
            fg_color=PALETTE["bg"],
            segmented_button_fg_color=PALETTE["panel"],
            segmented_button_selected_color=PALETTE["accent_dim"],
            segmented_button_selected_hover_color=PALETTE["accent"],
            segmented_button_unselected_color=PALETTE["panel"],
            segmented_button_unselected_hover_color=PALETTE["panel_hi"],
            text_color=PALETTE["text"])
        self.tabs.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 12))
        for name in ("概览", "AI 服务", "IP 信息", "风险评分",
                     "流媒体", "网站可达", "网络质量", "日志"):
            self.tabs.add(name)
        self.tab_overview  = self.tabs.tab("概览")
        self.tab_ai        = self.tabs.tab("AI 服务")
        self.tab_ip        = self.tabs.tab("IP 信息")
        self.tab_risk      = self.tabs.tab("风险评分")
        self.tab_streaming = self.tabs.tab("流媒体")
        self.tab_sites     = self.tabs.tab("网站可达")
        self.tab_net       = self.tabs.tab("网络质量")
        self.tab_log       = self.tabs.tab("日志")
        self.tabs.set("概览")

        self._build_overview_tab()
        self._build_ai_tab()
        self._build_ip_tab()
        self._build_risk_tab()
        self._build_streaming_tab()
        self._build_sites_tab()
        self._build_net_tab()
        self._build_log_tab()

    def _scroll(self, parent) -> ctk.CTkScrollableFrame:
        f = ctk.CTkScrollableFrame(parent, fg_color="transparent",
                                   scrollbar_button_color=PALETTE["border"],
                                   scrollbar_button_hover_color=PALETTE["accent_dim"])
        f.pack(fill="both", expand=True)
        return f

    def _build_overview_tab(self):
        """True dashboard — no card duplicates. Shows the highest-signal
        verdicts the user wants to see at a glance: 综合分, 出口国家, 时区,
        ASN/ISP, plus any current 风险警示 as one-liners."""
        s = self._scroll(self.tab_overview)

        # ── Big verdict card (overall score + country + timezone) ──
        self.overall_card = ctk.CTkFrame(
            s, corner_radius=12, fg_color=PALETTE["panel"],
            border_color=PALETTE["border"], border_width=1)
        self.overall_card.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(self.overall_card, text="▎ 综合检测结果",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self.overall_text = ctk.CTkLabel(
            self.overall_card,
            text="点击 \"开始全面检测\" 启动诊断",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=12), justify="left", anchor="w")
        self.overall_text.pack(anchor="w", padx=12, pady=(0, 12))

        # ── Egress / Geo / Timezone / ASN — the "where am I" fact strip ──
        self.facts_card = ctk.CTkFrame(
            s, corner_radius=12, fg_color=PALETTE["panel"],
            border_color=PALETTE["border"], border_width=1)
        self.facts_card.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(self.facts_card, text="▎ 出口画像",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self.facts_grid = ctk.CTkFrame(self.facts_card, fg_color="transparent")
        self.facts_grid.pack(fill="x", padx=12, pady=(0, 12))
        self.facts_grid.grid_columnconfigure(1, weight=1)
        self.facts_grid.grid_columnconfigure(3, weight=1)
        self._fact_rows = {}
        fact_labels = [
            ("country",  "出口国家",  "—"),
            ("timezone", "时区",      "—"),
            ("asn",      "ASN/ISP",   "—"),
            ("ip_v4",    "公共 IPv4", "—"),
            ("ip_v6",    "Claude 视角 IP", "—"),
            ("dns",      "出口 DNS",  "—"),
        ]
        for i, (k, lbl, val) in enumerate(fact_labels):
            r, c = divmod(i, 2)
            ctk.CTkLabel(self.facts_grid, text=lbl + ":",
                         anchor="e",
                         text_color=PALETTE["muted"],
                         font=ctk.CTkFont(size=11)).grid(
                row=r, column=c * 2, padx=(8, 6), pady=4, sticky="e")
            v = ctk.CTkLabel(self.facts_grid, text=val, anchor="w",
                             text_color=PALETTE["text"],
                             font=ctk.CTkFont(family="Menlo", size=12,
                                              weight="bold"))
            v.grid(row=r, column=c * 2 + 1, padx=(0, 16), pady=4, sticky="w")
            self._fact_rows[k] = v

        # ── 警示摘要 — only fail/warn shown as compact one-liners ──
        self.alerts_card = ctk.CTkFrame(
            s, corner_radius=12, fg_color=PALETTE["panel"],
            border_color=PALETTE["border"], border_width=1)
        self.alerts_card.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(self.alerts_card, text="▎ 风险与警示",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4))
        self.alerts_text = ctk.CTkLabel(
            self.alerts_card,
            text="无数据",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=12), justify="left", anchor="w",
            wraplength=1100)
        self.alerts_text.pack(anchor="w", padx=12, pady=(0, 12), fill="x")

    def _build_ai_tab(self):
        """Merged tab — Claude 专项 IP/risk view + ClaudeHero score ring +
        AI unlock cards (Claude / ChatGPT / Gemini)."""
        s = self._scroll(self.tab_ai)
        # 1. Claude visual hero
        self.claude_hero = ClaudeHero(s)
        self.claude_hero.pack(fill="x", padx=8, pady=(8, 12))
        # 2. Claude-focused IP & status cards (was the Claude 专项 tab)
        self.group_claude_focus = ResultGroup(
            s, "Claude 专项 (IP / 信任分 / 可达性)",
            subtitle=("对标 ip.net.coffee/claude/。Claude 可能走 IPv6 路径，"
                      "其风险评分与你的 IPv4 是两套数据。"))
        self.group_claude_focus.pack(fill="x", padx=8, pady=8)
        # 3. AI service unlock probes
        self.group_ai = ResultGroup(s, "AI 服务可用性 (Claude / ChatGPT / Gemini)")
        self.group_ai.pack(fill="x", padx=8, pady=8)
        # 4. Browser-only hint
        hint = ctk.CTkFrame(s, corner_radius=8,
                            fg_color=PALETTE["panel"],
                            border_color=PALETTE["border"], border_width=1)
        hint.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(hint, text="▎ 浏览器专项检测（CLI/桌面无法跑）",
                     text_color=PALETTE["accent"],
                     font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(
            hint,
            text=("WebRTC UDP 泄露、时区/语言/操作系统/"
                  "WebGL/Canvas 浏览器指纹一致性 — 必须在浏览器里跑。"),
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(size=11),
            wraplength=900, justify="left", anchor="w").pack(
            anchor="w", padx=12, pady=(0, 4))
        link = ctk.CTkLabel(
            hint, text="↗ https://ip.net.coffee/claude/",
            text_color=PALETTE["accent"],
            font=ctk.CTkFont(family="Menlo", size=12, underline=True),
            cursor="hand2")
        link.pack(anchor="w", padx=12, pady=(0, 10))
        link.bind("<Button-1>",
                  lambda _e: webbrowser.open("https://ip.net.coffee/claude/"))

    def _build_ip_tab(self):
        s = self._scroll(self.tab_ip)
        self.group_ip = ResultGroup(s, "多源 IP / Geo / ASN 数据")
        self.group_ip.pack(fill="x", padx=8, pady=8)

    def _build_risk_tab(self):
        s = self._scroll(self.tab_risk)
        self.group_risk = ResultGroup(s, "风险评分（聚合多个权威库）")
        self.group_risk.pack(fill="x", padx=8, pady=8)

    def _build_streaming_tab(self):
        s = self._scroll(self.tab_streaming)
        self.group_streaming = ResultGroup(s, "流媒体地区解锁")
        self.group_streaming.pack(fill="x", padx=8, pady=8)

    def _build_sites_tab(self):
        s = self._scroll(self.tab_sites)
        self.group_sites = ResultGroup(s, "网站连通性 (HTTP)")
        self.group_sites.pack(fill="x", padx=8, pady=8)

    def _build_net_tab(self):
        s = self._scroll(self.tab_net)
        self.group_latency = ResultGroup(s, "延迟 / TCP-Ping")
        self.group_latency.pack(fill="x", padx=8, pady=8)
        self.group_speed = ResultGroup(s, "测速与路由")
        self.group_speed.pack(fill="x", padx=8, pady=8)
        self.group_dns = ResultGroup(s, "DNS")
        self.group_dns.pack(fill="x", padx=8, pady=8)

    def _build_log_tab(self):
        self.log = ctk.CTkTextbox(
            self.tab_log,
            fg_color=PALETTE["panel"],
            border_color=PALETTE["border"], border_width=1,
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Menlo", size=11))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

    def _ensure_card(self, key: str, title: str) -> ResultCard:
        if key.startswith("latency_") or key == "latency_all":
            return self.group_latency.add(key, title)
        if key.startswith("site_"):
            return self.group_sites.add(key, title)
        kind = self.GROUP_MAP.get(key, "ip")
        group = {
            "ai_claude": self.group_claude_focus,
            "ai_unlock": self.group_ai,
            "ip": self.group_ip, "risk": self.group_risk,
            "streaming": self.group_streaming,
            "dns": self.group_dns, "speed": self.group_speed,
        }[kind]
        return group.add(key, title)

    # ── floating widget control ───────────────────────────────────────
    def show_widget(self):
        # Tear down splash if we came here from cold boot in widget mode
        try:
            if getattr(self, "_splash", None) and self._splash.winfo_exists():
                self._splash.destroy()
                self._splash = None
        except Exception:
            pass
        # save current main geometry before switching
        try:
            if self.state() != "withdrawn":
                cfg = load_config()
                cfg.setdefault("ui", {})
                cfg["ui"]["main_geometry"] = self.geometry()
                cfg["ui"]["mode"] = "widget"
                save_config(cfg)
        except Exception:
            pass
        # Always destroy + recreate the widget. CTkFrame's rounded corners
        # don't always re-render correctly after withdraw/deiconify cycles,
        # which is why repeated toggles eventually make the widget look like
        # a hard square. Rebuilding fresh sidesteps the issue entirely.
        if self.widget is not None and self.widget.winfo_exists():
            try:
                self.widget.destroy()
            except Exception:
                pass
            self.widget = None
        self.widget = FloatingWidget(self)
        if self.last_score is not None:
            self.widget.set_score(self.last_score)
        self.withdraw()

    def show_main_from_widget(self):
        if self.widget and self.widget.winfo_exists():
            self.widget.withdraw()
        self.deiconify()
        self.lift()
        self.focus_force()
        try:
            cfg = load_config()
            cfg.setdefault("ui", {})
            cfg["ui"]["mode"] = "main"
            save_config(cfg)
        except Exception:
            pass

    def reset_widget_position(self):
        """Rescue command — moves the floating widget back to a known visible
        location (primary monitor top-right corner). Useful if a previous
        snap on a now-disconnected monitor or wrong screen put it off-screen."""
        try:
            cfg = load_config()
            cfg.setdefault("ui", {})
            cfg["ui"]["widget_pos"] = ""
            cfg["ui"]["widget_snap"] = ""
            save_config(cfg)
        except Exception:
            pass
        if self.widget is not None and self.widget.winfo_exists():
            try:
                self.widget.destroy()
            except Exception:
                pass
            self.widget = None
        self.widget = FloatingWidget(self)
        if self.last_score is not None:
            self.widget.set_score(self.last_score)
        # Make sure it's on top
        try:
            self.widget.lift()
            self.widget.attributes("-topmost", True)
        except Exception:
            pass

    # ── periodic auto-refresh ─────────────────────────────────────────
    def _schedule_auto_refresh(self):
        """Cancel any pending tick and re-arm based on current settings."""
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
            self._refresh_after_id = None
        secs = int(load_config().get("settings", {}).get(
            "auto_refresh_seconds", 120))
        if secs <= 0:
            return
        self._refresh_after_id = self.after(
            secs * 1000, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self._refresh_after_id = None
        if self.running:
            # the running check will reschedule via _finish
            return
        self._log("⏱ 定时自动刷新触发")
        self.start_check()

    # ── network-change monitor ────────────────────────────────────────
    def _capture_net_signature(self) -> tuple:
        """Snapshot of network state. Used to detect IP / gateway changes."""
        sig: list[str] = []
        sysname = platform.system()
        # Default gateway
        try:
            if sysname == "Darwin":
                r = subprocess.run(
                    ["route", "-n", "get", "default"],
                    capture_output=True, text=True, timeout=2)
                for line in (r.stdout or "").splitlines():
                    line = line.strip()
                    if line.startswith("gateway:"):
                        sig.append("gw=" + line.split(":", 1)[1].strip())
                    elif line.startswith("interface:"):
                        sig.append("if=" + line.split(":", 1)[1].strip())
            elif sysname == "Windows":
                r = subprocess.run(
                    ["route", "print", "0.0.0.0"],
                    capture_output=True, text=True, timeout=2)
                for line in (r.stdout or "").splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == "0.0.0.0":
                        sig.append("gw=" + parts[2])
                        break
            else:  # Linux
                r = subprocess.run(
                    ["ip", "route", "show", "default"],
                    capture_output=True, text=True, timeout=2)
                for line in (r.stdout or "").splitlines():
                    if "default via" in line:
                        parts = line.split()
                        if "via" in parts:
                            i = parts.index("via")
                            if i + 1 < len(parts):
                                sig.append("gw=" + parts[i + 1])
                        if "dev" in parts:
                            i = parts.index("dev")
                            if i + 1 < len(parts):
                                sig.append("if=" + parts[i + 1])
                        break
        except Exception:
            pass
        # Local egress IP (the one used to reach the internet)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("1.1.1.1", 53))
            sig.append("lan=" + s.getsockname()[0])
            s.close()
        except Exception:
            pass
        return tuple(sig)

    def _start_network_monitor(self):
        cfg = load_config().get("settings", {})
        if not cfg.get("network_change_detection", True):
            return
        secs = max(2, int(cfg.get("network_poll_seconds", 5)))
        self._net_after_id = self.after(secs * 1000, self._network_tick)

    def _network_tick(self):
        cfg = load_config().get("settings", {})
        if not cfg.get("network_change_detection", True):
            self._net_after_id = None
            return
        try:
            sig = self._capture_net_signature()
        except Exception:
            sig = self._net_signature
        if sig and sig != self._net_signature:
            old = ", ".join(self._net_signature) or "(空)"
            new = ", ".join(sig) or "(空)"
            self._log(f"⚡ 网络变化  {old}  →  {new}")
            self._net_signature = sig
            if not self.running:
                self.start_check()
        secs = max(2, int(cfg.get("network_poll_seconds", 5)))
        self._net_after_id = self.after(secs * 1000, self._network_tick)

    # ──────────────────────────────────────────────────────────────────
    def _push_to_widget(self, ev: str, **kw):
        if self.widget is None or not self.widget.winfo_exists():
            return
        if ev == "score":
            self.widget.set_score(kw["score"], prev=kw.get("prev"))
        elif ev == "claude":
            self.widget.set_claude_status(kw["summary"], kw["status"])
        elif ev == "egress":
            self.widget.set_egress(kw["ip"], kw["loc"], kw["status"])
        elif ev == "latency":
            self.widget.set_latency(kw["summary"], kw["status"])
        elif ev == "reset":
            self.widget.reset()

    # ── run check ─────────────────────────────────────────────────────
    def start_check(self):
        if self.running:
            return
        self.running = True
        self.results.clear()
        self.run_btn.configure(text="◐ 检测中…", state="disabled")
        self.progress.set(0)
        self.score_label.configure(text="综合评分  …",
                                   text_color=PALETTE["muted"])
        self.overall_text.configure(text="正在并行执行多源探测…")
        self._log_clear()
        self._log(f"=== 开始检测  {datetime.now():%Y-%m-%d %H:%M:%S} ===")

        for g in (self.group_claude_focus, self.group_ip, self.group_risk,
                  self.group_ai, self.group_streaming, self.group_sites,
                  self.group_latency, self.group_speed, self.group_dns):
            g.clear()
        self.claude_hero.reset()
        # Reset dashboard fact strip + alerts to "checking" state
        for v in self._fact_rows.values():
            v.configure(text="…", text_color=PALETTE["muted"])
        self.alerts_text.configure(text="检测中…", text_color=PALETTE["muted"])
        # NOTE: do NOT reset the floating widget — it should keep showing
        # the previous score during refresh so the user always sees a
        # number + color. New score replaces it the moment _finish runs.

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            ip = checkers.get_my_ip()
            ipv6 = checkers.get_my_ipv6()
            self.queue.put(("ip_resolved", {"ip": ip, "ipv6": ipv6}))
            if not ip:
                self.queue.put(("done", "无法获取公网 IP"))
                return
            self.current_ip = ip
            batches = checkers.build_default_batches(ip)
            self.queue.put(("started", {"total": len(batches)}))

            done = [0]
            total = len(batches)

            def on_result(name: str, res):
                if isinstance(res, list):
                    for sub in res:
                        sub_name = sub.get("source", "?")
                        key = "latency_" + re.sub(r"\W+", "_", sub_name)
                        self.queue.put(("result",
                                        {"key": key, "title": sub_name,
                                         "res": sub}))
                else:
                    title = (res.get("source") if isinstance(res, dict)
                             and res.get("source") else name)
                    self.queue.put(("result",
                                    {"key": name, "title": title,
                                     "res": res}))
                done[0] += 1
                self.queue.put(("progress",
                                {"done": done[0], "total": total}))

            checkers.run_batches(batches, on_result)
            self.queue.put(("done", None))
        except Exception as e:
            self.queue.put(("done", f"发生异常: {e}"))

    # ── queue drain ───────────────────────────────────────────────────
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ip_resolved":
                    ip, ipv6 = payload["ip"], payload["ipv6"]
                    self.ip_label.configure(
                        text=f"IPv4 {ip or '—'}    IPv6 {ipv6 or '—'}")
                    self._log(f"公网 IP: {ip}  IPv6: {ipv6 or '无'}")
                elif kind == "started":
                    self._log(f"启动 {payload['total']} 项检测…")
                elif kind == "result":
                    self._handle_result(payload["key"], payload["title"],
                                        payload["res"])
                elif kind == "progress":
                    self.progress.set(payload["done"] / payload["total"])
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _handle_result(self, key: str, title: str, res: dict):
        # ── Claude hero panel + floating widget data sync ──
        if key == "iprisk":
            self.claude_hero.set_iprisk(res)
        elif key == "egress_ips":
            self.claude_hero.set_egress(res)
            d = res.get("data") or {}
            self._push_to_widget("egress",
                                 ip=d.get("claude_egress", "—"),
                                 loc=d.get("claude_loc", ""),
                                 status=res.get("status", "running"))
        elif key == "claude_reach":
            self.claude_hero.set_reach(res)
            self._push_to_widget("latency",
                                 summary=res.get("summary", ""),
                                 status=res.get("status", "running"))
        elif key == "claude_status":
            self.claude_hero.set_status(res)
        elif key == "claude":
            self._push_to_widget("claude",
                                 summary=res.get("summary", ""),
                                 status=res.get("status", "running"))

        card = self._ensure_card(key, title)
        card.set_result(res)
        self.results.append({**res, "_key": key})
        self._log(f"[{res.get('status','?').upper():>6}] {title}: "
                  f"{res.get('summary','')}")
        # Refresh the top-banner geo line whenever a richer source comes in.
        # We re-derive every time (no early-return) so a later source like
        # ip-api can fill in timezone after ipinfo got us city first.
        d = res.get("data") or {}
        loc_obj = d.get("location") or {}
        city = d.get("city") or loc_obj.get("city")
        country = (d.get("country") or d.get("country_name")
                   or loc_obj.get("country"))
        cc = (d.get("countryCode") or d.get("country_code")
              or loc_obj.get("country_code") or "")
        org = (d.get("org") or d.get("isp")
               or (d.get("company") or {}).get("name"))
        tz = (d.get("timezone") or loc_obj.get("timezone")
              or (d.get("time_zone") if isinstance(d.get("time_zone"), str)
                  else ""))
        if city or country or org or tz:
            cur = self.geo_label.cget("text")
            # Only overwrite if the new info is at least as rich
            new_bits = []
            if city or country:
                new_bits.append(f"{city or '?'}, {country or '?'}"
                                + (f" ({cc})" if cc else ""))
            if org:
                new_bits.append(str(org))
            if tz:
                new_bits.append(f"时区 {tz}")
            new_text = "位置  " + "  ·  ".join(new_bits)
            if cur in ("位置: —", "") or "位置: ?" in cur \
                    or len(new_text) > len(cur):
                color = (PALETTE["fail"] if cc.upper() == "CN"
                         else PALETTE["muted"])
                self.geo_label.configure(text=new_text, text_color=color)

    def _finish(self, err: str | None):
        self.running = False
        self.run_btn.configure(text="▸ 开始全面检测", state="normal")
        if err:
            messagebox.showerror("检测失败", err)
            self._log(f"!!! {err}")
            return
        cfg = load_config().get("settings", {})
        force_cn = bool(cfg.get("force_cn_score_cap",
                                cfg.get("force_cn_cap_20", True)))
        verdict = checkers.overall_verdict(self.results, force_cn_cap=force_cn)
        self.progress.set(1.0)
        color, _ = score_color_label(verdict["score"])
        self.score_label.configure(
            text=f"综合评分  {verdict['score']}/100 · {verdict['label']}",
            text_color=color)
        self.overall_text.configure(
            text=(f"共 {verdict['total']} 项："
                  f"✓ {verdict['ok']}  ⚠ {verdict['warn']}  "
                  f"✗ {verdict['fail']}  ? {verdict.get('manual', 0)}  "
                  f"! {verdict['error']}\n"
                  f"风险等级综合判断：{verdict['label']}"))
        if verdict.get("cn_capped"):
            self._log(f"⛔ 出口在中国大陆 (CN) → 强制综合评分上限 20")
        self._build_overview_summary()
        prev_score = self.last_score
        self.last_score = verdict["score"]
        # push score with previous value so widget can do drop-flash
        self._push_to_widget("score", score=verdict["score"],
                             prev=prev_score)
        # Log a quality drop if it crossed the alert threshold
        low = int(cfg.get("low_score_threshold", 40))
        drop = int(cfg.get("score_drop_threshold", 20))
        if verdict["score"] < low:
            self._log(f"🔴 警报: 评分 {verdict['score']} 低于阈值 {low} — 浮窗持续闪烁")
            # Auto-kill Claude-flavored processes if user enabled the option
            if cfg.get("kill_claude_on_low_score", True):
                self._kill_claude_processes_async()
        elif prev_score is not None and prev_score - verdict["score"] >= drop:
            self._log(f"🟠 警示: 评分从 {prev_score} 降到 {verdict['score']} "
                      f"(下降 {prev_score - verdict['score']}) — 浮窗短暂闪烁")
        self._log(f"=== 检测完成  评分 {verdict['score']}/100 ===")
        # re-arm periodic refresh
        self._schedule_auto_refresh()

    def _kill_claude_processes_async(self):
        """Run process kill on a worker thread so the UI never blocks on
        ps/tasklist/lsof/netstat. Logs result back to the main thread."""
        def worker():
            # Pre-scan so we can log the breakdown before the kill happens
            try:
                procs = system_actions.list_claude_processes()
            except Exception:
                procs = []
            name_matches = [p for p in procs if "⚡" not in p[1]]
            conn_matches = [p for p in procs if "⚡" in p[1]]
            ok, msg, items = system_actions.kill_claude_processes()
            def report():
                if not items:
                    self._log("🔪 低分自动清理: 未发现 Claude 相关进程 / 连接")
                    return
                head = "🔪" if ok else "⚠"
                self._log(f"{head} 低分自动清理: {msg}  "
                          f"(进程名匹配 {len(name_matches)}, "
                          f"连接 Claude 匹配 {len(conn_matches)})")
                for it in items[:10]:
                    self._log(f"   · {it}")
                if len(items) > 10:
                    self._log(f"   · …还有 {len(items) - 10} 个")
            try:
                self.after(0, report)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _build_overview_summary(self):
        """Populate the dashboard tab.

        - Fact strip: 出口国家 / 时区 / ASN / IPv4 / Claude 视角 / DNS
        - Alerts text: a compact list of every fail/warn/manual item, formatted
          as one line each. NO duplicate cards from other tabs.
        """
        by_key = {r.get("_key"): r for r in self.results}

        # ── Fact strip ──
        # country
        cc = ""
        country_name = ""
        for k in ("egress_ips", "iprisk", "ip-api", "ipinfo",
                  "ipapi.is", "ip2location", "dbip"):
            r = by_key.get(k) or {}
            d = r.get("data") or {}
            if k == "egress_ips":
                cc = (d.get("claude_loc") or "").upper()
            elif k == "iprisk":
                cc = (d.get("countryCode") or "").upper()
                country_name = d.get("country") or ""
            elif k == "ip-api":
                cc = cc or (d.get("countryCode") or "").upper()
                country_name = country_name or d.get("country") or ""
            elif k == "ipinfo":
                cc = cc or (d.get("country") or "").upper()
            elif k == "ipapi.is":
                loc = d.get("location") or {}
                cc = cc or (loc.get("country_code") or "").upper()
                country_name = country_name or loc.get("country") or ""
            elif k == "ip2location":
                cc = cc or (d.get("country_code") or "").upper()
                country_name = country_name or d.get("country_name") or ""
            elif k == "dbip":
                cc = cc or (d.get("country") or "").upper()
            if cc:
                break
        if cc == "CN":
            self._fact_rows["country"].configure(
                text=f"⚠ 中国大陆 (CN)  · 强制封顶 {checkers.CN_SCORE_CAP}",
                text_color=PALETTE["fail"])
        elif cc:
            self._fact_rows["country"].configure(
                text=f"{country_name or cc}  ({cc})",
                text_color=PALETTE["ok"])
        else:
            self._fact_rows["country"].configure(text="—",
                                                  text_color=PALETTE["muted"])

        # timezone — comes from ip-api
        tz = ((by_key.get("ip-api") or {}).get("data") or {}).get("timezone") \
             or ((by_key.get("ipinfo") or {}).get("data") or {}).get("timezone") \
             or ((by_key.get("ipapi.is") or {}).get("data") or {}).get("location", {}).get("timezone") \
             or ""
        self._fact_rows["timezone"].configure(
            text=tz or "—",
            text_color=PALETTE["text"] if tz else PALETTE["muted"])

        # ASN/ISP
        asn = ""
        for k in ("ipinfo", "ip-api", "ipapi.is", "ip2location"):
            d = (by_key.get(k) or {}).get("data") or {}
            if k == "ipinfo":
                a = d.get("asn") or {}
                c = d.get("company") or {}
                if a.get("name") or c.get("name"):
                    asn = f"{a.get('asn','')} {a.get('name') or c.get('name','')}"
            elif k == "ip-api":
                if d.get("as"):
                    asn = asn or d.get("as")
            elif k == "ipapi.is":
                co = d.get("company") or {}
                if co.get("name"):
                    asn = asn or co.get("name")
            elif k == "ip2location":
                if d.get("as"):
                    asn = asn or f"AS{d.get('asn','')} {d.get('as','')}"
            if asn:
                break
        self._fact_rows["asn"].configure(
            text=asn or "—",
            text_color=PALETTE["text"] if asn else PALETTE["muted"])

        # IPv4 + Claude visible IP — now with city/ASN attribution
        eg = (by_key.get("egress_ips") or {}).get("data") or {}
        geo_by_ip = eg.get("geo") or {}

        def _attribution(ip: str) -> str:
            g = geo_by_ip.get(ip) or {}
            bits = []
            city = g.get("city")
            country = g.get("country") or g.get("countryCode")
            if city and country:
                bits.append(f"{city}, {country}")
            elif country:
                bits.append(country)
            if g.get("as"):
                # trim long AS names
                as_str = g["as"]
                if len(as_str) > 36:
                    as_str = as_str[:33] + "…"
                bits.append(as_str)
            return " · ".join(bits)

        v4 = eg.get("cn_visible_ipv4") or self.current_ip or ""
        v4_text = v4
        if v4:
            extra = _attribution(v4)
            if extra:
                v4_text = f"{v4}  ({extra})"
        self._fact_rows["ip_v4"].configure(
            text=v4_text or "—",
            text_color=PALETTE["text"] if v4 else PALETTE["muted"])

        cv = eg.get("claude_egress") or ""
        cv_loc = (eg.get("claude_loc") or "").upper()
        cv_text = cv
        if cv:
            extra = _attribution(cv) or (cv_loc if cv_loc else "")
            if extra:
                cv_text = f"{cv}  ({extra})"
            if ":" in cv:
                cv_text += "  ⚑ IPv6"
        self._fact_rows["ip_v6"].configure(
            text=cv_text or "—",
            text_color=PALETTE["accent"] if cv else PALETTE["muted"])

        # DNS resolver country
        dns_d = (by_key.get("dns_leak") or {}).get("data") or {}
        if dns_d:
            ds = (f"{dns_d.get('resolver_ip','?')}  "
                  f"({dns_d.get('resolver_cc','?')})")
            leak = dns_d.get("public_country_cc") and \
                   dns_d.get("resolver_cc") and \
                   dns_d.get("public_country_cc") != dns_d.get("resolver_cc")
            self._fact_rows["dns"].configure(
                text=ds + ("  ⚠ 可能泄露" if leak else ""),
                text_color=PALETTE["fail"] if leak else PALETTE["text"])
        else:
            self._fact_rows["dns"].configure(text="—",
                                              text_color=PALETTE["muted"])

        # ── Alerts: compact one-liners for each fail/warn/manual ──
        problems = []
        for r in self.results:
            st = r.get("status", "")
            if st in ("fail", "warn", "manual"):
                icon = STATUS_ICONS.get(st, "•")
                problems.append(
                    f"{icon}  [{r.get('name','?')}]  {r.get('summary','')}")
        if problems:
            self.alerts_text.configure(
                text="\n".join(problems),
                text_color=PALETTE["text"])
        else:
            self.alerts_text.configure(
                text="✓ 所有检测项均通过 · 无警示",
                text_color=PALETTE["ok"])

    def _log(self, line: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {line}\n")
        self.log.see("end")

    def _log_clear(self):
        self.log.delete("1.0", "end")

    def open_settings(self):
        SettingsWindow(self)

    def export_report(self):
        if not self.results:
            messagebox.showinfo("提示", "请先运行一次检测再导出报告")
            return
        default_name = f"ip-quality-{datetime.now():%Y%m%d-%H%M%S}.html"
        path = filedialog.asksaveasfilename(
            defaultextension=".html",
            initialfile=default_name,
            filetypes=[("HTML 报告", "*.html"), ("JSON 数据", "*.json")])
        if not path:
            return
        try:
            if path.endswith(".json"):
                Path(path).write_text(json.dumps({
                    "generated": datetime.now().isoformat(),
                    "ip": self.current_ip,
                    "verdict": checkers.overall_verdict(self.results),
                    "results": self.results,
                }, indent=2, ensure_ascii=False), encoding="utf-8")
            else:
                Path(path).write_text(self._render_html(), encoding="utf-8")
            messagebox.showinfo("已导出", f"报告已保存到\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _render_html(self) -> str:
        v = checkers.overall_verdict(self.results)
        rows = []
        for r in self.results:
            color = {"ok": "#3ddc97", "warn": "#ffc857",
                     "fail": "#ff5e5b", "manual": "#a78bfa",
                     "error": "#7d8896"}.get(r.get("status"), "#666")
            try:
                data_pretty = json.dumps(r.get("data", {}),
                                         indent=2, ensure_ascii=False)
            except Exception:
                data_pretty = str(r.get("data", {}))
            verify = (f'<a href="{r.get("verify_url","")}" target="_blank">'
                      f'对照 ↗</a>' if r.get("verify_url") else "")
            rows.append(f"""
            <tr>
              <td><b>{r.get('source','')}</b></td>
              <td><span style="color:{color}">●</span> {r.get('status','')}</td>
              <td>{r.get('summary','')}</td>
              <td>{verify}</td>
              <td><details><summary>展开</summary><pre>{data_pretty}</pre></details></td>
            </tr>""")
        score_color = {"优秀": "#3ddc97", "良好": "#ffc857"}.get(
            v["label"], "#ff5e5b")
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>IP 网络质量评估报告</title>
<style>
  body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;background:#0a0e1a;color:#e6edf3}}
  h1{{margin-bottom:0;color:#00d4ff}} .meta{{color:#8b95a8;margin-top:4px}}
  .score{{font-size:48px;font-weight:bold;color:{score_color}}}
  table{{border-collapse:collapse;width:100%;margin-top:16px;background:#111726;border-radius:8px;overflow:hidden}}
  th,td{{border:1px solid #1f2a44;padding:8px;text-align:left;vertical-align:top;font-size:13px}}
  th{{background:#172033;color:#00d4ff}}
  pre{{margin:0;font-size:11px;background:#0a0e1a;padding:8px;border-radius:4px;max-width:600px;overflow:auto;color:#8b95a8}}
  a{{color:#00d4ff}}
</style></head><body>
<h1>⌬ IP 网络质量评估报告</h1>
<div class="meta">生成时间: {datetime.now():%Y-%m-%d %H:%M:%S} · 公网 IP: {self.current_ip}</div>
<div class="score">{v['score']}/100 — {v['label']}</div>
<p>共 {v['total']} 项 · ✓ {v['ok']} · ⚠ {v['warn']} · ✗ {v['fail']} · ? {v.get('manual', 0)} · ! {v['error']}</p>
<table><thead><tr><th>来源</th><th>状态</th><th>摘要</th><th>对照</th><th>原始数据</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""


# ============================================================================
if __name__ == "__main__":
    # Single Tk root only. The splash is created inside App.__init__ as a
    # Toplevel(self), so customtkinter / Tk see exactly one interpreter
    # context. Creating two tk.Tk() instances on macOS causes the second
    # one's event pumping to stall — that's the "页面卡死" symptom we hit.
    app = App()
    app.mainloop()
