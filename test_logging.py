#!/usr/bin/env python3
"""Test script to verify low-score logging functionality.

Usage:
    python test_logging.py
"""
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

import logger


def test_basic_logging():
    """Test basic logging to file."""
    print("✓ 测试 1: 基本日志记录")
    logger.log("This is a test message")
    logger.log("Another message with emoji 🔴 🔪")
    
    log_file = logger.get_log_file_path()
    assert log_file.exists(), f"日志文件不存在: {log_file}"
    
    content = log_file.read_text(encoding='utf-8')
    assert "test message" in content, "日志内容不包含测试消息"
    assert "🔴" in content, "日志不支持emoji"
    print(f"  ✓ 日志文件创建成功: {log_file}")
    print(f"  ✓ 内容示例: {content.split(chr(10))[0]}")


def test_low_score_simulation():
    """Simulate low-score events."""
    print("\n✓ 测试 2: 低分事件模拟")
    logger.log("🔴 警报: 评分 35 低于阈值 40 — 浮窗持续闪烁")
    logger.log("🔪 低分自动清理: 已结束 2 个进程 · 0 个失败")
    logger.log("   · 1234 claude_proxy (python claude_proxy.py)")
    
    log_file = logger.get_log_file_path()
    content = log_file.read_text(encoding='utf-8')
    
    assert "警报" in content, "未发现低分警报日志"
    assert "低分自动清理" in content, "未发现进程清理日志"
    print("  ✓ 低分事件记录成功")


def test_callback():
    """Test callback mechanism."""
    print("\n✓ 测试 3: 回调函数机制")
    
    messages = []
    def capture_callback(line: str):
        messages.append(line)
    
    logger.set_logger_callback(capture_callback)
    logger.log("Message 1")
    logger.log("Message 2")
    
    assert len(messages) >= 2, "回调函数未被正确调用"
    assert "Message 1" in messages[-2], "第一条消息未被记录"
    assert "Message 2" in messages[-1], "第二条消息未被记录"
    print(f"  ✓ 捕获 {len(messages)} 条消息")
    print(f"  ✓ 最后消息: {messages[-1]}")


def test_log_files_list():
    """Test log file listing."""
    print("\n✓ 测试 4: 日志文件列表")
    
    files = logger.get_log_files_list()
    print(f"  ✓ 发现 {len(files)} 个日志文件")
    if files:
        print(f"  ✓ 最新日志: {files[0].name}")


def main():
    print("=" * 60)
    print("IP Quality Checker 日志模块测试")
    print("=" * 60)
    
    try:
        test_basic_logging()
        test_low_score_simulation()
        test_callback()
        test_log_files_list()
        
        print("\n" + "=" * 60)
        print("✓ 所有测试通过！")
        print(f"✓ 日志位置: {logger.LOG_DIR}")
        print("=" * 60)
        return 0
        
    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ 意外错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
