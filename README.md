# IP 网络质量评估 (IP Quality Checker)

跨平台 CLI + GUI 工具，**严格按 xykt/IPQuality、xykt/NetQuality、lmc999/RegionRestrictionCheck 三大开源脚本的源码逻辑**重写，聚合多个权威 IP 风险库与公开端点，评估当前网络对 Google / Claude / ChatGPT / Gemini / 流媒体的支持，以及 IP 的纯净度与风险。

## Claude 专项检测（对标 ip.net.coffee/claude/）

新增的 `Claude Focus` 组照搬了 ip.net.coffee/claude/ 的核心指标，关键洞察：**你看到的 IPv4 不一定是 Claude 看到的 IP**。Claude 走 IPv6 的话，封号风控就基于那个 IPv6 决策，跟你的 IPv4 风险分无关。

| 指标 | 实现 | 来源 |
|---|---|---|
| **出口 IP 多视角** | 同时探测 `claude.ai/cdn-cgi/trace`、`1.1.1.1/cdn-cgi/trace`、IPv4 公开端点；对比三者是否一致；标记 IPv6/IPv4 分流 | ip.net.coffee/claude top cards |
| **Claude 信任评分** | 调 `https://ip.net.coffee/api/iprisk/{ip}` 公开端点，**用 Claude 视角的 IP**；输出 0-100 + 极度纯净/纯净/良好/中性/可疑 + VPN/Proxy/Tor/Crawler/Abuser 标签 | ip.net.coffee Card 1 + 3 |
| **限制区域强制覆盖** | 出口在 `CN/HK/MO/RU/KP/IR/SY/CU/BY/VE` 任一 → 强制 0 分 + 红色不可访问 | 与 ip.net.coffee 同列表 |
| **Claude 可达性** | `claude.ai/cdn-cgi/trace` + `anthropic.com/favicon.ico` 双探测，<250ms 正常 / <500ms 良好 / ≥500ms 较慢 / 失败不可达 | ip.net.coffee Card 4 |
| **Claude 服务状态** | `status.claude.com/api/v2/status.json` 读取 `indicator` (none/minor/major/critical/maintenance) | ip.net.coffee Card 4 行 4 |

> **⚠ 浏览器专属指标我们 CLI 不做**：DNS-leak 带外检测（需要自建 authoritative DNS，他们自己有 *.d.ip.net.coffee 一套）、WebRTC UDP 泄漏（需 RTCPeerConnection）、时区/语言/操作系统/WebGL/Canvas/触屏 等浏览器指纹。这些只能在浏览器里跑，CLI 跳过。需要这些请直接打开 https://ip.net.coffee/claude/ 查看。

只跑 Claude 专项：
```bash
python cli.py --only egress_ips,iprisk,claude_reach,claude_status,claude
```

## 关键忠实度对照

| 检测项 | 我们的实现路径 | 上游源码定位 |
|---|---|---|
| **IPinfo (Geo+ASN+Privacy)** | `ipinfo.io/widget/demo/{ip}` 公开端点（无需 Key） | xykt/ip.sh L760-825 |
| **ip-api.com** | `ip-api.com/json/{ip}` 直接调用 | 通用 |
| **ipapi.is** | `api.ipapi.is/?q={ip}`，提取 abuser_score | xykt/ip.sh L920-990 |
| **DB-IP** | HTML 爬取 `db-ip.com/{ip}` 的威胁等级 + countryCode | xykt/ip.sh L1134-1169 |
| **IP2Location** | `api.ip2location.io/?ip={ip}` 公开端点 | xykt/ip.sh L1041-1133 |
| **Scamalytics** | 直接爬 `scamalytics.com/ip/{ip}`，正则 `Fraud Score:\s*(\d+)` | xykt/ip.sh L826-856 |
| **Claude** | `curl -L https://claude.ai/`，看最终 URL（不看状态码） | lmc999/check.sh L4564 |
| **ChatGPT** | `/compliance/cookie_requirements` (Bearer null) + `ios.chat.openai.com`，4 状态交叉 | xykt/ip.sh L1632 |
| **Gemini** | 检查页面里的 `45631641,null,true` 标记常量 | lmc999/check.sh L4544 |
| **Netflix** | 双标题 `81280792` + `70143836`，看 "Oh no!" 标记 | xykt/ip.sh L1462 / lmc999 L804 |
| **Disney+** | 三步 Bearer Token 鉴权 + 主页重定向检测 | xykt/ip.sh L1375 |
| **YouTube Premium** | 带特殊 cookies + Accept-Language: en，看 `INNERTUBE_CONTEXT_GL` | xykt/ip.sh L1502 |
| **TikTok** | 抓取主页 `"region":"XX"` | xykt/ip.sh L1327 |
| **Spotify** | 注册端点的 status code (311/120/320) | lmc999/check.sh L3569 |

