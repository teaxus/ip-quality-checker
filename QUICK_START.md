# 快速开始：查看低分日志

## 🎯 最快方式（5秒）

1. **启动应用**
2. 点击 **设置** 按钮
3. 向下滚动到 **日志管理**
4. 点击 **打开今日日志**
5. 搜索 `警报` 或 `低分`

---

## 📊 日志示例

搜索到的内容应该看起来像这样：

```
[14:24:20] 🔴 警报: 评分 35 低于阈值 40 — 浮窗持续闪烁
[14:24:20] 🔪 低分自动清理: 已结束 2 个进程 · 0 个失败
         · 1234 claude_proxy (python claude_proxy.py)
         · 5678 ⚡ 连接 Claude: 123.45.67.89:443
```

---

## 🔧 如果 Windows 上看到窗口闪现

1. 打开文件管理器
2. 地址栏输入：`%USERPROFILE%\.ip-quality-checker\logs`
3. 按 Enter
4. 打开最新的 `.log` 文件
5. 查看最后的 `FATAL ERROR` 行了解问题

**或**在设置中点击"打开日志文件夹"按钮

---

## 💻 命令行查看

```bash
# macOS / Linux
grep "警报" ~/.ip-quality-checker/logs/$(date +%Y%m%d).log

# Windows PowerShell
Select-String -Path "$env:USERPROFILE\.ip-quality-checker\logs\*.log" -Pattern "警报"
```

---

## 📍 日志文件位置

| 系统 | 路径 |
|------|------|
| Windows | `C:\Users\<你的用户名>\.ip-quality-checker\logs\` |
| macOS | `~/.ip-quality-checker/logs/` |
| Linux | `~/.ip-quality-checker/logs/` |

---

## ❓ 常见问题

**Q: 为什么看不到低分日志？**
- 应用可能没有运行过，还没生成日志
- 检查日志文件夹是否存在
- Windows 上：显示隐藏文件（Ctrl+H）

**Q: 日志文件名是什么？**
- 格式：`YYYYMMDD.log`（例如 `20260511.log`）
- 每天自动创建新文件

**Q: 可以删除日志吗？**
- 可以，不需要的日期直接删除即可

---

## 📚 详细文档

- [IMPROVEMENTS.md](IMPROVEMENTS.md) - 完整改进说明
- [WINDOWS_FIXES.md](WINDOWS_FIXES.md) - Windows 问题详细解决
- [README.md](README.md) - 应用完整文档
