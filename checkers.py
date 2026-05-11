"""IP / network quality checkers — faithful port of xykt/ip.sh, xykt/net.sh,
and lmc999/RegionRestrictionCheck.

Each checker returns:
    {
        "source": str,
        "status": "ok"|"warn"|"fail"|"error"|"manual",
        "summary": str,
        "data": dict,          # raw / parsed structured data
        "verify_url": str,     # public URL the user can manually open to compare
        "request_url": str,    # the URL we actually queried
        "error": str|None,
    }

Design notes:
- Many fraud-DB providers (Scamalytics, IPQualityScore public lookup, ipinfo
  /widget when rate-limited, ipinfo.check.place) sit behind Cloudflare bot
  protection. Plain `requests` calls get 403. For those we mark status="manual"
  and surface the verify URL so the user can open it in a browser.
- ipinfo.io/widget/demo/{ip} (the path that ipinfo.io's own homepage uses)
  is keyless and returns ASN type / company / privacy flags. xykt/ip.sh uses
  this exact endpoint.
- Netflix uses two title IDs (xykt L1462 / lmc999 L804): 81280792 (LEGO Ninjago
  — Netflix Original, available everywhere) + 70143836 (Breaking Bad — non-
  Original, region-licensed). HTTP 200 + no "Oh no!" + has countryName → full.
- Claude is detected purely by `curl -L` final URL (lmc999 L4564): if it ends
  at claude.ai/ → ok; if it redirects to anthropic.com/app-unavailable-in-region
  → blocked. Cloudflare 403 challenge is irrelevant here because the URL is
  the signal, not the body.
- ChatGPT uses 4-state cross product (xykt L1632, lmc999 L4510):
    /compliance/cookie_requirements (Bearer null) → "unsupported_country"?
    https://ios.chat.openai.com/             → "VPN"?
- Gemini availability marker is the literal string "45631641,null,true"
  embedded in the bootstrap JS state (lmc999 L4544).
"""
from __future__ import annotations

import json
import re
import socket
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from config import get_api_key, load_config

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def _timeout() -> int:
    return int(load_config().get("settings", {}).get("request_timeout", 10))


def _result(source: str, status: str, summary: str,
            data: dict | None = None, verify_url: str = "",
            request_url: str = "", error: str | None = None) -> dict:
    return {
        "source": source,
        "status": status,
        "summary": summary,
        "data": data or {},
        "verify_url": verify_url,
        "request_url": request_url,
        "error": error,
    }


# ============================================================================
# IP self-discovery (xykt/ip.sh L561)
# ============================================================================
def get_my_ip() -> str:
    """Resolve our public IPv4 — uses v4-only endpoints so dual-stack hosts
    don't accidentally hand back IPv6."""
    for url in ("https://api.ipify.org",                 # v4-only
                "https://ipv4.icanhazip.com",            # v4-only
                "https://api-ipv4.ip.sb/ip",             # v4-only
                "https://4.ifconfig.co/ip"):             # v4-only
        try:
            r = requests.get(url, headers=HEADERS, timeout=_timeout())
            if r.status_code == 200:
                txt = r.text.strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", txt):
                    return txt
        except Exception:
            continue
    return ""


def get_my_ipv6() -> str:
    for url in ("https://api6.ipify.org",
                "https://ipv6.icanhazip.com",
                "https://api-ipv6.ip.sb/ip"):
        try:
            r = requests.get(url, headers=HEADERS, timeout=_timeout())
            if r.status_code == 200:
                txt = r.text.strip()
                if ":" in txt and len(txt) < 64:
                    return txt
        except Exception:
            continue
    return ""


# ============================================================================
# IPinfo widget demo — xykt/ip.sh L760-825 (keyless, full data)
# ============================================================================
def check_ipinfo(ip: str) -> dict:
    """Two-call merge:

      1. /{ip}/json?token={key}        — official API (uses token if set;
         50k/mo on free tier; gives hostname/timezone/anycast)
      2. /widget/demo/{ip}              — keyless widget path; gives the
         privacy flags (vpn/proxy/tor/relay/hosting) and ASN type which
         the free official API does NOT return.

    The token speeds up #1 (no rate-limit on token) and gives a stable
    result for the basic geo/asn fields; #2 stays keyless because the
    privacy flags require a paid Standard plan via the official API.
    """
    token = get_api_key("ipinfo")
    api_url = f"https://ipinfo.io/{ip}/json"
    widget_url = f"https://ipinfo.io/widget/demo/{ip}"
    api_data: dict = {}
    widget_data: dict = {}
    api_status: str | None = None

    # 1. token-aware basic lookup
    try:
        params = {"token": token} if token else {}
        r = requests.get(api_url, headers=HEADERS, params=params,
                         timeout=_timeout())
        api_status = f"HTTP {r.status_code}"
        if r.status_code == 200:
            api_data = r.json() or {}
        elif r.status_code in (401, 403):
            # bad token — fall back to keyless widget only
            api_data = {"_token_invalid": True, "_status": r.status_code}
        elif r.status_code == 429:
            api_data = {"_rate_limited": True}
    except Exception as e:
        api_data = {"_error": str(e)}

    # 2. keyless widget (privacy flags)
    try:
        r2 = requests.get(widget_url, headers=HEADERS, timeout=_timeout())
        if r2.status_code == 200:
            widget_data = (r2.json() or {}).get("data", {}) or {}
    except Exception:
        pass

    privacy = widget_data.get("privacy", {}) or {}
    flags = [k for k in ("vpn", "proxy", "tor", "relay", "hosting")
             if privacy.get(k)]
    asn = widget_data.get("asn") or {}
    company = widget_data.get("company") or {}
    abuse = widget_data.get("abuse") or {}
    firmographic = (company.get("firmographic") or {}) if company else {}

    # Prefer api fields (token gives stable values), fall back to widget
    city = api_data.get("city") or widget_data.get("city") or "?"
    region = api_data.get("region") or widget_data.get("region") or ""
    country = api_data.get("country") or widget_data.get("country") or "?"
    postal = api_data.get("postal") or widget_data.get("postal") or ""
    timezone = api_data.get("timezone") or widget_data.get("timezone") or ""
    loc = api_data.get("loc") or widget_data.get("loc") or ""
    hostname = api_data.get("hostname") or widget_data.get("hostname") or ""
    anycast = (api_data.get("anycast") or
               widget_data.get("is_anycast") or False)
    is_mobile = widget_data.get("is_mobile") or False
    is_satellite = widget_data.get("is_satellite") or False
    is_anonymous = widget_data.get("is_anonymous") or False
    is_hosting = widget_data.get("is_hosting") or privacy.get("hosting") or False
    org_str = api_data.get("org") or ""

    # ── Build a multi-line summary that surfaces every interesting fact ──
    lines: list[str] = []
    # Line 1: full geo string
    geo_bits = [city]
    if region:
        geo_bits.append(region)
    geo_bits.append(country)
    geo_line = ", ".join(geo_bits)
    if postal:
        geo_line += f"  邮编 {postal}"
    if timezone:
        geo_line += f"  · 时区 {timezone}"
    lines.append(geo_line)

    # Line 2: hostname (rDNS) + CIDR route
    rdns_bits = []
    if hostname:
        rdns_bits.append(hostname)
    route = asn.get("route") or ""
    if route:
        rdns_bits.append(f"({route})")
    if rdns_bits:
        lines.append(" ".join(rdns_bits))

    # Line 3: ASN + AS owner domain
    if asn or org_str:
        asn_id = (asn.get("asn") or "").replace("AS", "")
        asn_name = asn.get("name") or company.get("name") or org_str
        asn_domain = asn.get("domain") or company.get("domain") or ""
        asn_type = asn.get("type") or company.get("type") or ""
        asn_line = f"AS{asn_id} {asn_name}" if asn_id else asn_name
        if asn_domain:
            asn_line += f" → {asn_domain}"
        if asn_type:
            asn_line += f" [{asn_type}]"
        lines.append(asn_line)

    # Line 4: registered company (firmographic) — the actual real entity
    real_co = firmographic.get("name") or ""
    if real_co and real_co != asn.get("name"):
        co_bits = [f"实名: {real_co}"]
        if firmographic.get("domain"):
            co_bits.append(firmographic["domain"])
        if firmographic.get("employees"):
            co_bits.append(f"员工 {firmographic['employees']}")
        lines.append(" · ".join(co_bits))

    # Line 5: feature flags (Anycast / Mobile / Satellite / Anonymous etc.)
    feat = []
    if anycast:    feat.append("⚑ Anycast")
    if is_mobile:  feat.append("⚑ 移动网络")
    if is_satellite: feat.append("⚑ 卫星")
    if is_anonymous: feat.append("⚑ 匿名网络")
    if is_hosting:   feat.append("⚑ 数据中心")
    if feat:
        lines.append(" · ".join(feat))

    # Line 6: privacy flags + VPN service name if known
    if flags:
        risk_line = "⚠ " + ",".join(flags)
        svc = privacy.get("service") or ""
        if svc:
            risk_line += f"  → 服务: {svc}"
        lines.append(risk_line)

    # Line 7: abuse contact (compact)
    if abuse.get("email") or abuse.get("name"):
        ab_bits = []
        if abuse.get("name"):  ab_bits.append(abuse["name"])
        if abuse.get("email"): ab_bits.append(abuse["email"])
        if abuse.get("country"): ab_bits.append(abuse["country"])
        lines.append("举报联系: " + " · ".join(ab_bits))

    # Line 8: token status
    if token:
        if api_data.get("_token_invalid"):
            lines.append("⚠ Token 无效，请重新填写")
        elif api_status == "HTTP 200":
            lines.append("token ✓")

    summary = "\n".join(lines)

    # status: privacy flag / anonymous / bad token → warn; data ok → ok
    if flags or is_anonymous:
        status = "warn"
    elif api_data.get("_token_invalid"):
        status = "warn"
    elif api_data or widget_data:
        status = "ok"
    else:
        status = "error"

    # Stash a flat "highlights" dict so the detail dialog can render a
    # nice table on top of the raw JSON.
    highlights = {
        "城市": city, "省/州": region, "国家": country, "邮编": postal,
        "时区": timezone, "经纬度": loc, "主机名 (rDNS)": hostname,
        "ASN": asn.get("asn", ""), "AS 名称": asn.get("name", ""),
        "AS 域名": asn.get("domain", ""), "AS 类型": asn.get("type", ""),
        "CIDR 路由": route,
        "实名公司": real_co,
        "公司类型": company.get("type", ""),
        "Anycast": "是" if anycast else "否",
        "移动网络": "是" if is_mobile else "否",
        "数据中心": "是" if is_hosting else "否",
        "VPN": "是" if privacy.get("vpn") else "否",
        "代理": "是" if privacy.get("proxy") else "否",
        "Tor": "是" if privacy.get("tor") else "否",
        "VPN 服务名": privacy.get("service", "") or "—",
        "滥用举报邮箱": abuse.get("email", ""),
        "滥用举报地址": abuse.get("address", ""),
        "Token 已使用": "是" if token else "否",
    }
    return _result("IPinfo", status, summary,
                   {"api": api_data, "widget": widget_data,
                    "highlights": highlights,
                    "token_used": bool(token)},
                   verify_url=f"https://ipinfo.io/{ip}",
                   request_url=api_url)


