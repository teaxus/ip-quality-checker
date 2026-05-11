# Windows 运行问题修复指南

## 问题症状

### 现象 1: 窗口不停弹出又消失
- 在 Windows 上运行 IPQualityChecker.exe
- 看到一个窗口闪现，然后立即消失
- 重复这个过程

**原因**：
1. PyInstaller 生成的 GUI 应用在启动时发生异常
2. Python 进程立即退出，无法显示错误信息（Windows console 被隐藏）
3. 用户看不到任何错误日志

### 现象 2: 查不到低于 40 分的日志
- 程序似乎在运行
- 但看不到"评分低于 40 分"的日志记录

**原因**：
- 之前日志只保存在内存的 UI widget 中
- 程序崩溃后日志丢失
- 没有文件持久化

---

## 修复方案

### ✅ 修复 1: 全局异常捕获

**文件**: `main.py` (第 ~2370 行)

```python
if __name__ == "__main__":
    try:
        logger.log("IP Quality Checker 启动")
        app = App()
        app.mainloop()
    except Exception as e:
        error_msg = f"致命错误：{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        logger.log(f"FATAL ERROR: {error_msg}")
        
        # 显示 GUI 错误对话框
        try:
            error_root = tk.Tk()
            error_root.withdraw()
            messagebox.showerror(
                "IPQualityChecker 错误",
                f"程序启动失败：\n\n{error_msg[:500]}\n\n"
                f"请检查日志文件：{logger.get_log_file_path()}")
            error_root.destroy()
        except Exception:
            print(error_msg, file=sys.stderr)
        
        sys.exit(1)
```

**效果**：
- 任何异常都会被捕获并显示
- Windows 用户会看到一个错误对话框而不是窗口闪现
- 错误被写入日志文件供后续分析

---

### ✅ 修复 2: 日志文件持久化

**新增文件**: `logger.py`

```python
LOG_DIR = Path.home() / ".ip-quality-checker" / "logs"
LOG_FILE = LOG_DIR / f"{datetime.now():%Y%m%d}.log"
```

**日志文件位置**：
- Windows: `C:\Users\<username>\.ip-quality-checker\logs\20260511.log`
- macOS: `/Users/<username>/.ip-quality-checker/logs/20260511.log`
- Linux: `/home/<username>/.ip-quality-checker/logs/20260511.log`

**特性**：
- 线程安全（使用 `threading.Lock`）
- 自动日期轮转（每天创建新文件）
- 同时写入文件和 UI 显示

---

### ✅ 修复 3: UI 日志管理

**位置**: 设置窗口 → 日志管理

**新增功能**：
```
📋 日志管理
  日志保存位置：C:\Users\<username>\.ip-quality-checker\logs
  当日日志：20260511.log
  所有低于 40 分的事件都会被记录到文件中
  
  [打开日志文件夹]  [打开今日日志]
```

**按钮说明**：
- **打开日志文件夹** - 在文件管理器中打开日志目录
  - Windows: 使用 `explorer.exe`
  - macOS: 使用 `open` 命令
  - Linux: 使用 `xdg-open`

- **打开今日日志** - 用默认编辑器打开日志文件
  - Windows: 使用 `notepad.exe`
  - macOS: 使用 `TextEdit` (open -t)
  - Linux: 使用系统默认编辑器

---

## 如何查看低分事件

### 方法 1: 通过 UI 查看（最简单）

1. 启动应用
2. 点击 **设置** 按钮
3. 向下滚动到 **日志管理** 部分
4. 点击 **打开今日日志**
5. 搜索 `警报` 关键词

### 方法 2: 用记事本查看

1. 打开 Windows 文件管理器
2. 在地址栏输入：`%USERPROFILE%\.ip-quality-checker\logs`
3. 打开当日的 `.log` 文件（如 `20260511.log`）
4. 使用 Ctrl+F 搜索 `警报` 或 `低分`

### 方法 3: 用 PowerShell 查看

```powershell
# 查看今天的日志
Get-Content "$env:USERPROFILE\.ip-quality-checker\logs\$(Get-Date -Format 'yyyyMMdd').log"

# 搜索低分事件
Select-String -Path "$env:USERPROFILE\.ip-quality-checker\logs\*.log" -Pattern "警报|低分"
```