每项检测都返回 **request URL（实际请求）** 和 **verify URL（可在浏览器打开对照的网站）**，方便人工核验。

## 状态码

| 标签 | 含义 |
|---|---|
| ✓ OK | 通过 |
| ⚠ WARN | 部分可用 / 弱信号 |
| ✗ FAIL | 失败 / 被封 |
| ? MANUAL | 自动检测被拦截（CF Turnstile / 缺 API Key），打开 verify URL 手动确认 |
| ! ERROR | 网络错误或异常 |

## CLI 使用

```bash
# 默认全跑（30+ 项）
python cli.py

# 只跑某些项（用 --list 看完整列表）
python cli.py --only ip-api,claude,chatgpt,netflix
python cli.py --list

# 检查指定 IP（站点/AI 检测仍走自己的出口）
python cli.py --ip 8.8.8.8

# 输出原始 JSON 数据（包含每个上游响应的解析细节）
python cli.py --raw          # 文本 + 内嵌 JSON
python cli.py --json         # 单个 JSON 文档（适合脚本）

# 调整超时和并发
python cli.py --timeout 15 --workers 16
```

样例输出：

```
[ 6/31] ✓ OK     Claude (claude.ai) — 地区支持 (final=https://claude.ai/login, HTTP 403)
    request: https://claude.ai/
    verify : https://claude.ai/
[10/31] ✓ OK     ChatGPT (OpenAI) — 完全可用 (出口 MX)
    request: https://api.openai.com/compliance/cookie_requirements + https://ios.chat.openai.com/
    verify : https://chatgpt.com/
[31/31] ✓ OK     Netflix — 完整解锁 · 地区 MX
    request: https://www.netflix.com/title/{81280792,70143836}
    verify : https://www.netflix.com/title/70143836
```

每行的 verify URL 都可以直接在浏览器打开，跟脚本判定做交叉验证。

## GUI 使用

GUI 用 customtkinter 构建（共用同一个 checkers 模块）：

```bash
./run.sh            # macOS / Linux（自动选带 tkinter 的 Python，建 venv）
run.bat             # Windows
```

> **macOS 注意**：Homebrew 自带的 python 默认不含 tkinter。先 `brew install python-tk@3.13`，然后 `rm -rf .venv` 让 `run.sh` 选用新 Python。

## 安装与依赖

仅 4 个 Python 依赖：
- `requests` — HTTP
- `customtkinter` — GUI（CLI 模式不需要）
- `Pillow` — customtkinter 隐式依赖
- `dnspython` — DNS 解析（可选）

```bash
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python cli.py                # CLI 模式
python main.py               # GUI 模式
```

## API Keys（可选，全免费）

不填 Key 也能跑大部分项。配置 Key 后可启用付费数据源：

| 服务 | 申请地址 | 免费额度 | 解锁内容 |
|---|---|---|---|
| IPQualityScore | https://ipqualityscore.com/create-account | 5000/月 | 详细欺诈分 |
| AbuseIPDB | https://abuseipdb.com/register | 1000/天 | 滥用置信度 |
| IP2Location | https://ip2location.io/sign-up | 30k/月 | 详细 proxy 分类 |

填写方式：
- GUI：顶部 "设置" 按钮
- CLI：编辑 `~/.ip-quality-checker/config.json`，把 key 填入对应字段

## 受 Cloudflare 拦截、自动检测降级为 manual 的源

某些源用了 Cloudflare Turnstile / Aliyun CAPTCHA 等浏览器验证，纯 HTTP 请求拿不到内容。这些源会输出 `? MANUAL` 状态 + verify URL，请用浏览器打开人工查看：

- IPQualityScore 公开页（如不用 API Key）
- ping0.cc（Aliyun CAPTCHA + CF Turnstile）
- AbuseIPDB（如不用 API Key）

## 打包独立可执行文件

本项目提供 **4 个目标架构**（macOS arm64 / macOS x86_64 / Windows x64 / Windows arm64）的打包方案。**PyInstaller 不支持跨 OS 编译**，所以一台机器最多只能打两个（同 OS 的两个 arch），要四个全要必须用 GitHub Actions 或者多台机器配合。

### 三种打包方式

#### 方式 A：GitHub Actions（**真·一键四端**，推荐）

仓库已带 `.github/workflows/build.yml`，单次工作流并行跑四个 runner，输出四个 zip 工件：