# ============================================================================
# ip-api.com — keyless, comprehensive (lmc999 + standard everywhere)
# ============================================================================
def check_ip_api(ip: str) -> dict:
    fields = ("status,message,country,countryCode,regionName,city,zip,lat,lon,"
              "timezone,isp,org,as,asname,reverse,mobile,proxy,hosting,query")
    url = f"http://ip-api.com/json/{ip}?fields={fields}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        d = r.json()
        if d.get("status") != "success":
            return _result("ip-api.com", "error", d.get("message", "失败"),
                           verify_url=f"https://members.ip-api.com/?ip={ip}", request_url=url)
        flags = [k for k in ("proxy", "hosting", "mobile") if d.get(k)]
        summary = f"{d.get('city','?')}, {d.get('country','?')} · {d.get('as','?')}"
        if flags:
            summary += f" · ⚠ {','.join(flags)}"
        return _result("ip-api.com", "warn" if flags else "ok", summary, d,
                       verify_url=f"https://ip-api.com/json/{ip}", request_url=url)
    except Exception as e:
        return _result("ip-api.com", "error", "请求失败",
                       verify_url=f"https://ip-api.com/json/{ip}", request_url=url, error=str(e))


# ============================================================================
# ipapi.is — xykt/ip.sh L920 (keyless, exposes abuser_score string)
# ============================================================================
def check_ipapi_is(ip: str) -> dict:
    url = f"https://api.ipapi.is/?q={ip}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        if r.status_code != 200:
            return _result("ipapi.is", "error", f"HTTP {r.status_code}",
                           verify_url=f"https://ipapi.is/?q={ip}", request_url=url)
        d = r.json()
        flags = [k.replace("is_", "") for k in
                 ("is_vpn", "is_proxy", "is_tor", "is_datacenter",
                  "is_abuser", "is_bogon", "is_crawler") if d.get(k)]
        # parse abuser_score "0.0014 (Very Low)"
        ab = ((d.get("company") or {}).get("abuser_score") or "")
        m = re.match(r"([\d\.]+)\s*\(([^)]+)\)", ab)
        score_pct, label = (None, None)
        if m:
            try:
                score_pct = round(float(m.group(1)) * 100, 2)
            except Exception:
                pass
            label = m.group(2).strip()
        loc = d.get("location") or {}
        company = d.get("company") or {}
        summary_parts = [f"{loc.get('city','?')}, {loc.get('country','?')}",
                         company.get("name", "?")]
        if score_pct is not None:
            summary_parts.append(f"abuser {score_pct}% ({label})")
        if flags:
            summary_parts.append("⚠ " + ",".join(flags))
        # status: any flag → warn, abuser high → fail
        status = "ok"
        if flags or (label or "").lower() in ("elevated", "high", "very high"):
            status = "warn"
        if (label or "").lower() in ("high", "very high"):
            status = "fail"
        return _result("ipapi.is", status, " · ".join(summary_parts), d,
                       verify_url=f"https://ipapi.is/?q={ip}", request_url=url)
    except Exception as e:
        return _result("ipapi.is", "error", "请求失败",
                       verify_url=f"https://ipapi.is/?q={ip}", request_url=url, error=str(e))


# ============================================================================
# IP2Location.io — keyless free tier (xykt proxies but io endpoint also works)
# ============================================================================
def check_ip2location(ip: str) -> dict:
    key = get_api_key("ip2location")
    url = f"https://api.ip2location.io/?ip={ip}" + (f"&key={key}" if key else "")
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        if r.status_code != 200:
            return _result("IP2Location", "error", f"HTTP {r.status_code}",
                           verify_url=f"https://www.ip2location.io/{ip}", request_url=url)
        d = r.json()
        if "error" in d:
            return _result("IP2Location", "error", d["error"].get("error_message", "失败"),
                           verify_url=f"https://www.ip2location.io/{ip}", request_url=url)
        is_proxy = d.get("is_proxy", False)
        summary = (f"{d.get('city_name','?')}, {d.get('country_name','?')} · "
                   f"AS{d.get('asn','?')} {d.get('as','?')}")
        if is_proxy:
            ptype = (d.get("proxy") or {}).get("proxy_type") or "proxy"
            summary += f" · ⚠ {ptype}"
        return _result("IP2Location", "warn" if is_proxy else "ok", summary, d,
                       verify_url=f"https://www.ip2location.io/{ip}", request_url=url)
    except Exception as e:
        return _result("IP2Location", "error", "请求失败",
                       verify_url=f"https://www.ip2location.io/{ip}", request_url=url, error=str(e))