---

## 低分事件日志示例

```log
[14:23:48] === IP Quality Checker 启动 2026-05-11 14:23:48 ===
[14:23:48] 平台: Windows · Python: 3.13.0
[14:23:48] === 开始检测  2026-05-11 14:23:48 ===
[14:23:50] 公网 IP: 123.45.67.89  IPv6: 无
[14:23:50] 启动 32 项检测…
[14:24:00] [    OK] IPinfo — Shanghai, China · AS1234 Example ISP
[14:24:02] [  WARN] Scamalytics — 风险分 25/100 · Low Risk
[14:24:05] 🔴 警报: 评分 35 低于阈值 40 — 浮窗持续闪烁
[14:24:05] 🔪 低分自动清理: 已结束 2 个进程 · 0 个失败
       · (进程名匹配 1, 连接 Claude 匹配 1)
       · 1234 claude_proxy (python claude_proxy.py)
       · 5678 ⚡ 连接 Claude: 123.45.67.89:443 (api.anthropic.com)
[14:24:05] === 检测完成  评分 35/100 ===
```

**解读**：
- `🔴 警报` - 评分低于阈值 40
- `🔪 低分自动清理` - 自动结束了相关进程
- 列出了所有被结束的进程（PID 和进程名）

---

## 常见问题

### Q1: 日志文件在哪里？

**A**: Windows 上位置是：
```
C:\Users\<你的用户名>\.ip-quality-checker\logs\
```

例如：
```
C:\Users\tom\.ip-quality-checker\logs\20260511.log
```

快速打开方法：
1. 在地址栏粘贴：`%USERPROFILE%\.ip-quality-checker\logs`
2. 按 Enter 打开

### Q2: 为什么看不到日志？

**A**: 可能的原因：
1. 应用刚安装，还没有运行过
2. `.ip-quality-checker` 文件夹是隐藏的
   - 解决：Windows 资源管理器 → 查看 → 显示隐藏的项目

3. 文件夹权限不足
   - 解决：检查 `C:\Users\<user>` 目录权限

### Q3: 日志文件太大了怎么办？

**A**: 日志文件只在低于 40 分时才会有大量内容。
- 每天创建新文件，自动轮转
- 手动删除旧日志：右键点击 → 删除（只删除不需要的日期）

### Q4: 如何查看所有低分事件？

**A**: 使用记事本的搜索功能：
1. 打开日志文件
2. Ctrl+H 打开"查找和替换"
3. 查找：`🔴 警报`
4. 查看所有匹配项

或用 PowerShell：
```powershell
Select-String -Path "$env:USERPROFILE\.ip-quality-checker\logs\*.log" `
  -Pattern "🔴|警报" | Format-Table -AutoSize
```

### Q5: 如果应用仍然闪现怎么办？

**A**: 检查日志文件：
1. 打开 `~\.ip-quality-checker\logs\20260511.log`
2. 查看最后几行，找到 `FATAL ERROR` 信息
3. 根据错误信息排查：
   - 缺少依赖包？
   - 配置文件损坏？
   - 网络问题？

---

## 技术细节

### Windows 异常处理的挑战

PyInstaller 在 Windows 上创建 GUI 应用时：
- 隐藏 console 窗口（–windowed 参数）
- 如果应用启动时异常，Python 进程立即退出
- 用户看不到错误信息，只看到闪现

### 解决方案

```python
try:
    app = App()
    app.mainloop()
except Exception as e:
    # 1. 写入文件日志（即使 UI 失败也能保留记录）
    logger.log(f"FATAL ERROR: {e}")
    
    # 2. 显示 GUI 对话框（Windows 用户能看到）
    messagebox.showerror("错误", str(e))
    
    # 3. 标准错误输出（如果控制台打开了能看到）
    print(f"ERROR: {e}", file=sys.stderr)
```

---

## 版本信息

- **应用版本**: 1.2.1
- **修复日期**: 2026-05-11
- **支持平台**: Windows 10/11, macOS 10.13+, Linux (GTK 3.0+)

---

## 反馈和问题

如果遇到问题：
1. 查看日志文件中的错误信息
2. 确保已安装所有依赖：`pip install -r requirements.txt`
3. 在 GitHub 上提交 Issue

---

**最后更新**: 2026-05-11