```bash
git tag v1.0.0
git push origin v1.0.0      # 触发，自动 build 4 端并附加到 Release
# 或者
git push origin main         # build 4 端，只上传为 Actions artifacts
# 或者直接在 GitHub Actions 页面点 "Run workflow"
```

完成后到仓库 Actions 页 → Artifacts 或 Releases 页面下载：
- `IPQualityChecker-macos-arm64.zip`
- `IPQualityChecker-macos-x86_64.zip`
- `IPQualityChecker-windows-x64.zip`
- `IPQualityChecker-windows-arm64.zip`

#### 方式 B：本机 macOS — `scripts/build-all.sh`（**零环境前置**）

```bash
bash scripts/build-all.sh
```

脚本会**自动**完成下面这些事，**不需要预先装任何东西**：

1. **找 Python**：依次搜 `/Library/Frameworks/Python.framework/...` → `python3.13/12/11` → `python3`
2. **没找到 → 自动装**：
   - **Tier 1**：下载 python.org 的 `python-3.13.x-macos11.pkg`（universal2），`sudo installer -pkg ... -target /` 静默装上。装完两端都能打。
   - **Tier 2 失败回退**：装 Homebrew + `brew install python@3.13 python-tk@3.13`（这条只给 arm64，Intel 端打不了，但 Apple Silicon 用户多数只关心自家 arm64 .app）
3. **建 `.venv-build` 隔离环境**，装 `requirements.txt` + `pyinstaller`
4. **按 Python 实际支持的架构循环打包**

跳过自动安装：`BUILD_NO_AUTO_INSTALL=1 bash scripts/build-all.sh`  
手动指定 Python：`PYTHON=/path/to/python3 bash scripts/build-all.sh`

输出：
- `dist/IPQualityChecker-macos-arm64.app` + `dist/ipqc-macos-arm64`
- `dist/IPQualityChecker-macos-x86_64.app` + `dist/ipqc-macos-x86_64`（仅在 universal2 Python 存在时）

#### 方式 C：本机 Windows — 双击 `scripts\build-all-windows.bat`（**零环境前置**）

最干净的玩法：把项目目录拷到 Windows 上 → 双击 `scripts\build-all-windows.bat`。

脚本会**自动**：

1. **找 Python**：用 `py launcher` 探测每个版本 + 默认安装路径
2. **没找到 → 自动装**：
   - **Tier 1**：`winget install Python.Python.3.13 --architecture x64 --scope user --silent`（Windows 10 1809+ / Windows 11 自带 winget，**无需管理员权限**）
   - **Tier 2 回退**：直接从 `python.org/ftp/python/...` 下 `.exe` 安装器，`/passive InstallAllUsers=0 PrependPath=1` 静默装（per-user，仍然无需 UAC）
3. **重读 PATH** 让新装的 Python 立刻可见
4. **建 `.venv-build-AMD64 / .venv-build-ARM64`** 双 venv（如果是 ARM64 Windows）
5. **打 x64 .exe**（任何 Windows）+ **ARM64 .exe**（仅 ARM64 Windows 能产，因为 x86 Windows 不能跑 ARM64 Python）