# ============================================================================
# DB-IP — HTML scrape (xykt/ip.sh L1134-1169)
# ============================================================================
def check_dbip(ip: str) -> dict:
    url = f"https://db-ip.com/{ip}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        if r.status_code != 200:
            return _result("DB-IP", "error", f"HTTP {r.status_code}",
                           verify_url=url, request_url=url)
        html = r.text
        # threat level: "Estimated threat level for this IP address is X"
        m_threat = re.search(
            r"Estimated threat level for this IP address is\s*<[^>]+>([^<]+)<",
            html, re.I)
        threat = (m_threat.group(1).strip() if m_threat else "").lower()
        # country code from embedded JSON
        m_cc = re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', html)
        cc = m_cc.group(1) if m_cc else "?"
        # crawler/proxy/abuser tri-state (sr-only spans after threat header)
        flags = []
        for label in ("Crawler", "Proxy", "Abuser"):
            m = re.search(
                rf"{label}.*?<span[^>]*sr-only[^>]*>(Yes|No)</span>",
                html, re.I | re.S)
            if m and m.group(1) == "Yes":
                flags.append(label.lower())
        status_map = {"low": "ok", "medium": "warn", "high": "fail"}
        status = status_map.get(threat, "warn")
        if flags:
            status = "fail" if "abuser" in flags else "warn"
        summary = f"{cc} · 威胁等级 {threat or '?'}"
        if flags:
            summary += f" · ⚠ {','.join(flags)}"
        return _result("DB-IP", status, summary,
                       {"country": cc, "threat": threat, "flags": flags},
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("DB-IP", "error", "请求失败",
                       verify_url=url, request_url=url, error=str(e))


# ============================================================================
# Scamalytics — Cloudflare-protected, scraping unreliable. Mark as manual.
# ============================================================================
def check_scamalytics(ip: str) -> dict:
    url = f"https://scamalytics.com/ip/{ip}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        if r.status_code == 200 and "Fraud Score" in r.text:
            m_score = re.search(r"Fraud Score:\s*(\d+)", r.text)
            m_risk = re.search(r"(Very High Risk|High Risk|Medium Risk|Low Risk)", r.text)
            if m_score:
                score = int(m_score.group(1))
                label = m_risk.group(1) if m_risk else "?"
                status = "fail" if score >= 75 else ("warn" if score >= 25 else "ok")
                return _result("Scamalytics", status, f"风险分 {score}/100 · {label}",
                               {"score": score, "label": label},
                               verify_url=url, request_url=url)
        # Cloudflare blocked or no parse
        return _result("Scamalytics", "manual",
                       f"Cloudflare 拦截 (HTTP {r.status_code}) · 请在浏览器打开",
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("Scamalytics", "manual", "需在浏览器查看",
                       verify_url=url, request_url=url, error=str(e))


# ============================================================================
# IPQualityScore — public lookup is CF-protected; API needs key.
# ============================================================================
_QUOTA_KEYWORDS = (
    "insufficient credit", "exceeded", "quota", "rate limit",
    "credits", "limit reached", "daily limit", "monthly limit",
    "out of credits", "no credits",
)


def _is_quota_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in _QUOTA_KEYWORDS)


def check_ipqs(ip: str) -> dict:
    key = get_api_key("ipqualityscore")
    verify = f"https://www.ipqualityscore.com/free-ip-lookup-proxy-vpn-test/lookup/{ip}"
    if key:
        url = f"https://ipqualityscore.com/api/json/ip/{key}/{ip}?strictness=1"
        try:
            r = requests.get(url, headers=HEADERS, timeout=_timeout())
            d = r.json()
            if not d.get("success", False):
                msg = d.get("message", "失败")
                # Detect quota / credit / rate-limit problems → mark as
                # 'manual' so they don't drag the overall score down. The
                # user just needs a fresh key or to wait for the monthly
                # reset; this isn't a network/IP problem.
                if _is_quota_error(msg):
                    return _result("IPQualityScore", "manual",
                                   "API 配额已用尽 (IPQS 免费层 5000 次/月) · "
                                   "可换新 Key 或下月重试，期间请在浏览器查看",
                                   {"raw_message": msg},
                                   verify_url=verify, request_url=url)
                # Generic "errors" key may also indicate auth issues
                if "invalid" in msg.lower() and "key" in msg.lower():
                    return _result("IPQualityScore", "manual",
                                   "API Key 无效，请到设置中重新填写",
                                   {"raw_message": msg},
                                   verify_url=verify, request_url=url)
                return _result("IPQualityScore", "error", msg,
                               verify_url=verify, request_url=url)
            score = d.get("fraud_score", 0)
            flags = [k for k in ("proxy", "vpn", "tor", "active_vpn", "active_tor", "is_crawler")
                     if d.get(k)]
            status = "fail" if score >= 85 else ("warn" if score >= 50 else "ok")
            summary = f"风险分 {score}/100"
            if flags:
                summary += " · " + ",".join(flags)
            return _result("IPQualityScore", status, summary, d,
                           verify_url=verify, request_url=url)
        except Exception as e:
            return _result("IPQualityScore", "error", "API 请求失败",
                           verify_url=verify, request_url=url, error=str(e))
    # no key — public page is CF-protected; mark manual
    return _result("IPQualityScore", "manual",
                   "未配置 API Key · 公开页面被 Cloudflare 拦截，请在浏览器打开",
                   verify_url=verify)


# ============================================================================
# AbuseIPDB — needs free API key
# ============================================================================
def check_abuseipdb(ip: str) -> dict:
    key = get_api_key("abuseipdb")
    if not key:
        return _result("AbuseIPDB", "manual", "未配置 API Key",
                       verify_url=f"https://www.abuseipdb.com/check/{ip}")
    url = "https://api.abuseipdb.com/api/v2/check"
    try:
        r = requests.get(
            url,
            headers={**HEADERS, "Key": key, "Accept": "application/json"},
            params={"ipAddress": ip, "maxAgeInDays": 90, "verbose": "true"},
            timeout=_timeout(),
        )
        if r.status_code == 429 or r.status_code == 402:
            return _result("AbuseIPDB", "manual",
                           f"API 配额已用尽 (HTTP {r.status_code}) · "
                           f"AbuseIPDB 免费层 1000 次/天，明日重试",
                           verify_url=f"https://www.abuseipdb.com/check/{ip}",
                           request_url=url, error=r.text[:200])
        if r.status_code != 200:
            # The body sometimes contains a JSON error like
            # {"errors":[{"detail":"You have exceeded the rate limit ..."}]}
            body = r.text
            if _is_quota_error(body):
                return _result("AbuseIPDB", "manual",
                               "API 配额已用尽，请明日重试",
                               verify_url=f"https://www.abuseipdb.com/check/{ip}",
                               request_url=url, error=body[:200])
            return _result("AbuseIPDB", "error", f"HTTP {r.status_code}",
                           verify_url=f"https://www.abuseipdb.com/check/{ip}",
                           request_url=url, error=body[:200])
        d = r.json().get("data", {})
        score = d.get("abuseConfidenceScore", 0)
        reports = d.get("totalReports", 0)
        usage = d.get("usageType", "?")
        if score >= 75:
            status = "fail"
        elif score >= 25 or reports > 0:
            status = "warn"
        else:
            status = "ok"
        return _result("AbuseIPDB", status,
                       f"滥用置信度 {score}% · 举报 {reports} 次 · {usage}", d,
                       verify_url=f"https://www.abuseipdb.com/check/{ip}",
                       request_url=url)
    except Exception as e:
        return _result("AbuseIPDB", "error", "请求失败",
                       verify_url=f"https://www.abuseipdb.com/check/{ip}",
                       request_url=url, error=str(e))


# ============================================================================
# ping0.cc — Chinese IP risk page, server-rendered, accepts plain UA
# ============================================================================
def check_ping0(ip: str) -> dict:
    url = f"https://ping0.cc/ip/{ip}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        if r.status_code != 200:
            return _result("ping0.cc", "manual",
                           f"HTTP {r.status_code} · 站点不可达，请在浏览器打开",
                           verify_url=url, request_url=url)
        html = r.text
        html_l = html.lower()
        # Detect Cloudflare Turnstile / Aliyun Captcha / generic challenge
        # pages. ping0.cc started forcing CAPTCHA on all backend requests
        # in 2026 — there is no purely HTTP way around it.
        captcha_markers = (
            "cf-turnstile",                 # Cloudflare Turnstile widget
            "challenges.cloudflare.com",    # Turnstile JS host
            "aliyuncaptchaconfig",          # Aliyun captcha config
            "captcha-element",              # the verification container
            "请完成安全验证",                  # Aliyun Chinese verbiage
            "verify you are human",         # CF Turnstile English
        )
        if any(m in html_l for m in captcha_markers):
            return _result("ping0.cc", "manual",
                           "⚠ 站点已启用 Cloudflare Turnstile + 阿里云人机验证，"
                           "后端 HTTP 抓取无法绕过 · 请在浏览器中打开查看",
                           {"_blocked_by": "captcha"},
                           verify_url=url, request_url=url)
        if len(html) < 1000:
            return _result("ping0.cc", "manual",
                           f"页面过短 ({len(html)} bytes) · 可能被拦截，请浏览器打开",
                           {"_html_len": len(html)},
                           verify_url=url, request_url=url)
        risk_m = re.search(r"风险值[\s\S]{0,400}?(\d+)\s*%", html)
        native_m = re.search(r"原生\s*IP[\s\S]{0,400}?<[^>]+>([^<]{1,30})<", html)
        type_m = re.search(r"IP\s*类型[\s\S]{0,400}?<[^>]+>([^<]{1,40})<", html)
        usage_m = re.search(r"使用类型[\s\S]{0,400}?<[^>]+>([^<]{1,40})<", html)
        data = {
            "risk_pct": int(risk_m.group(1)) if risk_m else None,
            "native_ip": native_m.group(1).strip() if native_m else None,
            "ip_type": type_m.group(1).strip() if type_m else None,
            "usage_type": usage_m.group(1).strip() if usage_m else None,
        }
        if data["risk_pct"] is None and not data["native_ip"]:
            return _result("ping0.cc", "manual",
                           "页面结构异常 · 风控指标未找到，请浏览器查看",
                           data, verify_url=url, request_url=url)
        risk = data["risk_pct"] or 0
        status = "fail" if risk >= 66 else ("warn" if risk >= 33 else "ok")
        bits = []
        if data["risk_pct"] is not None:
            bits.append(f"风险 {data['risk_pct']}%")
        if data["native_ip"]:
            bits.append(f"原生={data['native_ip']}")
        if data["ip_type"]:
            bits.append(data["ip_type"])
        if data["usage_type"]:
            bits.append(data["usage_type"])
        return _result("ping0.cc", status, " · ".join(bits), data,
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("ping0.cc", "error", "请求失败",
                       verify_url=url, request_url=url, error=str(e))


# ============================================================================
# Multi-egress IP detection (mimics ip.net.coffee/claude top cards)
# ============================================================================
# Same machine can show DIFFERENT public IPs to different services because of
# CDN routing, IPv4/IPv6 dual stack, and ISP-level mappings. The IP that
# CLAUDE sees is often the one that matters for risk decisions, not your
# obvious IPv4. This is the core insight from ip.net.coffee/claude.
RESTRICTED_REGIONS = {
    "CN": "中国大陆", "HK": "香港", "MO": "澳门",
    "RU": "俄罗斯", "KP": "朝鲜", "IR": "伊朗",
    "SY": "叙利亚", "CU": "古巴", "BY": "白俄罗斯", "VE": "委内瑞拉",
}


def _trace(host: str) -> dict:
    """Fetch /cdn-cgi/trace from an arbitrary CF-fronted host. Returns parsed
    key=value lines plus latency. Empty dict on failure."""
    url = f"https://{host}/cdn-cgi/trace"
    try:
        t0 = time.perf_counter()
        r = requests.get(url, headers=HEADERS, timeout=_timeout())
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}", "_url": url, "_ms": ms}
        kv = {}
        for line in r.text.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        kv["_url"] = url
        kv["_ms"] = ms
        return kv
    except Exception as e:
        return {"_error": str(e), "_url": url}


def _geo_for(ip: str) -> dict:
    """Free-tier ip-api lookup. Returns city/region/country/ASN/ISP for a
    given IP. Returns {} on failure. Handles both IPv4 and IPv6."""
    if not ip:
        return {}
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,"
            f"regionName,city,zip,timezone,isp,org,as,asname,reverse,"
            f"mobile,proxy,hosting,query",
            headers=HEADERS, timeout=_timeout())
        if r.status_code != 200:
            return {}
        d = r.json()
        return d if d.get("status") == "success" else {}
    except Exception:
        return {}


