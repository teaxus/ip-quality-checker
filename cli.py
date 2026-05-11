"""Command-line IP / network quality checker.

Usage:
    python cli.py                    # run all checks
    python cli.py --only ipinfo,claude,netflix   # run named checks
    python cli.py --ip 8.8.8.8       # check a specific IP (still runs site/AI checks against own egress)
    python cli.py --raw              # also print raw JSON for each check
    python cli.py --json             # output a single JSON document, no formatting
    python cli.py --no-color         # disable ANSI colors

Each result line shows:
    [STATUS] Source — summary
        request: <url we hit>
        verify : <public website URL — open this to compare>
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime

import checkers


# ---------------------------------------------------------------------------
# ANSI helpers (no external deps)
# ---------------------------------------------------------------------------
class C:
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    B = "\033[34m"
    M = "\033[35m"
    Cy = "\033[36m"
    GR = "\033[90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        for k in list(vars(cls).keys()):
            v = getattr(cls, k)
            if isinstance(v, str) and v.startswith("\033"):
                setattr(cls, k, "")


STATUS_TAG = {
    "ok":     ("✓ OK    ", "G"),
    "warn":   ("⚠ WARN  ", "Y"),
    "fail":   ("✗ FAIL  ", "R"),
    "manual": ("? MANUAL", "M"),
    "error":  ("! ERROR ", "GR"),
    "running": ("… RUN  ", "Cy"),
}


def colorize(text: str, color_attr: str) -> str:
    return f"{getattr(C, color_attr)}{text}{C.END}"


def fmt_status(status: str) -> str:
    tag, col = STATUS_TAG.get(status, (status, "GR"))
    return colorize(tag, col)


# ---------------------------------------------------------------------------
# Pretty-print one result
# ---------------------------------------------------------------------------
def print_result(res: dict, raw: bool = False, indent: str = "") -> None:
    src = res.get("source", "?")
    summary = res.get("summary", "")
    print(f"{indent}{fmt_status(res.get('status','error'))} "
          f"{C.BOLD}{src}{C.END} — {summary}")
    if res.get("request_url"):
        print(f"{indent}    {C.DIM}request:{C.END} {C.GR}{res['request_url']}{C.END}")
    if res.get("verify_url"):
        print(f"{indent}    {C.DIM}verify :{C.END} {C.B}{res['verify_url']}{C.END}")
    if res.get("error"):
        print(f"{indent}    {C.DIM}error  :{C.END} {C.R}{res['error']}{C.END}")
    if raw and res.get("data"):
        try:
            j = json.dumps(res["data"], indent=2, ensure_ascii=False)
        except Exception:
            j = str(res["data"])
        for line in j.splitlines():
            print(f"{indent}    {C.GR}{line}{C.END}")


# ---------------------------------------------------------------------------
# Group routing — same as GUI
# ---------------------------------------------------------------------------
GROUPS = {
    "Claude Focus":   ["egress_ips", "iprisk", "claude_reach", "claude_status"],
    "IP / Geo / ASN": ["ipinfo", "ip-api", "ipapi.is", "ip2location", "dbip"],
    "Risk / Fraud":   ["scamalytics", "ipqs", "abuseipdb", "ping0"],
    "AI Services":    ["claude", "chatgpt", "gemini"],
    "Streaming":      ["netflix", "disney", "youtube_premium", "tiktok", "spotify"],
    "Site Reach":     [f"site_{n}" for (n, _, _) in checkers.SITE_TARGETS],
    "Latency":        ["latency_all"],
    "Speed":          ["speed"],
}
KEY_TO_GROUP = {k: g for g, ks in GROUPS.items() for k in ks}


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="IP & network quality CLI checker")
    ap.add_argument("--ip", help="check this IP for IP/risk lookups (own egress IP "
                                  "is still used for site/AI/streaming checks)")
    ap.add_argument("--only", help="comma-separated list of check names "
                                    "(see --list)")
    ap.add_argument("--list", action="store_true",
                    help="list all available check names and exit")
    ap.add_argument("--raw", action="store_true",
                    help="also dump raw JSON data for each check")
    ap.add_argument("--json", action="store_true",
                    help="emit a single JSON document on stdout, no formatting")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    ap.add_argument("--workers", type=int, default=10,
                    help="parallel workers (default 10)")
    ap.add_argument("--timeout", type=int, default=10,
                    help="per-request timeout seconds (default 10)")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        C.disable()

    if args.list:
        print(f"{C.BOLD}Available checks:{C.END}")
        for group, keys in GROUPS.items():
            print(f"  {C.Cy}{group}{C.END}")
            for k in keys:
                print(f"    - {k}")
        return

    # apply timeout via config in-memory (not persisted)
    from config import load_config, save_config
    cfg = load_config()
    cfg.setdefault("settings", {})["request_timeout"] = args.timeout
    cfg["settings"]["max_workers"] = args.workers
    save_config(cfg)

    # 1. resolve egress IP
    if not args.json:
        print(f"{C.BOLD}{C.Cy}IP & 网络质量诊断{C.END}  "
              f"{C.DIM}{datetime.now():%Y-%m-%d %H:%M:%S}{C.END}")
        print(f"{C.DIM}—" * 60 + f"{C.END}")
        print(f"{C.DIM}解析公网 IP …{C.END}")
    ip = args.ip or checkers.get_my_ip()
    ipv6 = checkers.get_my_ipv6()
    if not ip:
        if args.json:
            print(json.dumps({"error": "cannot resolve public IP"}))
        else:
            print(f"{C.R}无法获取公网 IP，请检查网络连接{C.END}")
        sys.exit(1)
    if not args.json:
        print(f"  {C.BOLD}IPv4{C.END}: {ip}    {C.BOLD}IPv6{C.END}: {ipv6 or '—'}")
        print()

    # 2. build batches
    all_batches = checkers.build_default_batches(ip)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        # accept either group keys ("ipinfo") or full prefixes ("site_")
        def keep(b):
            base = b.name.split("_")[0] if b.name.startswith("site_") else b.name
            return b.name in wanted or base in wanted or any(b.name.startswith(w) for w in wanted)
        all_batches = [b for b in all_batches if keep(b)]
        if not all_batches:
            print(f"{C.R}--only 没有匹配到任何 check{C.END}")
            sys.exit(2)

    # 3. run with live streaming output
    results: list[dict] = []
    seen_keys: set[str] = set()
    lock = threading.Lock()
    progress = {"done": 0, "total": len(all_batches)}

    def cb(name: str, res):
        with lock:
            progress["done"] += 1
            done = progress["done"]
            total = progress["total"]
            if isinstance(res, list):
                for sub in res:
                    if not args.json:
                        print(f"{C.DIM}[{done:>2}/{total}]{C.END} ", end="")
                        print_result(sub, raw=args.raw)
                    results.append({**sub, "_key": name})
            else:
                if not args.json:
                    print(f"{C.DIM}[{done:>2}/{total}]{C.END} ", end="")
                    print_result(res, raw=args.raw)
                results.append({**res, "_key": name})

    t0 = time.perf_counter()
    checkers.run_batches(all_batches, cb, max_workers=args.workers)
    elapsed = time.perf_counter() - t0

    # 4. summary
    if args.json:
        print(json.dumps({
            "generated": datetime.now().isoformat(),
            "ip": ip, "ipv6": ipv6,
            "verdict": checkers.overall_verdict(results),
            "results": results,
        }, indent=2, ensure_ascii=False))
        return

    print()
    v = checkers.overall_verdict(results)
    score_color = "G" if v["score"] >= 80 else ("Y" if v["score"] >= 60 else "R")
    print(f"{C.DIM}—" * 60 + f"{C.END}")
    print(f"{C.BOLD}综合评分{C.END} "
          f"{colorize(str(v['score']) + '/100', score_color)} "
          f"{C.BOLD}{v['label']}{C.END}    "
          f"{C.G}✓{v['ok']}{C.END} "
          f"{C.Y}⚠{v['warn']}{C.END} "
          f"{C.R}✗{v['fail']}{C.END} "
          f"{C.M}?{v['manual']}{C.END} "
          f"{C.GR}!{v['error']}{C.END}    "
          f"{C.DIM}{elapsed:.1f}s{C.END}")
    if v["manual"]:
        print(f"{C.M}? = 需要在浏览器中手动核对（多为 Cloudflare 拦截或缺 API Key）{C.END}")


if __name__ == "__main__":
    main()