也可以走 PowerShell 直接调：
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-all.ps1
powershell -ExecutionPolicy Bypass -File scripts\build-all.ps1 -SkipAutoInstall
```

输出：
- `dist\IPQualityChecker-windows-x64.exe` + `dist\ipqc-windows-x64.exe`
- `dist\IPQualityChecker-windows-arm64.exe` + `dist\ipqc-windows-arm64.exe`（仅 ARM64 host）

### 单端打包（用于调试）

```bash
python build.py                           # 当前 OS + arch
python build.py --arch arm64              # 仅 macOS：强制 Apple Silicon
python build.py --arch x86_64             # 仅 macOS：强制 Intel
python build.py --arch universal2         # 仅 macOS：单个胖二进制
python build.py --onefile                 # 强制单文件（Windows 默认）
python build.py --onedir                  # 强制目录（macOS 默认，启动快）
python build.py --clean                   # 先清 build/ dist/
python build.py --cli                     # 同时打 CLI 二进制
python build.py --out-suffix=arm64        # 自定义输出后缀
```

输出位置：
- macOS：`dist/IPQualityChecker.app`（默认 onedir，启动 sub-second）
- Windows：`dist/IPQualityChecker.exe`（默认 onefile）
- Linux：`dist/IPQualityChecker`

### macOS 单文件 vs 目录的取舍

- **`--onefile`** 把所有资源压成单个自解压二进制。每次双击 `.app` 都要把 ~40MB 资源解压到 `/var/folders/.../_MEI…/`，**4-8 秒**首启延迟，用户以为闪退。
- **`--onedir`**（macOS 默认）资源直接摊在 `IPQualityChecker.app/Contents/Frameworks/` 下，没有解压步骤，启动 **<1 秒**。.app 仍是单个可拖拽 bundle。

不建议在 macOS 上用 `--onefile`，除非你需要某种特殊的单二进制分发场景。

## 项目结构

```
ip-quality-checker/
├── cli.py                       # 命令行入口（推荐用于服务器/脚本）
├── main.py                      # GUI 入口（customtkinter）
├── checkers.py                  # 所有探测逻辑（每项一个函数）
├── config.py                    # API Key / 设置持久化
├── system_actions.py            # 进程/连接扫描 + 杀进程 + 开机自启
├── make_icon.py                 # 生成 icon.png/.ico/.icns（雷达主题）
├── build.py                     # 单端 PyInstaller 打包（核心引擎）
├── run.sh / run.bat             # 一键启动 GUI（开发模式）
├── build-windows.bat            # Windows 单端打包（双击运行）
├── scripts/
│   ├── build-all.sh             # macOS 本机打包 arm64+x86_64（含 Python 自动安装）
│   ├── build-all.ps1            # Windows 本机打包 x64+arm64（含 Python 自动安装）
│   └── build-all-windows.bat    # Windows 双击外壳（绕过 ExecutionPolicy）
├── .github/workflows/
│   └── build.yml                # GitHub Actions 一键打包 4 端
└── requirements.txt
```

## 已知限制

- **ping0.cc / Scamalytics 等 CF 强保护站点**，无 Key 路径下偶尔解析成功偶尔被挡。失败时会输出 verify URL 让你手动核对。
- **Disney+ region** 需要其 GraphQL `device.graphql` 端点的最新 api-key（Disney 已收紧），目前只能确认是否被禁，不能稳定提取国家码。
- **net quality 的「三网回程」** （xykt/net.sh 的核心）依赖 nexttrace/mtr + ASN 库做逐跳分类，跨平台移植较重，本工具尚未集成；CN 境内回程分析推荐直接跑 xykt/net.sh。

---

## 📝 日志和故障排除 (v1.2.1+)

### 日志文件位置

所有事件（包括低于 40 分的警报和进程清理）都被持久化到日志文件：

| 系统 | 路径 |
|---|---|
| macOS | `~/.ip-quality-checker/logs/YYYYMMDD.log` |
| Windows | `C:\Users\<username>\.ip-quality-checker\logs\YYYYMMDD.log` |
| Linux | `~/.ip-quality-checker/logs/YYYYMMDD.log` |

### 🔴 查看低分警报日志

**GUI 方式**（推荐）：
1. 点击 **设置** → 向下滚动到 **日志管理**
2. 点击 **打开今日日志** 按钮
3. 搜索 `警报` 或 `低分` 关键词

**命令行方式**：
```bash
# macOS / Linux
grep "警报\|低分" ~/.ip-quality-checker/logs/$(date +%Y%m%d).log

# Windows PowerShell
Select-String -Path "$env:USERPROFILE\.ip-quality-checker\logs\*.log" -Pattern "警报|低分"
```

### 🐛 Windows 窗口闪现问题

如果启动时看到窗口闪现又消失：

1. 查看日志文件：`%USERPROFILE%\.ip-quality-checker\logs\YYYYMMDD.log`
2. 找到 `FATAL ERROR` 行，了解具体错误
3. 常见原因及解决：
   - **缺少依赖**：`pip install -r requirements.txt`
   - **Python 版本**：需要 3.9+，推荐 3.11+
   - **权限问题**：确保对 `~/.ip-quality-checker/` 目录有读写权限

更多细节见 [WINDOWS_FIXES.md](WINDOWS_FIXES.md)。

### 📊 日志示例

```log
[14:24:05] === IP Quality Checker 启动 2026-05-11 14:24:05 ===
[14:24:05] 平台: Windows · Python: 3.13.0
[14:24:05] === 开始检测  2026-05-11 14:24:05 ===
[14:24:05] 公网 IP: 123.45.67.89  IPv6: 无
[14:24:10] [    OK] IPinfo — Shanghai, China · AS1234 ISP
[14:24:15] [  WARN] Scamalytics — 风险分 25/100 · Low Risk
[14:24:20] 🔴 警报: 评分 35 低于阈值 40 — 浮窗持续闪烁
[14:24:20] 🔪 低分自动清理: 已结束 2 个进程 · 0 个失败
         · 1234 claude_proxy (python claude_proxy.py)
         · 5678 ⚡ 连接 Claude: 123.45.67.89:443 (api.anthropic.com)
[14:24:20] === 检测完成  评分 35/100 ===
```