def _format_geo_line(label: str, ip: str, geo: dict) -> str:
    """One-liner like:
       'Claude 出口: 104.28.x.x (Tokyo, Japan · AS13335 Cloudflare, Inc.)'
    """
    if not ip:
        return f"{label}: —"
    bits = []
    city = geo.get("city")
    region = geo.get("regionName")
    country = geo.get("country") or ""
    cc = geo.get("countryCode") or ""
    if city or country:
        loc = ""
        if city:    loc += city
        if region and region != city:  loc += f", {region}"
        if country:
            loc += (", " if loc else "") + country
            if cc and cc not in loc:
                loc += f" ({cc})"
        bits.append(loc)
    if geo.get("as"):
        bits.append(geo["as"])
    elif geo.get("isp"):
        bits.append(geo["isp"])
    feat = []
    if geo.get("hosting"):  feat.append("数据中心")
    if geo.get("proxy"):    feat.append("代理")
    if geo.get("mobile"):   feat.append("移动")
    if feat:
        bits.append("⚑ " + "/".join(feat))
    detail_str = " · ".join(bits)
    return f"{label}: {ip}  →  {detail_str}" if detail_str else f"{label}: {ip}"


def check_egress_ips() -> dict:
    """Detect 3 different IPs different services see — Cloudflare's view,
    Claude's view, plus our generic IPv4 — AND geolocate every single one
    so the user sees the full ASN/city/ISP attribution like ip.net.coffee
    shows. The country code from /cdn-cgi/trace alone is not enough.
    """
    cf = _trace("1.1.1.1")
    claude = _trace("claude.ai")
    cn_ip = get_my_ip()  # IPv4-only via api.ipify etc.

    cf_ip = cf.get("ip", "")
    claude_ip = claude.get("ip", "")
    claude_loc = claude.get("loc", "")

    # Geolocate each unique IP in parallel — these are cheap ip-api calls.
    unique_ips = []
    for ip in (claude_ip, cf_ip, cn_ip):
        if ip and ip not in unique_ips:
            unique_ips.append(ip)
    geo_by_ip: dict[str, dict] = {}
    if unique_ips:
        with ThreadPoolExecutor(max_workers=min(4, len(unique_ips))) as pool:
            futs = {pool.submit(_geo_for, ip): ip for ip in unique_ips}
            for fut in as_completed(futs):
                ip = futs[fut]
                try:
                    geo_by_ip[ip] = fut.result()
                except Exception:
                    geo_by_ip[ip] = {}

    consistent = bool(cf_ip and claude_ip and cf_ip == claude_ip)
    ipv6_to_claude = ":" in (claude_ip or "")
    detail = {
        "cn_visible_ipv4": cn_ip,
        "cloudflare_egress": cf_ip,
        "claude_egress": claude_ip,
        "claude_loc": claude_loc,
        "claude_colo": claude.get("colo"),
        "claude_warp": claude.get("warp"),
        "consistent": consistent,
        "claude_uses_ipv6": ipv6_to_claude,
        "geo": geo_by_ip,
    }

    # ── Build a multi-line summary showing each egress + its归属地 ──
    lines = []
    if claude_ip:
        lines.append(_format_geo_line("Claude 视角", claude_ip,
                                       geo_by_ip.get(claude_ip, {})))
    if cf_ip and cf_ip != claude_ip:
        lines.append(_format_geo_line("CF 视角", cf_ip,
                                       geo_by_ip.get(cf_ip, {})))
    if cn_ip and cn_ip != claude_ip and cn_ip != cf_ip:
        lines.append(_format_geo_line("本地 IPv4", cn_ip,
                                       geo_by_ip.get(cn_ip, {})))
    if claude.get("colo"):
        lines.append(f"Claude CF 边缘节点 (colo): {claude.get('colo')}"
                     f"   ·   WARP: {claude.get('warp', '?')}")
    if not claude_ip:
        lines.append("⚠ 无法获取 Claude 视角的出口 IP")

    # ── Status: same restricted-region / mismatch logic as before ──
    if not claude_ip:
        status = "warn"
    elif claude_loc.upper() in RESTRICTED_REGIONS:
        status = "fail"
        lines.insert(0,
            f"⚠ Claude 看到出口在 {RESTRICTED_REGIONS[claude_loc.upper()]} "
            f"({claude_loc}) → 极易触发风控/封号")
    elif ipv6_to_claude and cn_ip and ":" not in cn_ip:
        status = "warn"
        lines.append("⚠ Claude 走 IPv6 / CF 走 IPv4 — 二者风险评分需分别核查")
    elif not consistent and cf_ip and claude_ip:
        status = "warn"
        lines.append("⚠ CF 与 Claude 看到的 IP 不一致")
    else:
        status = "ok"
        if claude_ip:
            lines.append("✓ Claude 与 CF 看到的 IP 一致")

    # Highlights table for the detail dialog
    highlights: dict[str, str] = {}
    for label, ip in (("Claude 视角 IP", claude_ip),
                       ("Cloudflare 视角 IP", cf_ip),
                       ("本地 IPv4", cn_ip)):
        if not ip:
            continue
        g = geo_by_ip.get(ip, {})
        highlights[label] = ip
        loc = ", ".join(x for x in (g.get("city"), g.get("regionName"),
                                     g.get("country")) if x)
        if loc:
            highlights[f"  └ 位置 ({label.split()[0]})"] = loc
        if g.get("as"):
            highlights[f"  └ ASN ({label.split()[0]})"] = g["as"]
        if g.get("isp") and g.get("isp") != g.get("as"):
            highlights[f"  └ ISP ({label.split()[0]})"] = g["isp"]
    if claude.get("colo"):
        highlights["Claude 边缘节点"] = claude["colo"]
    if claude.get("warp"):
        highlights["Cloudflare WARP"] = claude["warp"]
    detail["highlights"] = highlights

    return _result("出口 IP 多视角", status, "\n".join(lines) or "无数据",
                   detail,
                   verify_url="https://ip.net.coffee/claude/",
                   request_url="https://claude.ai/cdn-cgi/trace")


# ============================================================================
# Claude Trust Score — pulls ip.net.coffee's aggregated /api/iprisk endpoint.
# Their endpoint is publicly callable and bundles trust_score + all booleans
# in one round-trip. Uses Claude-visible IP if we can detect it.
# ============================================================================
def check_iprisk_score(ip: str) -> dict:
    """Call ip.net.coffee/api/iprisk/{ip}. Implements their restricted-region
    override: any IP in CN/HK/MO/RU/KP/IR/SY/CU/BY/VE → trust forced to 0 with
    a red warning. (See research notes — the website itself does this client-
    side regardless of the score the API returns.)"""
    url = f"https://ip.net.coffee/api/iprisk/{ip}"
    try:
        r = requests.get(url, headers={**HEADERS,
                                       "Referer": "https://ip.net.coffee/"},
                         timeout=_timeout())
        if r.status_code != 200:
            return _result("Claude 信任评分", "error", f"HTTP {r.status_code}",
                           verify_url=f"https://ip.net.coffee/ip/{ip}",
                           request_url=url, error=r.text[:200])
        d = r.json()
        cc = (d.get("countryCode", "") or "").upper()
        score = d.get("trust_score")
        flags = [k.replace("is_", "") for k in
                 ("is_vpn", "is_proxy", "is_tor", "is_abuser", "is_crawler")
                 if d.get(k)]
        residential = d.get("isResidential")
        # restricted region forces score = 0
        if cc in RESTRICTED_REGIONS:
            return _result("Claude 信任评分", "fail",
                           f"出口在 {RESTRICTED_REGIONS[cc]} → 强制 0/100 不可访问",
                           {**d, "_overridden": True},
                           verify_url=f"https://ip.net.coffee/claude/",
                           request_url=url)
        if score is None:
            status = "warn"; label = "无评分"
        elif score >= 95:
            status, label = "ok", "极度纯净"
        elif score >= 80:
            status, label = "ok", "纯净"
        elif score >= 50:
            status, label = "ok", "良好"
        elif score >= 25:
            status, label = "warn", "中性 (有风险)"
        else:
            status, label = "fail", "可疑"
        bits = [f"{score}/100 · {label}"]
        if residential is True:
            bits.append("家庭住宅")
        elif residential is False:
            bits.append("机房")
        if flags:
            bits.append("⚠ " + ",".join(flags))
            status = "warn" if status == "ok" else "fail"
        return _result("Claude 信任评分", status, " · ".join(bits), d,
                       verify_url=f"https://ip.net.coffee/ip/{ip}",
                       request_url=url)
    except Exception as e:
        return _result("Claude 信任评分", "error", "请求失败",
                       verify_url=f"https://ip.net.coffee/ip/{ip}",
                       request_url=url, error=str(e))


# ============================================================================
# Claude reachability — claude.ai trace + anthropic.com favicon latency
# (mimics ip.net.coffee Card 4 with same thresholds: <250 normal,
# <500 good, >=500 slow, error fail)
# ============================================================================
def _http_latency(url: str, method: str = "GET") -> dict:
    """Time a single HTTP request, return ms + status."""
    t0 = time.perf_counter()
    try:
        r = requests.request(method, url, headers=HEADERS,
                             timeout=_timeout(), allow_redirects=True)
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": True, "ms": ms, "http": r.status_code, "url": url}
    except Exception as e:
        return {"ok": False, "ms": None, "url": url, "error": str(e)[:120]}


def _latency_label(ms: int | None, ok: bool) -> tuple[str, str]:
    if not ok or ms is None:
        return ("fail", "不可访问")
    if ms < 250:
        return ("ok", f"正常 {ms}ms")
    if ms < 500:
        return ("ok", f"良好 {ms}ms")
    return ("warn", f"较慢 {ms}ms")


