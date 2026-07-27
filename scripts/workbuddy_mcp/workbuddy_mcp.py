"""
WorkBuddy MCP Server —— 将本地 codebuddy CLI 包装为 MCP 工具。

运行方式（stdio 传输）：
    python workbuddy_mcp.py

xiaozhi.me 云端大模型可通过此工具调用本地 codebuddy 生成报告，
工具立即返回（不阻塞），后台执行完成后通过信号文件通知桥接器。
"""

import os
import sys
import json
import re
import time
import subprocess
import threading
import tempfile
from datetime import datetime
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("WorkBuddy")

# 信号目录（与 mcp_gui.py 的桥接器共享同一路径）
SIGNAL_DIR = os.path.join(tempfile.gettempdir(), "workbuddy_mcp_signals")

# 配置文件路径（与 mcp_gui.py 共享）
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_config.json")


def _topic_to_name(topic: str, max_len: int = 30) -> str:
    """将话题转为合法的文件夹/文件名（不含日期前缀）。
    例如"开发项目激励方案" → "开发项目激励方案"，
    "关于新能源汽车的调研报告" → "关于新能源汽车的调研报告"
    """
    # 保留中文、英文、数字、连字符、下划线；其余字符替换为下划线
    name = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', topic)
    # 合并连续下划线
    name = re.sub(r'_+', '_', name)
    # 去掉首尾下划线
    name = name.strip('_')
    # 截断
    if len(name) > max_len:
        name = name[:max_len]
    return name or "文档"


def _pandoc_convert(out_dir: str, expected_md_name: str = None):
    """将目录下所有 .md 文件用 pypandoc 本地转为 .docx。"""
    try:
        import pypandoc
        md_files = [f for f in os.listdir(out_dir) if f.endswith('.md')]
        # 如果有同名 MD 但文件名不对，先重命名
        if expected_md_name and not os.path.exists(os.path.join(out_dir, expected_md_name)):
            for f in md_files:
                if f != expected_md_name and os.path.getsize(os.path.join(out_dir, f)) >= 100:
                    os.rename(os.path.join(out_dir, f), os.path.join(out_dir, expected_md_name))
                    break
        # 转换所有 MD → DOCX
        for f in os.listdir(out_dir):
            if f.endswith('.md'):
                md_path = os.path.join(out_dir, f)
                if os.path.getsize(md_path) < 100:
                    continue
                docx_path = os.path.join(out_dir, f[:-3] + '.docx')
                pypandoc.convert_file(md_path, 'docx', outputfile=docx_path)
    except Exception:
        pass


def _bg_runner(out_dir: str, topic_name: str, prompt: str, expected_md: str):
    """后台线程：启动 codebuddy，等待完成，pandoc 本地转 DOCX，写 done 信号。"""
    try:
        proc = subprocess.run(
            ["codebuddy", "-p", "--permission-mode", "bypassPermissions", prompt],
            cwd=out_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        # 超时但仍尝试转换已有的 MD
        _pandoc_convert(out_dir, expected_md)
        return
    except FileNotFoundError:
        return  # codebuddy 未安装

    # 无论 codebuddy 退出码如何，都尝试转换已有的 MD
    _pandoc_convert(out_dir, expected_md)

    if proc.returncode != 0:
        return  # codebuddy 执行失败

    # 写入 done 信号（桥接器的 _watch_signals 会检测到并发通知）
    os.makedirs(SIGNAL_DIR, exist_ok=True)
    sig_name = f"done_{topic_name}_{int(time.time())}.json"
    sig_path = os.path.join(SIGNAL_DIR, sig_name)
    try:
        with open(sig_path, "w", encoding="utf-8") as f:
            json.dump({
                "output_name": topic_name,
                "save_path": out_dir,
            }, f, ensure_ascii=False)
    except Exception:
        pass  # 信号写入失败不影响主流程


@mcp.tool()
def write_report(topic: str, version: str = "1") -> dict:
    """写文档工具。

    当用户要写报告/文档/文章/方案/总结/调研时，调用本工具。
    工具会自动生成文件保存到桌面（或配置的工作目录）。
    文件夹名和文件名都从 topic 自动派生（日期_话题名称_V版本号）。
    调用后你只需告诉用户"已经安排好了"。

    参数:
        topic: 报告主题。例如"开发项目激励方案"
        version: 版本号，默认"1"。同名文件夹中已有文件时自动递增
    """
    # --- 确定工作目录（优先使用配置的 work_dir，否则桌面）---
    work_dir = ""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            work_dir = cfg.get("work_dir", "").strip()
    except Exception:
        pass
    if not work_dir or not os.path.isdir(work_dir):
        work_dir = os.path.join(os.environ["USERPROFILE"], "Desktop")

    # --- 文件夹名和文件名都从 topic 派生 ---
    topic_name = _topic_to_name(topic)
    date_str = datetime.now().strftime("%Y-%m-%d")
    folder_name = f"{date_str}_{topic_name}"
    out_dir = os.path.join(work_dir, folder_name)
    os.makedirs(out_dir, exist_ok=True)

    # --- 文件：日期_名称_V版本号.格式（若文件已存在则自动递增版本号）---
    ver = int(version)
    file_name_md = f"{folder_name}_V{ver}.md"
    while os.path.exists(os.path.join(out_dir, file_name_md)):
        ver += 1
        file_name_md = f"{folder_name}_V{ver}.md"

    prompt = (
        f"请撰写一份关于《{topic}》的文档，内容详实、结构清晰、"
        f"包含背景分析、现状、趋势、结论与建议等部分。"
        f"请将最终文件保存为 Markdown 文档（.md）到：{out_dir}\\{file_name_md}"
    )

    # 检查 codebuddy 是否可用
    try:
        subprocess.run(["codebuddy", "--version"], capture_output=True, timeout=5)
    except FileNotFoundError:
        return {
            "success": False,
            "error": "未找到 codebuddy 命令，请确认已安装 CodeBuddy CLI。",
        }
    except Exception:
        pass  # version 检查失败可忽略

    # 写 pending 信号文件（桥接器轮询检测报告完成）
    os.makedirs(SIGNAL_DIR, exist_ok=True)
    pending_sig = os.path.join(SIGNAL_DIR, f"pending_{topic_name}.json")
    try:
        with open(pending_sig, "w", encoding="utf-8") as f:
            json.dump({
                "output_name": topic_name,
                "save_path": out_dir,
            }, f, ensure_ascii=False)
    except Exception:
        pass  # 信号写入失败不影响主流程

    # 后台线程执行 codebuddy（不阻塞 MCP 工具返回）
    t = threading.Thread(
        target=_bg_runner,
        args=(out_dir, topic_name, prompt, file_name_md),
        daemon=True,
    )
    t.start()

    return {
        "success": True,
        "message": f"文档生成任务已启动，保存到：{folder_name}\\{file_name_md}。请告诉用户：已帮您安排好了，请稍等。",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