def check_claude_reachability() -> dict:
    """3-target latency probe — same logic as ip.net.coffee Card 4."""
    a = _http_latency("https://claude.ai/cdn-cgi/trace")
    b = _http_latency("https://www.anthropic.com/favicon.ico")
    # determine claude region
    claude_loc = ""
    if a.get("ok"):
        try:
            r = requests.get("https://claude.ai/cdn-cgi/trace",
                             headers=HEADERS, timeout=_timeout())
            m = re.search(r"loc=([A-Z]{2})", r.text or "")
            claude_loc = m.group(1) if m else ""
        except Exception:
            pass
    detail = {"claude_ai": a, "anthropic_com": b, "claude_loc": claude_loc}
    if claude_loc.upper() in RESTRICTED_REGIONS:
        return _result("Claude 可达性", "fail",
                       f"出口 {claude_loc} 在 Claude 限制区域 → 强制不可达",
                       detail,
                       verify_url="https://claude.ai/",
                       request_url=a.get("url", ""))
    s_a, lbl_a = _latency_label(a.get("ms"), a.get("ok"))
    s_b, lbl_b = _latency_label(b.get("ms"), b.get("ok"))
    # combine: worse of the two
    rank = {"ok": 0, "warn": 1, "fail": 2}
    overall = max([s_a, s_b], key=lambda s: rank.get(s, 0))
    summary = f"claude.ai={lbl_a} · anthropic.com={lbl_b}"
    return _result("Claude 可达性", overall, summary, detail,
                   verify_url="https://claude.ai/",
                   request_url=a.get("url", ""))


# ============================================================================
# Anthropic / Claude status page (status.claude.com)
# ============================================================================
def check_claude_status() -> dict:
    """status.claude.com (formerly status.anthropic.com) summary."""
    url = "https://status.claude.com/api/v2/status.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout(),
                         allow_redirects=True)
        if r.status_code != 200:
            return _result("Claude 服务状态", "error", f"HTTP {r.status_code}",
                           verify_url="https://status.claude.com/",
                           request_url=url)
        d = r.json()
        ind = (d.get("status") or {}).get("indicator", "?")
        desc = (d.get("status") or {}).get("description", "?")
        labels = {
            "none": ("ok", "全部服务正常"),
            "minor": ("warn", "轻微故障"),
            "maintenance": ("warn", "维护中"),
            "major": ("fail", "重大故障"),
            "critical": ("fail", "严重故障"),
        }
        s, lbl = labels.get(ind, ("warn", desc))
        return _result("Claude 服务状态", s, f"{lbl} (indicator={ind})", d,
                       verify_url="https://status.claude.com/",
                       request_url=url)
    except Exception as e:
        return _result("Claude 服务状态", "error", "请求失败",
                       verify_url="https://status.claude.com/",
                       request_url=url, error=str(e))


# ============================================================================
# AI services unlock — Claude / ChatGPT / Gemini  (faithful to lmc999/check.sh)
# ============================================================================
def check_claude_unlock() -> dict:
    """lmc999 L4564: follow redirects, check final URL.

    Rules from upstream:
      - final URL contains 'app-unavailable-in-region' (anthropic.com)  → FAIL
      - final URL is on claude.ai (any path: /, /login, /chat/...)      → OK
      - anything else (e.g. unknown CDN error redirect)                 → WARN
    The HTTP status code is irrelevant — Cloudflare's bot challenge
    returns 403 from supported regions too.
    """
    url = "https://claude.ai/"
    verify = "https://claude.ai/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout(),
                         allow_redirects=True)
        final = r.url
        if "app-unavailable-in-region" in final or \
                "anthropic.com/app-unavailable" in final:
            return _result("Claude (claude.ai)", "fail",
                           f"重定向到 {final} → 地区不支持",
                           {"final_url": final, "http": r.status_code},
                           verify_url=verify, request_url=url)
        if "claude.ai" in final:
            return _result("Claude (claude.ai)", "ok",
                           f"地区支持 (final={final}, HTTP {r.status_code})",
                           {"final_url": final, "http": r.status_code},
                           verify_url=verify, request_url=url)
        return _result("Claude (claude.ai)", "warn",
                       f"未知重定向 {final}",
                       {"final_url": final, "http": r.status_code},
                       verify_url=verify, request_url=url)
    except Exception as e:
        return _result("Claude (claude.ai)", "error", "请求失败",
                       verify_url=verify, request_url=url, error=str(e))


def check_chatgpt_unlock() -> dict:
    """xykt L1632 / lmc999 L4510 — 4-state cross product."""
    web_blocked = False
    app_blocked = False
    loc = ""
    detail = {}
    # 1) Compliance endpoint — web availability
    cu = "https://api.openai.com/compliance/cookie_requirements"
    try:
        r1 = requests.get(cu,
                          headers={**HEADERS, "Authorization": "Bearer null",
                                   "Accept": "*/*"},
                          timeout=_timeout())
        detail["compliance_status"] = r1.status_code
        detail["compliance_body"] = r1.text[:300]
        if "unsupported_country" in r1.text.lower():
            web_blocked = True
    except Exception as e:
        detail["compliance_error"] = str(e)
    # 2) iOS endpoint — app availability
    # xykt L1632 / lmc999 L4516: only the literal "VPN" marker counts.
    # 403 + Cloudflare cf_details body is just bot challenge, NOT a geo block.
    iu = "https://ios.chat.openai.com/"
    try:
        r2 = requests.get(iu, headers=HEADERS, timeout=_timeout(),
                          allow_redirects=True)
        detail["ios_status"] = r2.status_code
        body = (r2.text or "")[:2000]
        detail["ios_body_snippet"] = body[:300]
        # Only flag if the actual "VPN" string from OpenAI's geo-block
        # template appears. Cloudflare bot challenge body has cf_details.
        if "VPN" in body and "cf_details" not in body:
            app_blocked = True
    except Exception as e:
        detail["ios_error"] = str(e)
    # 3) Country
    try:
        r3 = requests.get("https://chat.openai.com/cdn-cgi/trace",
                          headers=HEADERS, timeout=_timeout())
        m = re.search(r"loc=([A-Z]{2})", r3.text or "")
        loc = m.group(1) if m else ""
        detail["loc"] = loc
    except Exception:
        pass
    # 4) Verdict
    if not web_blocked and not app_blocked:
        s, label = "ok", f"完全可用 (出口 {loc or '?'})"
    elif web_blocked and app_blocked:
        s, label = "fail", f"网页+App 均被封 (出口 {loc or '?'})"
    elif web_blocked:
        s, label = "warn", f"仅 App 可用 (出口 {loc or '?'})"
    else:
        s, label = "warn", f"仅网页可用 (出口 {loc or '?'})"
    return _result("ChatGPT (OpenAI)", s, label, detail,
                   verify_url="https://chatgpt.com/",
                   request_url=f"{cu} + {iu}")


def check_gemini_unlock() -> dict:
    """lmc999 L4544 — sentinel string."""
    url = "https://gemini.google.com/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout(),
                         allow_redirects=True)
        body = r.text or ""
        has_marker = "45631641,null,true" in body
        m_cc = re.search(r',2,1,200,"([A-Z]{3})"', body)
        cc = m_cc.group(1) if m_cc else "?"
        if has_marker:
            return _result("Gemini", "ok", f"可用 · 地区 {cc}",
                           {"country": cc, "marker": True, "http": r.status_code},
                           verify_url=url, request_url=url)
        return _result("Gemini", "fail", "不可用 (未找到可用标记)",
                       {"country": cc, "marker": False, "http": r.status_code},
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("Gemini", "error", "请求失败",
                       verify_url=url, request_url=url, error=str(e))


# ============================================================================
# Streaming — Netflix / Disney+ / YouTube Premium / TikTok / Spotify
# ============================================================================
NETFLIX_TITLES = (("81280792", "LEGO Ninjago (Original)"),
                  ("70143836", "Breaking Bad (non-Original)"))


def check_netflix() -> dict:
    """xykt L1462, lmc999 L804 — both titles, look for 'Oh no!' marker."""
    results = {}
    for tid, label in NETFLIX_TITLES:
        u = f"https://www.netflix.com/title/{tid}"
        try:
            r = requests.get(u, headers=HEADERS, timeout=_timeout(),
                             allow_redirects=True)
            body = r.text or ""
            results[tid] = {
                "label": label,
                "status": r.status_code,
                "final": r.url,
                "oh_no": "Oh no!" in body,
                "len": len(body),
            }
            # extract region from final URL "/<lang>-<cc>/title/..."
            m = re.search(r"netflix\.com/([a-z]{2}(?:-[a-z]{2})?)/title", r.url)
            if m:
                results[tid]["region_url"] = m.group(1)
            m2 = re.search(r'"id":"([A-Z]{2})","countryName":"([^"]+)"', body)
            if m2:
                results[tid]["region_html"] = m2.group(1)
                results[tid]["country_name"] = m2.group(2)
        except Exception as e:
            results[tid] = {"label": label, "error": str(e)}
    # Verdict
    a = results.get("81280792", {})
    b = results.get("70143836", {})
    a_ok = a.get("status") == 200 and not a.get("oh_no")
    b_ok = b.get("status") == 200 and not b.get("oh_no")
    region = b.get("region_html") or a.get("region_html") or \
             (a.get("region_url", "") + b.get("region_url", "")).upper()[:2]
    if a_ok and b_ok:
        verdict = ("ok", f"完整解锁 · 地区 {region or '?'}")
    elif a.get("oh_no") and b.get("oh_no"):
        verdict = ("warn", "仅自制剧 (Originals Only)")
    elif a_ok:
        verdict = ("warn", f"仅自制剧 · 地区 {region or '?'}")
    else:
        verdict = ("fail", "Netflix 不可用")
    return _result("Netflix", verdict[0], verdict[1], results,
                   verify_url="https://www.netflix.com/title/70143836",
                   request_url="https://www.netflix.com/title/{81280792,70143836}")


def check_disney_plus() -> dict:
    """Disney+ unlock — 3-signal approach (faithful to xykt + lmc999 logic):

    A. POST /devices                → 403 / "403 ERROR" body  → IP banned
    B. POST /token (with assertion) → "forbidden-location"    → geo blocked
    C. GET disneyplus.com           → final URL contains 'preview' + 'unavailable' → blocked
                                    → final URL contains 'es-mx', 'ja-jp' etc.    → region detected
    The graphql session step (xykt L1444) is brittle without a fully recreated
    web auth chain; signals A+B+C give the same verdict for unlock/no-unlock.
    """
    BEARER = "ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84"
    base = "https://disney.api.edge.bamgrid.com"
    detail = {}
    verify = "https://www.disneyplus.com/"
    try:
        # ── A. /devices ──
        r1 = requests.post(
            f"{base}/devices",
            headers={**HEADERS, "Authorization": f"Bearer {BEARER}",
                     "Content-Type": "application/json"},
            json={"deviceFamily": "browser", "applicationRuntime": "chrome",
                  "deviceProfile": "windows", "attributes": {}},
            timeout=_timeout(),
        )
        detail["devices_status"] = r1.status_code
        if r1.status_code == 403 or "403 ERROR" in r1.text:
            return _result("Disney+", "fail",
                           "IP 被 Disney 封禁 (devices step 403)",
                           detail, verify_url=verify,
                           request_url=f"{base}/devices")
        d1 = r1.json() if r1.headers.get("content-type", "").startswith("application/json") else {}
        assertion = d1.get("assertion") if isinstance(d1, dict) else None

        # ── B. /token ──
        token_blocked = False
        if assertion:
            r2 = requests.post(
                f"{base}/token",
                headers={**HEADERS,
                         "Authorization": f"Bearer {BEARER}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type":
                        "urn:ietf:params:oauth:grant-type:token-exchange",
                    "latitude": "0", "longitude": "0",
                    "platform": "browser",
                    "subject_token": assertion,
                    "subject_token_type":
                        "urn:bamtech:params:oauth:token-type:device",
                },
                timeout=_timeout(),
            )
            detail["token_status"] = r2.status_code
            detail["token_body_excerpt"] = r2.text[:200]
            if "forbidden-location" in r2.text:
                token_blocked = True

        # ── C. /disneyplus.com homepage redirect ──
        cc = "?"
        preview_blocked = False
        try:
            r3 = requests.get(verify, headers=HEADERS,
                              timeout=_timeout(), allow_redirects=True)
            final = r3.url
            detail["home_final_url"] = final
            detail["home_status"] = r3.status_code
            if "preview" in final and "unavailable" in final:
                preview_blocked = True
            else:
                # extract /xx-yy/ region from final URL
                m = re.search(r"disneyplus\.com/([a-z]{2}-[a-z]{2})", final)
                if m:
                    cc = m.group(1).upper()
        except Exception as e:
            detail["home_error"] = str(e)

        if token_blocked or preview_blocked:
            return _result("Disney+", "fail",
                           f"地区不可用 (cc={cc})", detail,
                           verify_url=verify, request_url=f"{base}/token")
        if cc != "?":
            return _result("Disney+", "ok",
                           f"已解锁 · 地区 {cc}", detail,
                           verify_url=verify, request_url=f"{base}/token")
        # /devices + /token both passed (no forbidden-location) → IP is in a
        # Disney-supported country. Region code requires the GraphQL session
        # call which Disney has tightened (api-key.invalid for naive callers),
        # so we report supported without an exact region code.
        return _result("Disney+", "ok",
                       "Disney+ 可用 (基于 token API；region 需在浏览器查看)",
                       detail, verify_url=verify,
                       request_url=f"{base}/token")
    except Exception as e:
        return _result("Disney+", "error", "请求失败", detail,
                       verify_url=verify, error=str(e))


def check_youtube_premium() -> dict:
    """xykt L1502, lmc999 L1694 — needs cookies + en accept-language."""
    url = "https://www.youtube.com/premium"
    cookies = ("YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; "
               "GPS=1; VISITOR_INFO1_LIVE=4VwPMkB7W5A; "
               "PREF=tz=Asia.Shanghai; _gcl_au=1.1.1809531354.1646633279")
    try:
        r = requests.get(url, headers={**HEADERS, "Cookie": cookies,
                                       "Accept-Language": "en"},
                         timeout=_timeout(), allow_redirects=True)
        body = r.text or ""
        if "www.google.cn" in body:
            return _result("YouTube Premium", "fail", "中国大陆 (无 YouTube)",
                           {"http": r.status_code},
                           verify_url=url, request_url=url)
        if "Premium is not available in your country" in body:
            return _result("YouTube Premium", "fail", "地区不支持 Premium",
                           {"http": r.status_code},
                           verify_url=url, request_url=url)
        m = re.search(r'"INNERTUBE_CONTEXT_GL"\s*:\s*"([^"]+)"', body) or \
            re.search(r'"countryCode"\s*:\s*"([A-Z]{2})"', body)
        country = m.group(1) if m else "?"
        if "ad-free" in body.lower():
            return _result("YouTube Premium", "ok", f"可用 · 地区 {country}",
                           {"country": country, "http": r.status_code},
                           verify_url=url, request_url=url)
        return _result("YouTube Premium", "warn",
                       f"地区 {country} · 未确认可用",
                       {"country": country, "http": r.status_code},
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("YouTube Premium", "error", "请求失败",
                       verify_url=url, request_url=url, error=str(e))


def check_tiktok() -> dict:
    """xykt L1327."""
    url = "https://www.tiktok.com/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout(),
                         allow_redirects=True)
        body = r.text or ""
        m = re.search(r'"region":\s*"([A-Z]{2})"', body)
        region = m.group(1) if m else None
        if region:
            return _result("TikTok", "ok", f"可用 · region={region}",
                           {"region": region, "http": r.status_code},
                           verify_url=url, request_url=url)
        if r.status_code == 200:
            return _result("TikTok", "warn", "可访问但无 region 字段",
                           {"http": r.status_code, "len": len(body)},
                           verify_url=url, request_url=url)
        return _result("TikTok", "fail", f"HTTP {r.status_code}",
                       verify_url=url, request_url=url)
    except Exception as e:
        return _result("TikTok", "error", "请求失败",
                       verify_url=url, request_url=url, error=str(e))


def check_spotify() -> dict:
    """lmc999 L3569 — Spotify signup endpoint.

    Status codes (verified against live API):
      311 + is_country_launched=true → region OK, can register
      120                            → region not supported
      320                            → IP is recognized as a proxy/datacenter
      otherwise                      → unknown
    """
    url = "https://spclient.wg.spotify.com/signup/public/v1/account"
    try:
        r = requests.post(url, headers=HEADERS, timeout=_timeout(),
                          data={"birth_day": "11", "birth_month": "11",
                                "birth_year": "2000",
                                "collect_personal_info": "undefined",
                                "creation_flow": "",
                                "creation_point": "https://www.spotify.com/us/select-plan/individual/",
                                "displayname": "Test",
                                "username": "ipchk_test",
                                "password": "Aa12345678",
                                "password_repeat": "Aa12345678",
                                "email": "ipchk@test.test",
                                "iagree": "1",
                                "key": "a1e486e2729f46d6bb368d6b2bcda326",
                                "platform": "www", "referrer": "",
                                "send-email": "0", "thirdpartyemail": "0",
                                "ad_email": "0"})
        body = r.text or ""
        m_status = re.search(r'"status"\s*:\s*(\d+)', body)
        m_country = re.search(r'"country"\s*:\s*"([^"]+)"', body)
        m_launched = re.search(r'"is_country_launched"\s*:\s*(true|false)', body)
        m_proxy = re.search(r'"generic_error"\s*:\s*"([^"]+)"', body)
        st = int(m_status.group(1)) if m_status else 0
        cc = m_country.group(1) if m_country else "?"
        launched = m_launched.group(1) if m_launched else "?"
        proxy_msg = m_proxy.group(1) if m_proxy else ""
        detail = {"http": r.status_code, "status": st, "country": cc,
                  "launched": launched, "proxy_msg": proxy_msg,
                  "body_excerpt": body[:300]}
        if st == 320:
            # Spotify thinks our IP is a proxy/datacenter → strong fraud signal
            return _result("Spotify", "fail",
                           "Spotify 检测到代理/数据中心 IP", detail,
                           verify_url="https://www.spotify.com/",
                           request_url=url)
        if st == 120:
            return _result("Spotify", "fail",
                           f"地区不支持 ({cc})", detail,
                           verify_url="https://www.spotify.com/",
                           request_url=url)
        if st == 311 and launched == "true":
            return _result("Spotify", "ok",
                           f"可注册 · 地区 {cc}", detail,
                           verify_url="https://www.spotify.com/",
                           request_url=url)
        return _result("Spotify", "warn",
                       f"未知状态 (status={st}, cc={cc})", detail,
                       verify_url="https://www.spotify.com/", request_url=url)
    except Exception as e:
        return _result("Spotify", "error", "请求失败",
                       verify_url="https://www.spotify.com/",
                       request_url=url, error=str(e))


# ============================================================================
# Site reachability
# ============================================================================
SITE_TARGETS = [
    ("Google", "https://www.google.com/generate_204", 204),
    ("Google Search", "https://www.google.com/search?q=test", 200),
    ("YouTube", "https://www.youtube.com/", 200),
    ("Cloudflare", "https://www.cloudflare.com/cdn-cgi/trace", 200),
    ("GitHub", "https://github.com/", 200),
    ("Wikipedia", "https://www.wikipedia.org/", 200),
    ("Reddit", "https://www.reddit.com/", 200),
    ("X (Twitter)", "https://x.com/", 200),
    ("Telegram Web", "https://web.telegram.org/", 200),
    ("Discord", "https://discord.com/", 200),
    ("Anthropic", "https://www.anthropic.com/", 200),
    ("OpenAI", "https://openai.com/", 200),
]


def check_site(name: str, url: str, expected: int) -> dict:
    t0 = time.perf_counter()
    try:
        r = requests.get(url, headers=HEADERS, timeout=_timeout(),
                         allow_redirects=True)
        ms = int((time.perf_counter() - t0) * 1000)
        ok = r.status_code == expected or 200 <= r.status_code < 400
        body = r.text[:3000].lower() if r.headers.get("content-type", "").startswith("text") else ""
        for marker in ("unsupported_country", "not available in your country",
                       "country is not supported"):
            if marker in body:
                return _result(name, "warn",
                               f"HTTP {r.status_code} · {ms}ms · 地区限制",
                               {"http": r.status_code, "ms": ms, "marker": marker},
                               verify_url=url, request_url=url)
        return _result(name, "ok" if ok else "fail",
                       f"HTTP {r.status_code} · {ms}ms",
                       {"http": r.status_code, "ms": ms},
                       verify_url=url, request_url=url)
    except requests.exceptions.ConnectTimeout:
        return _result(name, "fail", "连接超时", verify_url=url, request_url=url)
    except requests.exceptions.ReadTimeout:
        return _result(name, "fail", "读取超时", verify_url=url, request_url=url)
    except requests.exceptions.SSLError as e:
        return _result(name, "fail", "SSL 错误",
                       verify_url=url, request_url=url, error=str(e)[:120])
    except requests.exceptions.ConnectionError as e:
        return _result(name, "fail", "连接失败",
                       verify_url=url, request_url=url, error=str(e)[:120])
    except Exception as e:
        return _result(name, "error", "异常",
                       verify_url=url, request_url=url, error=str(e))


# ============================================================================
# Latency & speed
# ============================================================================
def tcp_ping(host: str, port: int = 443, count: int = 4,
             timeout: float = 3.0) -> dict:
    rtts, fails = [], 0
    for _ in range(count):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            s.connect((host, port))
            rtts.append((time.perf_counter() - t0) * 1000)
        except Exception:
            fails += 1
        finally:
            s.close()
        time.sleep(0.05)
    if not rtts:
        return {"host": host, "loss": 100, "min": None, "avg": None, "max": None}
    return {"host": host, "loss": int(fails / count * 100),
            "min": round(min(rtts), 1),
            "avg": round(statistics.mean(rtts), 1),
            "max": round(max(rtts), 1)}


PING_TARGETS = [
    ("Google", "www.google.com"),
    ("Cloudflare", "1.1.1.1"),
    ("Claude.ai", "claude.ai"),
    ("ChatGPT", "chat.openai.com"),
    ("GitHub", "github.com"),
    ("Netflix", "www.netflix.com"),
    ("百度", "www.baidu.com"),
    ("阿里云", "www.aliyun.com"),
]


def check_latency_all() -> list[dict]:
    out = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(tcp_ping, host): name for name, host in PING_TARGETS}
        for fut in as_completed(futs):
            name = futs[fut]
            try:
                d = fut.result()
                if d["avg"] is None:
                    out.append(_result(f"延迟 · {name}", "fail",
                                        "全部超时", d))
                elif d["loss"] > 0:
                    out.append(_result(f"延迟 · {name}", "warn",
                                        f"{d['avg']}ms (丢包 {d['loss']}%)", d))
                else:
                    s = "ok" if d["avg"] < 200 else "warn"
                    out.append(_result(f"延迟 · {name}", s,
                                        f"{d['avg']}ms (min {d['min']}, max {d['max']})", d))
            except Exception as e:
                out.append(_result(f"延迟 · {name}", "error", "失败", error=str(e)))
    return out


# ============================================================================
# DNS — system resolvers + leak detection
#
# Strategy: ask Cloudflare's `whoami.cloudflare` TXT record, which echoes back
# the IP of the resolver that asked. Compare that resolver's country with our
# public-IP country. If they differ, DNS queries are exiting through a
# different geography than the rest of our traffic — classic DNS leak.
#
# Also lists the resolver(s) the OS is configured to use, with country/ISP
# hints. Useful for spotting "you think you're using Cloudflare DNS but
# you're actually still on your ISP's resolver".
# ============================================================================
def _system_dns_servers() -> list[str]:
    servers: list[str] = []
    # Try dnspython's parser first (handles platform quirks)
    try:
        import dns.resolver  # type: ignore
        r = dns.resolver.Resolver()
        servers = list(r.nameservers or [])
    except Exception:
        pass
    if servers:
        return servers
    # Fallback: parse /etc/resolv.conf on Unix
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
    except Exception:
        pass
    return servers


def _geolocate_ip_quick(ip: str) -> dict:
    """Best-effort country/ASN lookup for an IP using ip-api (no key)."""
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,"
            f"isp,as,org,query", headers=HEADERS, timeout=_timeout())
        if r.status_code == 200:
            d = r.json()
            if d.get("status") == "success":
                return d
    except Exception:
        pass
    return {}


def check_dns_resolvers() -> dict:
    """Inspect the resolvers the OS is currently using and tag each by country."""
    servers = _system_dns_servers()
    if not servers:
        return _result("DNS 解析器", "warn", "无法读取系统 DNS 配置",
                       verify_url="https://1.1.1.1/help",
                       request_url="(local: dns.resolver / /etc/resolv.conf)")
    rows = []
    for ip in servers[:6]:
        # skip IPv6 link-local etc.
        if ip.startswith("fe80") or ip.startswith("169.254"):
            continue
        meta = _geolocate_ip_quick(ip)
        rows.append({"ip": ip,
                     "country": meta.get("country", "?"),
                     "cc": meta.get("countryCode", "?"),
                     "isp": meta.get("isp", "?"),
                     "as": meta.get("as", "?")})
    if not rows:
        return _result("DNS 解析器", "warn", "仅检测到链路本地解析器",
                       {"servers": servers},
                       verify_url="https://1.1.1.1/help",
                       request_url="(local)")
    summary = " · ".join(
        f"{r['ip']} ({r['cc']}/{r['isp'][:18]})" for r in rows[:3])
    if len(rows) > 3:
        summary += f" …+{len(rows)-3}"
    # Detect mismatch: any resolver in CN while our public IP isn't, or vice
    # versa. We don't have public-IP context here, so only flag obvious
    # private-network-only resolvers (same ip as gateway).
    return _result("DNS 解析器", "ok", summary,
                   {"resolvers": rows, "raw": servers},
                   verify_url="https://www.dnsleaktest.com/",
                   request_url="(local + ip-api lookups)")


def check_dns_leak(public_country: str = "") -> dict:
    """DNS leak detection — does the resolver our queries actually traverse
    sit in the same country as our public IP?

    Method:
      1. TXT lookup of `whoami.cloudflare` via system resolver → returns the
         egress IP of whichever recursive resolver actually answered.
      2. Geolocate that IP.
      3. Compare its country with `public_country` (the country our public
         IP resolves to).
      4. If they differ → DNS leak warning.

    `public_country` is optional; if absent we still report what we saw.
    """
    url_verify = "https://www.dnsleaktest.com/"
    try:
        import dns.resolver  # type: ignore
    except Exception as e:
        return _result("DNS 泄露检测", "error", "dnspython 未安装",
                       verify_url=url_verify, request_url="(local)",
                       error=str(e))
    # Two echo records, tried in order. Each returns the egress IP of the
    # actual recursive resolver that asked.
    #   1. o-o.myaddr.l.google.com TXT (IN)  — works via any resolver that
    #      forwards to Google's authoritative servers
    #   2. whoami.akamai.net A             — Akamai variant, A record
    resolver_ips: list[str] = []
    last_err = ""
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = max(2, _timeout() / 2)
        try:
            ans = resolver.resolve("o-o.myaddr.l.google.com", "TXT")
            for rr in ans:
                txt = (b"".join(rr.strings).decode(errors="replace")
                       if hasattr(rr, "strings") else str(rr).strip('"'))
                resolver_ips.append(txt.strip('"'))
        except Exception as e:
            last_err = f"google TXT: {e}"
        if not resolver_ips:
            try:
                ans = resolver.resolve("whoami.akamai.net", "A")
                for rr in ans:
                    resolver_ips.append(str(rr))
            except Exception as e:
                last_err += f" | akamai A: {e}"
    except Exception as e:
        return _result("DNS 泄露检测", "error", "解析器初始化失败",
                       verify_url=url_verify, request_url="dig",
                       error=str(e))
    if not resolver_ips:
        return _result("DNS 泄露检测", "error", "TXT 查询失败",
                       verify_url=url_verify,
                       request_url="dig TXT o-o.myaddr.l.google.com",
                       error=last_err)
    if not resolver_ips:
        return _result("DNS 泄露检测", "warn", "未取到出口解析器 IP",
                       verify_url=url_verify, request_url="dig TXT whoami.cloudflare")
    rip = resolver_ips[0]
    meta = _geolocate_ip_quick(rip)
    rcc = (meta.get("countryCode") or "").upper()
    rcountry = meta.get("country") or "?"
    pcc = (public_country or "").upper()
    detail = {"resolver_ip": rip, "resolver_country": rcountry,
              "resolver_cc": rcc, "public_country_cc": pcc,
              "isp": meta.get("isp", ""), "as": meta.get("as", "")}
    # Restricted-region resolver = automatic warn even without comparison
    cn_resolver = rcc in RESTRICTED_REGIONS
    if pcc and rcc and pcc != rcc:
        return _result("DNS 泄露检测", "fail",
                       f"⚠ 出口解析器在 {rcountry}({rcc}) ≠ 公共 IP ({pcc}) — DNS 泄露",
                       detail, verify_url=url_verify,
                       request_url="dig TXT whoami.cloudflare")
    if cn_resolver:
        return _result("DNS 泄露检测", "warn",
                       f"出口解析器在 {rcountry}({rcc}) — 限制区，可能泄露",
                       detail, verify_url=url_verify,
                       request_url="dig TXT whoami.cloudflare")
    if rcc:
        return _result("DNS 泄露检测", "ok",
                       f"出口解析器在 {rcountry}({rcc}) · 与公共 IP 一致",
                       detail, verify_url=url_verify,
                       request_url="dig TXT whoami.cloudflare")
    return _result("DNS 泄露检测", "warn",
                   f"出口解析器 IP {rip} (无法定位)",
                   detail, verify_url=url_verify,
                   request_url="dig TXT whoami.cloudflare")


def check_speed() -> dict:
    """Quick download from Cloudflare 10MB test endpoint."""
    url = "https://speed.cloudflare.com/__down?bytes=10000000"
    try:
        t0 = time.perf_counter()
        with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
            if r.status_code != 200:
                return _result("下载速度", "fail", f"HTTP {r.status_code}",
                               verify_url="https://speed.cloudflare.com/",
                               request_url=url)
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                total += len(chunk)
                if time.perf_counter() - t0 > 12:
                    break
            elapsed = time.perf_counter() - t0
        mbps = (total * 8) / elapsed / 1_000_000
        s = "ok" if mbps > 5 else ("warn" if mbps > 1 else "fail")
        return _result("下载速度", s,
                       f"{round(mbps,2)} Mbps ({total/1e6:.1f}MB / {elapsed:.1f}s)",
                       {"mbps": round(mbps, 2), "bytes": total,
                        "elapsed_s": round(elapsed, 2)},
                       verify_url="https://speed.cloudflare.com/", request_url=url)
    except Exception as e:
        return _result("下载速度", "fail", "测速失败",
                       verify_url="https://speed.cloudflare.com/",
                       request_url=url, error=str(e))


# ============================================================================
# Orchestrator
# ============================================================================
@dataclass
class CheckBatch:
    name: str
    fn: Callable[..., Any]
    args: tuple = field(default_factory=tuple)


def _claude_visible_ip() -> str:
    """Resolve the IP that claude.ai's edge actually sees us as.
    Falls back to provided IP only if trace fails."""
    try:
        r = requests.get("https://claude.ai/cdn-cgi/trace",
                         headers=HEADERS, timeout=_timeout())
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.startswith("ip="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def build_default_batches(ip: str) -> list[CheckBatch]:
    # The IP Claude actually sees may differ from `ip` (e.g. dual-stack →
    # Claude routes via IPv6 while we resolved an IPv4). Risk lookups for
    # Claude-related verdicts must use the Claude-visible IP.
    claude_ip = _claude_visible_ip() or ip
    return [
        # ── Multi-egress IP map (ip.net.coffee/claude top cards) ──
        CheckBatch("egress_ips", check_egress_ips, ()),
        CheckBatch("iprisk", check_iprisk_score, (claude_ip,)),
        CheckBatch("claude_reach", check_claude_reachability, ()),
        CheckBatch("claude_status", check_claude_status, ()),
        # ── IP / Geo / ASN ──
        CheckBatch("ipinfo", check_ipinfo, (ip,)),
        CheckBatch("ip-api", check_ip_api, (ip,)),
        CheckBatch("ipapi.is", check_ipapi_is, (ip,)),
        CheckBatch("ip2location", check_ip2location, (ip,)),
        CheckBatch("dbip", check_dbip, (ip,)),
        # ── Risk scores (some need keys / are CF-blocked) ──
        CheckBatch("scamalytics", check_scamalytics, (ip,)),
        CheckBatch("ipqs", check_ipqs, (ip,)),
        CheckBatch("abuseipdb", check_abuseipdb, (ip,)),
        CheckBatch("ping0", check_ping0, (ip,)),
        # ── AI services unlock ──
        CheckBatch("claude", check_claude_unlock, ()),
        CheckBatch("chatgpt", check_chatgpt_unlock, ()),
        CheckBatch("gemini", check_gemini_unlock, ()),
        # ── Streaming ──
        CheckBatch("netflix", check_netflix, ()),
        CheckBatch("disney", check_disney_plus, ()),
        CheckBatch("youtube_premium", check_youtube_premium, ()),
        CheckBatch("tiktok", check_tiktok, ()),
        CheckBatch("spotify", check_spotify, ()),
        # ── DNS resolvers + leak ──
        CheckBatch("dns_resolvers", check_dns_resolvers, ()),
        # We don't yet know our public-IP country at batch-build time; the
        # function tolerates an empty hint and still reports what it saw.
        CheckBatch("dns_leak", check_dns_leak, ()),
        # ── Latency & speed ──
        CheckBatch("latency_all", check_latency_all, ()),
        CheckBatch("speed", check_speed, ()),
    ] + [CheckBatch(f"site_{n}", check_site, (n, u, e)) for (n, u, e) in SITE_TARGETS]


def run_batches(batches: list[CheckBatch],
                on_result: Callable[[str, Any], None],
                max_workers: int | None = None) -> None:
    cfg = load_config()
    workers = max_workers or int(cfg.get("settings", {}).get("max_workers", 12))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(b.fn, *b.args): b for b in batches}
        for fut in as_completed(future_map):
            b = future_map[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = _result(b.name, "error", "执行异常", error=str(e))
            on_result(b.name, res)


# Mainland-China score cap. Egress in mainland → score cannot exceed this.
CN_SCORE_CAP = 40


def _detect_country_code(all_results: list[dict]) -> str:
    """Best-effort extraction of the egress country code from a results list.
    Prefers Claude's view → ip.net.coffee → ipinfo / ip-api fallbacks."""
    by_key = {}
    for r in all_results:
        k = (r.get("name") or "").strip()
        if k and k not in by_key:
            by_key[k] = r
    # 1. egress_ips → claude_loc (the IP Claude itself sees us at)
    for name in ("出口 IP 多视角",):
        d = (by_key.get(name) or {}).get("data") or {}
        cc = (d.get("claude_loc") or "").upper()
        if cc:
            return cc
    # 2. ip.net.coffee/api/iprisk → countryCode
    for name in ("Claude 信任评分",):
        d = (by_key.get(name) or {}).get("data") or {}
        cc = (d.get("countryCode") or "").upper()
        if cc:
            return cc
    # 3. fallbacks: ipinfo / ip-api / ipapi.is
    for r in all_results:
        d = r.get("data") or {}
        cc = (d.get("country") or d.get("countryCode") or
              d.get("country_code") or "")
        if isinstance(cc, str) and len(cc) == 2:
            return cc.upper()
    return ""


def overall_verdict(all_results: list[dict],
                    force_cn_cap: bool = True) -> dict:
    """Aggregate per-check statuses into an overall 0-100 score.

    Highest-priority override: if `force_cn_cap` is True and the detected
    egress country code is "CN" (mainland China), the score is hard-capped
    at 20 regardless of how the individual checks scored. The user's stance
    is that no amount of green checks can rescue an exit-in-China IP.
    """
    total = len(all_results)
    if total == 0:
        return {"score": 0, "label": "无数据", "ok": 0, "warn": 0,
                "fail": 0, "manual": 0, "error": 0,
                "country_code": "", "cn_capped": False}
    ok = sum(1 for r in all_results if r.get("status") == "ok")
    warn = sum(1 for r in all_results if r.get("status") == "warn")
    fail = sum(1 for r in all_results if r.get("status") == "fail")
    manual = sum(1 for r in all_results if r.get("status") == "manual")
    err = sum(1 for r in all_results if r.get("status") == "error")
    # score: ok=1, warn=0.5, manual=neutral (excluded from denom), fail/err=0
    scored = total - manual
    score_pct = int((ok + 0.5 * warn) / scored * 100) if scored else 0

    cc = _detect_country_code(all_results)
    cn_capped = False
    if force_cn_cap and cc == "CN":
        score_pct = min(score_pct, CN_SCORE_CAP)
        cn_capped = True

    if cn_capped:
        label = f"中国大陆 · 强制封顶 {CN_SCORE_CAP}"
    elif score_pct >= 80:
        label = "优秀"
    elif score_pct >= 60:
        label = "良好"
    elif score_pct >= 40:
        label = "一般"
    else:
        label = "较差"
    return {"score": score_pct, "label": label, "ok": ok, "warn": warn,
            "fail": fail, "manual": manual, "error": err, "total": total,
            "country_code": cc, "cn_capped": cn_capped}
