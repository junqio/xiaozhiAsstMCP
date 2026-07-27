"""
WorkBuddy MCP 桌面桥接器 — GUI + MCP Server 二合一

运行模式：
  - 双击 mcp_gui.exe → GUI 模式
  - mcp_gui.exe --mcp-server → MCP Server 模式（stdio 传输，供桥接器内部调用）

功能：
 1. 配置 xiaozhi.me MCP 接入点
 2. 一键启动/停止 MCP 桥接服务
 3. 实时显示连接状态和日志
 4. 测试 codebuddy CLI 是否可用

架构：
  xiaozhi.me 云端 ←→ WebSocket ←→ 桥接器 ←→ stdio ←→ mcp_gui.exe --mcp-server (FastMCP)
"""

import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext, messagebox
import json
import os
import shutil
import sys
import queue
import threading
import asyncio
import subprocess
import winsound
import time
from datetime import datetime

# --- find_codebuddy 缓存（避免每次工具调用都扫描磁盘） ---
_CODEBUDDY_SENTINEL = object()
_CODEBUDDY_CACHE = _CODEBUDDY_SENTINEL  # 未搜索；搜到存 tuple；没搜到存 None

# ============================================================
#  查找 codebuddy 命令（返回 (exe, [prefix_args]) 或 None）
#  策略：优先找独立二进制，再找 npm + node 方案
# ============================================================
_NODE_CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles%\nodejs\node.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\nodejs\node.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\nodejs\node.exe"),
]
_CB_JS_CANDIDATES = [
    os.path.expandvars(r"%APPDATA%\npm\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy"),
    os.path.expandvars(r"%LOCALAPPDATA%\npm\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy"),
]


def _cleanup_artifacts(out_dir: str):
    """清理 codebuddy 在输出目录留下的临时文件（package.json, node_modules 等）"""
    import shutil
    cleanup_items = ["package.json", "package-lock.json", "node_modules"]
    for item in cleanup_items:
        item_path = os.path.join(out_dir, item)
        try:
            if item == "node_modules":
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                    print(f"[cleanup] 已删除: {item_path}", file=sys.stderr, flush=True)
            else:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                    print(f"[cleanup] 已删除: {item_path}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[cleanup] 删除失败 {item_path}: {e}", file=sys.stderr, flush=True)


def _safe_print(msg: str):
    """安全打印到 stderr，避免 --noconsole 模式下 stderr=None 导致线程崩溃"""
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _safe_log(log_path: str, msg: str):
    """安全追加写入日志文件"""
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(msg)
    except Exception:
        pass


def find_codebuddy() -> tuple | None:
    """返回 (可执行文件路径, 参数前缀列表)，找不到返回 None
    结果会被缓存，避免每次工具调用都扫描磁盘。

    例如: (r'C:\\...\\node.exe', [r'C:\\...\\codebuddy'])
    或:   (r'C:\\...\\codebuddy.exe', [])
    """
    global _CODEBUDDY_CACHE
    if _CODEBUDDY_CACHE is not _CODEBUDDY_SENTINEL:
        return _CODEBUDDY_CACHE  # 已搜过，直接返回（可能是 None 或 tuple）
    # 1. 优先：node.exe 全路径 + codebuddy JS 入口（不受 PATH 影响）
    node_exe = None
    for check in ("node", "node.exe"):
        p = shutil.which(check)
        if p:
            node_exe = p
            break
    if not node_exe:
        for p in _NODE_CANDIDATES:
            if os.path.exists(p):
                node_exe = p
                break

    if node_exe:
        for p in _CB_JS_CANDIDATES:
            if os.path.exists(p):
                _CODEBUDDY_CACHE = (node_exe, [p])
                return _CODEBUDDY_CACHE

    # 2. 独立 .exe 二进制
    for name in ("codebuddy", "codebuddy.exe"):
        p = shutil.which(name)
        if p and p.lower().endswith(('.exe', '.com')):
            _CODEBUDDY_CACHE = (p, [])
            return _CODEBUDDY_CACHE

    # 3. 兜底：.cmd 批处理（依赖 PATH，EXE 环境可能找不到 node）
    for name in ("codebuddy", "codebuddy.cmd"):
        p = shutil.which(name)
        if p:
            _CODEBUDDY_CACHE = (p, [])
            return _CODEBUDDY_CACHE
    # 也搜硬编码目录
    cb_cmd = os.path.expandvars(r"%APPDATA%\npm\codebuddy.cmd")
    if os.path.exists(cb_cmd):
        _CODEBUDDY_CACHE = (cb_cmd, [])
        return _CODEBUDDY_CACHE

    _CODEBUDDY_CACHE = None
    return None


# ============================================================
#  PATH 修复：确保 System32 在子进程 PATH 中（避免 reg/wmic 报错）
# ============================================================
def _get_fixed_env():
    """返回修复了 PATH 的环境变量字典。
    
    codebuddy CLI 冷启动时会调 reg (C:\\Windows\\System32) 和 wmic (C:\\Windows\\System32\\wbem)
    来扫描系统环境。部分 Windows 安装中这些目录不在 PATH 中，导致每次启动都报错并重试，
    额外浪费约 10-30 秒。此函数确保 System32 路径在子进程的 PATH 中。
    """
    env = os.environ.copy()
    path = env.get("PATH", "")
    required = [r"C:\Windows\System32", r"C:\Windows\System32\wbem"]
    missing = [p for p in required if p not in path and p.upper() not in path.upper()]
    if missing:
        env["PATH"] = ";".join(missing) + ";" + path
    return env


# ============================================================
#  常量
# ============================================================
APP_TITLE = "WorkBuddy MCP 桌面桥接器"
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "mcp_config.json")
DEFAULT_ENDPOINT = ""

# 报告完成信号目录（供 write_to_file 后台线程 → 桥接器之间通信）
import tempfile as _tempfile
SIGNAL_DIR = os.path.join(_tempfile.gettempdir(), "workbuddy_mcp_signals")


# ============================================================
#  MCP 桥接引擎（运行在独立线程中）
# ============================================================
class McpBridge:
    """WebSocket ↔ MCP stdio 双向桥接器"""

    def __init__(self, endpoint: str, log_queue: queue.Queue):
        self.endpoint = endpoint
        self.log_queue = log_queue
        self._running = False
        self._ws = None
        self._proc = None
        self._tasks = []
        self._pending_notifications: list[dict] = []  # 等 WS 重连后发送的通知队列
        self._notify_lock = asyncio.Lock()           # 串行化 sampling 通知，防止多段 TTS 抢扬声器
        self._tts_cooldown_until = 0.0               # 上一段 TTS 预计播完的时间戳

    # ---------- 日志辅助 ----------
    def _log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put((ts, level, msg))

    # ---------- 生命周期 ----------
    async def run(self):
        self._running = True
        self._log("桥接引擎启动", "INFO")

        # 启动时扫描信号目录，处理任何遗留的信号文件
        await self._scan_startup_signals()

        while self._running:
            try:
                await self._one_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"桥接异常: {e}", "ERROR")
            finally:
                await self._cleanup()

            if self._running:
                self._log("3 秒后重连…", "WARN")
                await asyncio.sleep(3)

        self._log("桥接引擎已停止", "INFO")

    async def _one_cycle(self):
        # 1. 启动 MCP server 子进程（自身带 --mcp-server 参数）
        self._log("启动 MCP Server 子进程…", "INFO")
        bridge_cmd = [sys.executable]
        if not getattr(sys, 'frozen', False):
            bridge_cmd.append(__file__)          # 源码模式：python mcp_gui.py --mcp-server
        bridge_cmd.append("--mcp-server")        # EXE模式：mcp_gui.exe --mcp-server
        self._proc = await asyncio.create_subprocess_exec(
            *bridge_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=BASE_DIR,
        )

        # 后台读取 stderr
        self._tasks.append(asyncio.create_task(self._read_stderr()))

        # 2. 连接 xiaozhi.me MCP 端点
        endpoint_url = self.endpoint.strip()
        self._log(f"连接 xiaozhi.me: {endpoint_url}", "INFO")

        try:
            import websockets
            self._ws = await asyncio.wait_for(
                websockets.connect(endpoint_url, ping_interval=30, ping_timeout=10),
                timeout=30,
            )
        except asyncio.TimeoutError:
            self._log("连接 xiaozhi.me 超时（30秒），请检查端点地址是否正确", "ERROR")
            return
        except Exception as e:
            self._log(f"连接 xiaozhi.me 失败: {e}", "ERROR")
            return

        self._log("✅ 已连接到 xiaozhi.me MCP 接入点", "INFO")

        # 方案A：重连后立即发送积压的通知
        await self._drain_pending_notifications()

        # 3. 双向转发 + 信号监听
        t1 = asyncio.create_task(self._ws_to_stdio())
        t2 = asyncio.create_task(self._stdio_to_ws())
        t3 = asyncio.create_task(self._watch_signals())
        self._tasks.extend([t1, t2, t3])

        done, pending = await asyncio.wait(
            [t1, t2, t3],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    async def _ws_to_stdio(self):
        """xiaozhi.me WebSocket → MCP Server stdin"""
        try:
            async for msg in self._ws:
                if not self._running:
                    break
                self._log(f"⬇ xiaozhi.me → MCP", "DEBUG")
                self._proc.stdin.write((msg + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
        except Exception as e:
            self._log(f"WS 读取断开: {e}", "WARN")

    async def _stdio_to_ws(self):
        """MCP Server stdout → xiaozhi.me WebSocket"""
        try:
            while self._running:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if text:
                    self._log(f"⬆ MCP → xiaozhi.me", "DEBUG")
                    await self._ws.send(text)
        except Exception as e:
            self._log(f"stdio 读取断开: {e}", "WARN")

    async def _read_stderr(self):
        try:
            while self._running:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                self._log(line.decode("utf-8", errors="replace").strip(), "MCP")
        except Exception:
            pass

    async def _scan_startup_signals(self):
        """启动时扫描信号目录，处理上次运行遗留的 pending/done 文件。"""
        if not os.path.isdir(SIGNAL_DIR):
            return
        try:
            for fname in list(os.listdir(SIGNAL_DIR)):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(SIGNAL_DIR, fname)
                self._log(f"🔍 发现启动残留信号: {fname}", "INFO")
                # 移入积压队列，由 _drain_pending_notifications 在 WS 重连后处理
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    output_name = data.get("output_name", "报告")
                    save_path = data.get("save_path", "")
                    self._pending_notifications.append({
                        "output_name": output_name,
                        "save_path": save_path,
                        "pending_file": fpath if fname.startswith("pending_") else None,
                    })
                except Exception:
                    pass
        except Exception as e:
            self._log(f"启动信号扫描失败: {e}", "WARN")

    async def _watch_signals(self):
        """监听信号目录，处理 pending（待监控）和 done（已完成）两种信号文件。

        pending_*.json: write_report 启动后立即写入，桥接器轮询检测报告是否完成。
                       即使 MCP 子进程被杀死重建，pending 文件仍然存在，监控不中断。
        done_*.json:    MCP Server 后台线程的兜底通道，检测到完成即写入。

        方案A：WS 存活 → 直接发送通知；WS 断开 → 入队，等重连后补发。
        """
        while self._running:
            try:
                if os.path.isdir(SIGNAL_DIR):
                    for fname in list(os.listdir(SIGNAL_DIR)):
                        if not fname.endswith(".json"):
                            continue
                        fpath = os.path.join(SIGNAL_DIR, fname)
                        # --- 处理 pending 文件：检查报告是否完成 ---
                        if fname.startswith("pending_"):
                            try:
                                with open(fpath, "r", encoding="utf-8") as f:
                                    data = json.load(f)
                                out_dir = data.get("save_path", "")
                                output_name = data.get("output_name", "报告")

                                # 检查报告是否已生成（DOCX 优先，MD 兜底）
                                done = False
                                if os.path.isdir(out_dir):
                                    try:
                                        from datetime import datetime as _dt
                                        has_md = False
                                        for f2 in os.listdir(out_dir):
                                            fp2 = os.path.join(out_dir, f2)
                                            if f2.endswith('.docx') and os.path.getsize(fp2) >= 500:
                                                done = True
                                                docx_time = _dt.fromtimestamp(os.path.getmtime(fp2)).strftime("%H:%M:%S")
                                                self._log(
                                                    f"[timing] DOCX 文件时间: {f2} ({os.path.getsize(fp2)} bytes) = {docx_time}",
                                                    "INFO"
                                                )
                                                _cleanup_artifacts(out_dir)
                                                break
                                            if f2.endswith('.md') and os.path.getsize(fp2) >= 100:
                                                has_md = True
                                                md_time = _dt.fromtimestamp(os.path.getmtime(fp2)).strftime("%H:%M:%S")
                                                self._log(
                                                    f"[timing] MD 文件时间: {f2} ({os.path.getsize(fp2)} bytes) = {md_time}",
                                                    "INFO"
                                                )
                                        # MD 兜底：如果只有 MD 且 codebuddy 进程已退出，也视为完成
                                        if not done and has_md:
                                            # 检查是否还有 codebuddy 进程在运行
                                            cb_running = False
                                            try:
                                                import subprocess as _sp
                                                result = _sp.run(
                                                    ["tasklist", "/FI", "IMAGENAME eq node.exe", "/FO", "CSV", "/NH"],
                                                    capture_output=True, text=True, timeout=5,
                                                )
                                                # 简单检查：没有 node 进程在 out_dir 中就认为已完成
                                                cb_running = ("node.exe" in result.stdout and "codebuddy" in result.stdout.lower())
                                            except Exception:
                                                pass
                                            # 如果 MD 文件在 60 秒前就已存在（说明 codebuddy 已退出）
                                            for f2 in os.listdir(out_dir):
                                                fp2 = os.path.join(out_dir, f2)
                                                if f2.endswith('.md') and os.path.getsize(fp2) >= 100:
                                                    age = time.time() - os.path.getmtime(fp2)
                                                    if age > 60 and not cb_running:
                                                        done = True
                                                        self._log(
                                                            f"[timing] MD 兜底: 无 DOCX，但 MD 已存在 {age:.0f}s，标记完成",
                                                            "INFO"
                                                        )
                                                    break
                                    except Exception:
                                        pass

                                if done:
                                    # 记录检测到完成的时间
                                    done_time = time.strftime("%H:%M:%S")
                                    self._log(
                                        f"[timing] _watch_signals 检测到完成: {output_name} @ {done_time}",
                                        "DEBUG"
                                    )
                                    sent = await self._notify_report_done(output_name, out_dir)
                                    if sent:
                                        os.remove(fpath)
                                        self._log(f"📢 pending→完成: {output_name}", "SUCCESS")
                                    else:
                                        # WS 已断开 → 入队等待重连
                                        self._pending_notifications.append({
                                            "output_name": output_name,
                                            "save_path": out_dir,
                                            "pending_file": fpath,
                                        })
                                        self._log(
                                            f"⏳ 报告【{output_name}】已完成，WS 断开，等待重连后通知",
                                            "WARN",
                                        )
                                        self.log_queue.put((
                                            datetime.now().strftime("%H:%M:%S"),
                                            "NOTIFY_PENDING",
                                            json.dumps({"output_name": output_name, "count": len(self._pending_notifications)},
                                                       ensure_ascii=False),
                                        ))

                                # 未完成则保留 pending 文件，下次继续检查
                            except Exception as e:
                                self._log(f"处理 pending 信号失败: {e}", "ERROR")
                            continue

                        # --- 处理 done 文件（兜底通道，后台线程写入）---
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            output_name = data.get("output_name", "报告")
                            save_path = data.get("save_path", "")
                            sent = await self._notify_report_done(output_name, save_path)
                            if sent:
                                self._log(f"📢 已通知报告完成: {output_name}", "SUCCESS")
                            else:
                                self._pending_notifications.append({
                                    "output_name": output_name,
                                    "save_path": save_path,
                                    "pending_file": None,  # done 文件没有对应的 pending
                                })
                                self._log(f"⏳ done→入队: {output_name}", "WARN")
                                self.log_queue.put((
                                    datetime.now().strftime("%H:%M:%S"),
                                    "NOTIFY_PENDING",
                                    json.dumps({"output_name": output_name, "count": len(self._pending_notifications)},
                                               ensure_ascii=False),
                                ))

                        except Exception as e:
                            self._log(f"处理 done 信号失败: {e}", "ERROR")
                        finally:
                            try:
                                os.remove(fpath)
                            except Exception:
                                pass
            except Exception:
                pass
            await asyncio.sleep(3)

    async def _drain_pending_notifications(self):
        """方案A+B：重连后发送积压通知，15 秒内未成功则触发 Windows 通知（方案B兜底）"""
        if not self._pending_notifications:
            return

        self._log(f"📬 有 {len(self._pending_notifications)} 条积压通知等待发送", "INFO")
        # 清除 GUI 的待通知提示
        self.log_queue.put((
            datetime.now().strftime("%H:%M:%S"),
            "NOTIFY_CLEAR",
            "",
        ))

        deadline = asyncio.get_event_loop().time() + 15

        for note in self._pending_notifications[:]:
            try:
                remaining = max(0, deadline - asyncio.get_event_loop().time())
                if remaining <= 0:
                    break
                if self._ws:
                    sent = await asyncio.wait_for(
                        self._notify_report_done(note["output_name"], note["save_path"]),
                        timeout=remaining,
                    )
                    if sent:
                        self._pending_notifications.remove(note)
                        self._log(f"📢 积压通知已发送: {note['output_name']}", "SUCCESS")
                        # 清理对应的 pending 文件
                        pf = note.get("pending_file")
                        if pf and os.path.exists(pf):
                            try:
                                os.remove(pf)
                            except Exception:
                                pass
                    else:
                        break  # WS 又断了
                else:
                    break
            except (asyncio.TimeoutError, Exception):
                break

        # 方案B：仍有未发送的 → 触发 Windows 系统通知
        if self._pending_notifications:
            for note in self._pending_notifications[:]:
                self._log(f"🔔 报告【{note['output_name']}】无法语音通知，走系统通知通道", "WARN")
                self._trigger_fallback_notification(note)
                self._pending_notifications.remove(note)

    def _trigger_fallback_notification(self, note: dict):
        """兜底：通过 Windows 通知中心显示报告完成系统通知（不再弹窗）。"""
        output_name = note.get("output_name", "报告")
        save_path = note.get("save_path", "")
        title = "任务已完成"
        if save_path:
            message = f"《{output_name}》任务已成功完成，保存位置：{save_path}。您可以在编辑器中查看结果。"
        else:
            message = f"《{output_name}》任务已成功完成。您可以在编辑器中查看结果。"

        # 通过日志队列通知 GUI 标题栏闪烁
        self.log_queue.put((
            datetime.now().strftime("%H:%M:%S"),
            "NOTIFY_FALLBACK",
            json.dumps({"output_name": output_name, "save_path": save_path}, ensure_ascii=False),
        ))

        # 通知仅通过 Tkinter 右下角弹窗 + 系统提示音（不依赖 Windows Toast API）


    async def _notify_report_done(self, output_name: str, save_path: str = "") -> bool:
        """报告完成后，通过 MCP sampling 让 LLM 语音通知用户。

        使用 asyncio.Lock 串行化通知，确保上一段 TTS 播完才发下一条，
        避免多段语音同时到达 ESP32 导致扬声器争抢卡顿。

        同时发送 Windows 系统通知作为兜底。

        返回 True 表示成功发送，False 表示失败（WS 断开等）。
        """
        TTS_COOLDOWN_SEC = 10.0  # 冷却时间：足够云端生成 TTS + ESP32 播完一段短语音

        # --- 系统通知（与语音并行，不阻塞）---
        title = "任务已完成"
        toast_msg = f"《{output_name}》任务已完成。"
        # Tkinter 原生弹窗（最可靠的桌面通知，100% 能弹出）
        self.log_queue.put((
            datetime.now().strftime("%H:%M:%S"),
            "SHOW_POPUP",
            json.dumps({"title": title, "message": toast_msg, "save_path": save_path}, ensure_ascii=False),
        ))
        # 通知仅通过 Tkinter 右下角弹窗 + 系统提示音（不依赖 Windows Toast API）

        # --- 语音通知（串行化，防止多段 TTS 抢扬声器）---
        async with self._notify_lock:
            now = asyncio.get_event_loop().time()
            wait = self._tts_cooldown_until - now
            if wait > 0:
                self._log(
                    f"⏳ 等待上一段 TTS 播完再通知【{output_name}】（还需 {wait:.0f} 秒）",
                    "INFO",
                )
                await asyncio.sleep(wait)

            location = save_path if save_path else f"【{output_name}】文件夹"
            notify_msg = {
                "jsonrpc": "2.0",
                "id": f"report_done_{output_name}",
                "method": "sampling/createMessage",
                "params": {
                    "messages": [{
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": (
                                f"报告已经生成完毕，保存在 {location}。"
                                f"请告诉用户：'刚刚的报告写好了，保存在 {location}，不合适的我再修改。'"
                            ),
                        },
                    }],
                    "maxTokens": 150,
                },
            }
            if not self._ws:
                self._log(f"⚠ WS 已断开，语音通知【{output_name}】将入队等待重连", "WARN")
                return False
            try:
                await self._ws.send(json.dumps(notify_msg, ensure_ascii=False))
                self._tts_cooldown_until = asyncio.get_event_loop().time() + TTS_COOLDOWN_SEC
                self._log(f"📢 语音通知已发送: {output_name}", "SUCCESS")
                return True
            except Exception as e:
                self._log(f"⚠ 语音通知发送失败: {e}", "ERROR")
                return False

    async def _cleanup(self):
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    async def stop(self):
        self._running = False
        await self._cleanup()


# ============================================================
#  后台桥接线程
# ============================================================
class BridgeThread(threading.Thread):
    def __init__(self, endpoint: str, log_queue: queue.Queue):
        super().__init__(daemon=True, name="McpBridgeThread")
        self.bridge = McpBridge(endpoint, log_queue)
        self._loop = None

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.bridge.run())
        finally:
            self._loop.close()

    def stop(self):
        if self._loop and self.bridge:
            asyncio.run_coroutine_threadsafe(self.bridge.stop(), self._loop)


# ============================================================
#  GUI 主窗口
# ============================================================
class McpGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("720x560")
        self.root.minsize(600, 450)

        # 配色
        self.COLORS = {
            "bg":         "#F5F5F5",
            "card":       "#FFFFFF",
            "primary":    "#0052D9",
            "success":    "#2BA471",
            "danger":     "#D54941",
            "warn":       "#FFA700",
            "text":       "#1D1D1D",
            "text_sec":   "#666666",
            "border":     "#E7E7E7",
        }

        self.root.configure(bg=self.COLORS["bg"])

        # 状态
        self.bridge_thread: BridgeThread | None = None
        self.log_queue = queue.Queue()
        self._recent_popups: set[str] = set()  # 弹窗去重（短时间同内容只弹一次）
        self._running = False
        self.work_dir_var = tk.StringVar(value="")
        self._pending_report_count = 0  # 待通知报告计数
        self._flash_count = 0  # 标题闪烁计数

        # 构建界面
        self._setup_styles()
        self._build_ui()
        self._load_config()
        self._poll_logs()
        self._poll_task_list()

    # =================== 样式 ===================
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("Card.TFrame", background=self.COLORS["card"])

        style.configure(
            "Primary.TButton",
            background=self.COLORS["primary"],
            foreground="white",
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
        )
        style.map("Primary.TButton",
            background=[("active", "#266FE8"), ("disabled", "#CCCCCC")],
            foreground=[("disabled", "#999999")],
        )

        style.configure(
            "Danger.TButton",
            background=self.COLORS["danger"],
            foreground="white",
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
        )

        style.configure(
            "Outline.TButton",
            background=self.COLORS["card"],
            foreground=self.COLORS["primary"],
            font=("Microsoft YaHei UI", 10),
        )

        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground=self.COLORS["text_sec"], font=("Microsoft YaHei UI", 9))

        # Notebook 和 Treeview 样式
        style.configure("TNotebook", background=self.COLORS["bg"], tabmargins=[2, 4, 2, 0])
        style.configure("TNotebook.Tab", padding=[16, 6], font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", self.COLORS["card"])],
                  foreground=[("selected", self.COLORS["primary"])])
        style.configure("Task.Treeview", font=("Microsoft YaHei UI", 9), rowheight=30,
                        background=self.COLORS["card"], fieldbackground=self.COLORS["card"])
        style.configure("Task.Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    # =================== 界面布局 ===================
    def _build_ui(self):
        # --- 标题栏 ---
        header = tk.Frame(self.root, bg=self.COLORS["primary"], height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        tk.Label(
            header, text="🔌  " + APP_TITLE,
            bg=self.COLORS["primary"], fg="white",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side=tk.LEFT, padx=20, pady=12)

        self.status_var = tk.StringVar(value="未连接")
        self.status_label = tk.Label(
            header, textvariable=self.status_var,
            bg=self.COLORS["primary"], fg="#FFD700",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.status_label.pack(side=tk.RIGHT, padx=20, pady=12)

        # --- 主内容 Notebook（双标签页）---
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 8))

        # ====== TAB 1: 控制台 ======
        console_frame = tk.Frame(self.notebook, bg=self.COLORS["bg"])
        self.notebook.add(console_frame, text=" 控制台 ")

        # 操作按钮行
        btn_row = tk.Frame(console_frame, bg=self.COLORS["bg"])
        btn_row.pack(fill=tk.X, padx=4, pady=(10, 10))

        self.test_btn = tk.Button(
            btn_row, text="🔍 测试 codebuddy",
            command=self._test_codebuddy,
            bg=self.COLORS["card"], fg=self.COLORS["primary"],
            font=("Microsoft YaHei UI", 10),
            relief=tk.GROOVE, borderwidth=1,
            cursor="hand2",
        )
        self.test_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.start_btn = tk.Button(
            btn_row, text="▶  启动桥接",
            command=self._start_bridge,
            bg=self.COLORS["primary"], fg="white",
            font=("Microsoft YaHei UI", 10, "bold"),
            relief=tk.FLAT, borderwidth=0,
            padx=24, pady=6, cursor="hand2",
        )
        self.start_btn.pack(side=tk.RIGHT, padx=(4, 0))

        self.stop_btn = tk.Button(
            btn_row, text="⏹ 停止",
            command=self._stop_bridge,
            bg=self.COLORS["danger"], fg="white",
            font=("Microsoft YaHei UI", 10),
            relief=tk.FLAT, borderwidth=0,
            padx=20, pady=6, cursor="hand2",
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.RIGHT, padx=4)

        # 配置卡片
        self._build_config_card(console_frame)

        # 日志卡片
        self._build_log_card(console_frame)

        # ====== TAB 2: 任务列表 ======
        self.task_frame = tk.Frame(self.notebook, bg=self.COLORS["bg"])
        self.notebook.add(self.task_frame, text=" 任务列表 ")
        self._build_task_list(self.task_frame)

    def _build_config_card(self, parent):
        card = tk.Frame(parent, bg=self.COLORS["card"], highlightbackground=self.COLORS["border"], highlightthickness=1)
        card.pack(fill=tk.X, pady=(0, 12))

        # 标题
        tk.Label(
            card, text="⚙  配置",
            bg=self.COLORS["card"], fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor=tk.W, padx=16, pady=(12, 8))

        # MCP 接入点
        row1 = tk.Frame(card, bg=self.COLORS["card"])
        row1.pack(fill=tk.X, padx=16, pady=(0, 8))

        tk.Label(
            row1, text="MCP 接入点 URL",
            bg=self.COLORS["card"], fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor=tk.W)

        self.endpoint_var = tk.StringVar(value=DEFAULT_ENDPOINT)
        entry_frame = tk.Frame(card, bg=self.COLORS["border"], height=38)
        entry_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        self.endpoint_entry = tk.Entry(
            entry_frame,
            textvariable=self.endpoint_var,
            font=("Consolas", 10),
            bg=self.COLORS["card"],
            fg=self.COLORS["text"],
            relief=tk.FLAT,
            insertbackground=self.COLORS["primary"],
        )
        self.endpoint_entry.pack(fill=tk.BOTH, expand=True, padx=1, pady=1, ipady=4)

        tk.Label(
            card, text="💡 从 xiaozhi.me 控制台 → 智能体角色配置 → 右下角 → 复制 MCP 接入点 URL",
            bg=self.COLORS["card"], fg=self.COLORS["text_sec"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, padx=16, pady=(0, 4))

        # --- 工作目录 ---
        tk.Label(
            card, text="工作目录",
            bg=self.COLORS["card"], fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor=tk.W, padx=16, pady=(8, 4))

        dir_frame = tk.Frame(card, bg=self.COLORS["card"])
        dir_frame.pack(fill=tk.X, padx=16, pady=(0, 4))

        self.work_dir_entry = tk.Entry(
            dir_frame,
            textvariable=self.work_dir_var,
            font=("Microsoft YaHei UI", 9),
            bg=self.COLORS["card"],
            fg=self.COLORS["text"],
            relief=tk.SOLID,
            borderwidth=1,
            insertbackground=self.COLORS["primary"],
        )
        self.work_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)

        tk.Button(
            dir_frame, text="浏览...",
            command=self._choose_work_dir,
            bg=self.COLORS["primary"], fg="white",
            font=("Microsoft YaHei UI", 9),
            relief=tk.FLAT, borderwidth=0,
            padx=12, pady=4, cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(8, 0))

        tk.Label(
            card, text="💡 文件夹名从 topic 自动派生（如 2026-07-27_开发项目激励方案_V1.docx）",
            bg=self.COLORS["card"], fg=self.COLORS["text_sec"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, padx=16, pady=(0, 12))

    def _build_log_card(self, parent):
        card = tk.Frame(parent, bg=self.COLORS["card"], highlightbackground=self.COLORS["border"], highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True)

        # 标题行
        header = tk.Frame(card, bg=self.COLORS["card"])
        header.pack(fill=tk.X, padx=16, pady=(12, 4))

        tk.Label(
            header, text="📋  运行日志",
            bg=self.COLORS["card"], fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.LEFT)

        tk.Button(
            header, text="清空",
            command=self._clear_log,
            bg=self.COLORS["card"], fg=self.COLORS["text_sec"],
            font=("Microsoft YaHei UI", 9),
            relief=tk.FLAT, cursor="hand2",
            borderwidth=0,
        ).pack(side=tk.RIGHT)

        # 日志区域
        self.log_text = scrolledtext.ScrolledText(
            card,
            font=("Consolas", 9),
            bg="#1E1E1E",
            fg="#D4D4D4",
            insertbackground="white",
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=1, pady=(0, 1))

        # 配置颜色标签
        self.log_text.tag_configure("INFO",    foreground="#4EC9B0")
        self.log_text.tag_configure("WARN",    foreground="#DCDCAA")
        self.log_text.tag_configure("ERROR",   foreground="#F44747")
        self.log_text.tag_configure("DEBUG",   foreground="#808080")
        self.log_text.tag_configure("MCP",     foreground="#569CD6")
        self.log_text.tag_configure("SUCCESS", foreground="#6A9955")
        self.log_text.tag_configure("TIME",    foreground="#808080")
        self.log_text.tag_configure("NOTIFY_PENDING",  foreground="#FFA700")
        self.log_text.tag_configure("NOTIFY_FALLBACK", foreground="#FF00FF")

    # =================== 任务列表 TAB ===================
    def _build_task_list(self, parent):
        """构建任务列表 Treeview"""
        # 顶部标题栏 + 刷新按钮
        header_row = tk.Frame(parent, bg=self.COLORS["bg"])
        header_row.pack(fill=tk.X, padx=4, pady=(8, 6))

        tk.Label(
            header_row, text="📋  文档生成任务",
            bg=self.COLORS["bg"], fg=self.COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side=tk.LEFT)

        task_count_var = tk.StringVar(value="共 0 个任务")
        self.task_count_label = tk.Label(
            header_row, textvariable=task_count_var,
            bg=self.COLORS["bg"], fg=self.COLORS["text_sec"],
            font=("Microsoft YaHei UI", 9),
        )
        self.task_count_label.pack(side=tk.LEFT, padx=(16, 0))

        self.task_refresh_btn = tk.Button(
            header_row, text="🔄 刷新",
            command=self._refresh_task_list,
            bg=self.COLORS["card"], fg=self.COLORS["primary"],
            font=("Microsoft YaHei UI", 9),
            relief=tk.GROOVE, borderwidth=1,
            cursor="hand2",
        )
        self.task_refresh_btn.pack(side=tk.RIGHT)

        # Treeview 容器
        tree_card = tk.Frame(parent, bg=self.COLORS["card"],
                             highlightbackground=self.COLORS["border"], highlightthickness=1)
        tree_card.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 8))

        columns = ("topic", "folder", "status", "start", "elapsed")
        self.task_tree = ttk.Treeview(
            tree_card, columns=columns, show="headings",
            selectmode="browse",
            style="Task.Treeview",
        )
        self.task_tree.heading("topic", text="主题", anchor=tk.W)
        self.task_tree.heading("folder", text="文件夹", anchor=tk.W)
        self.task_tree.heading("status", text="状态", anchor=tk.CENTER)
        self.task_tree.heading("start", text="开始时间", anchor=tk.CENTER)
        self.task_tree.heading("elapsed", text="耗时", anchor=tk.CENTER)

        self.task_tree.column("topic", width=180, minwidth=100)
        self.task_tree.column("folder", width=160, minwidth=80)
        self.task_tree.column("status", width=80, anchor=tk.CENTER)
        self.task_tree.column("start", width=100, anchor=tk.CENTER)
        self.task_tree.column("elapsed", width=80, anchor=tk.CENTER)

        # 滚动条
        scrollbar = ttk.Scrollbar(tree_card, orient=tk.VERTICAL, command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=scrollbar.set)
        self.task_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(1, 0), pady=1)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 1), pady=1)

        # 状态颜色标签
        self.task_tree.tag_configure("running", foreground="#0052D9")     # 蓝色-生成中
        self.task_tree.tag_configure("done", foreground="#2BA471")        # 绿色-完成
        self.task_tree.tag_configure("failed", foreground="#D54941")      # 红色-失败
        self.task_tree.tag_configure("pending", foreground="#FFA700")     # 橙色-排队中

        # 空状态提示
        self.task_empty_label = tk.Label(
            tree_card, text="暂无任务\n\n通过语音说「帮我写一份XXX报告」即可创建任务",
            bg=self.COLORS["card"], fg=self.COLORS["text_sec"],
            font=("Microsoft YaHei UI", 11),
            justify=tk.CENTER,
        )
        self.task_empty_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def _refresh_task_list(self):
        """扫描 SIGNAL_DIR 和 work_dir 重建任务列表"""
        # 清除现有行
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)

        tasks = {}  # key: folder_name, value: task info dict
        now = time.time()

        # 1. 扫描 work_dir 中所有 YYYY-MM-DD_* 文件夹
        work_dir = self.work_dir_var.get().strip()
        if not work_dir:
            work_dir = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        if os.path.isdir(work_dir):
            for folder_name in os.listdir(work_dir):
                folder_path = os.path.join(work_dir, folder_name)
                if not os.path.isdir(folder_path):
                    continue
                # 匹配日期前缀: YYYY-MM-DD_
                if len(folder_name) < 11 or folder_name[4] != '-' or folder_name[7] != '-':
                    continue
                date_part = folder_name[:10]
                try:
                    datetime.strptime(date_part, "%Y-%m-%d")
                except ValueError:
                    continue

                # 检查文件
                docx_files = []
                md_files = []
                for f in os.listdir(folder_path):
                    if f.endswith('.docx') and os.path.getsize(os.path.join(folder_path, f)) > 500:
                        docx_files.append(f)
                    if f.endswith('.md') and os.path.getsize(os.path.join(folder_path, f)) > 100:
                        md_files.append(f)

                # 从文件夹名提取主题
                topic = folder_name[11:]  # 去掉 "YYYY-MM-DD_"

                # 判断状态
                if docx_files:
                    status = "已完成"
                    status_tag = "done"
                elif md_files:
                    status = "生成中"
                    status_tag = "running"
                else:
                    status = "处理中"
                    status_tag = "pending"

                # 用文件夹修改时间作为开始时间
                folder_mtime = os.path.getmtime(folder_path)
                start_time_str = time.strftime("%H:%M:%S", time.localtime(folder_mtime))
                elapsed_str = self._format_elapsed(now - folder_mtime)

                tasks[folder_name] = {
                    "topic": topic,
                    "folder": folder_name,
                    "status": status,
                    "tag": status_tag,
                    "start": start_time_str,
                    "elapsed": elapsed_str,
                }

        # 2. 扫描 SIGNAL_DIR 中的 pending 信号，更新状态
        if os.path.isdir(SIGNAL_DIR):
            for sig_file in os.listdir(SIGNAL_DIR):
                if not sig_file.startswith("pending_"):
                    continue
                sig_path = os.path.join(SIGNAL_DIR, sig_file)
                try:
                    with open(sig_path, "r", encoding="utf-8") as sf:
                        data = json.load(sf)
                    output_name = data.get("output_name", "")
                    date_str = datetime.now().strftime("%Y-%m-%d")
                    folder_name = f"{date_str}_{output_name}"  # 新格式
                    # 检查是否已在任务列表中
                    found = False
                    for fn in tasks:
                        if fn.startswith(date_str) and output_name in fn:
                            if tasks[fn]["status"] == "已完成":
                                # 已是完成状态，删除 pending 信号
                                try:
                                    os.remove(sig_path)
                                except Exception:
                                    pass
                            elif tasks[fn]["status"] in ("处理中", "生成中"):
                                tasks[fn]["status"] = "生成中"
                                tasks[fn]["tag"] = "running"
                            found = True
                            break
                    if not found:
                        tasks[folder_name] = {
                            "topic": output_name,
                            "folder": folder_name,
                            "status": "生成中",
                            "tag": "running",
                            "start": time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(sig_path))),
                            "elapsed": self._format_elapsed(now - os.path.getmtime(sig_path)),
                        }
                except (json.JSONDecodeError, KeyError):
                    pass

        # 3. 填充 Treeview（按开始时间倒序）
        sorted_tasks = sorted(tasks.values(), key=lambda t: t["start"], reverse=True)
        for t in sorted_tasks:
            self.task_tree.insert("", tk.END, values=(
                t["topic"], t["folder"], t["status"], t["start"], t["elapsed"],
            ), tags=(t["tag"],))

        # 更新统计
        task_count_var = self.task_count_label.cget("textvariable")
        task_count_var.set(f"共 {len(sorted_tasks)} 个任务")

        # 显示/隐藏空状态
        if sorted_tasks:
            self.task_empty_label.place_forget()
        else:
            self.task_empty_label.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def _format_elapsed(self, seconds: float) -> str:
        """格式化耗时"""
        if seconds < 0:
            return "—"
        if seconds < 60:
            return f"{int(seconds)}秒"
        elif seconds < 3600:
            return f"{int(seconds // 60)}分{int(seconds % 60)}秒"
        else:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}时{m}分"

    def _poll_task_list(self):
        """每 3 秒刷新一次任务列表（仅在任务列表 TAB 可见时）"""
        try:
            current_tab = self.notebook.index(self.notebook.select())
            if current_tab == 1:  # 任务列表 tab
                self._refresh_task_list()
        except Exception:
            pass
        self.root.after(3000, self._poll_task_list)

    # =================== 配置持久化 ===================
    def _load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.endpoint_var.set(cfg.get("endpoint", ""))
                self.work_dir_var.set(cfg.get("work_dir", ""))
                self._log_gui("INFO", "已加载保存的配置")
        except Exception as e:
            self._log_gui("WARN", f"加载配置失败: {e}")

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "endpoint": self.endpoint_var.get().strip(),
                    "work_dir": self.work_dir_var.get().strip(),
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log_gui("ERROR", f"保存配置失败: {e}")

    def _choose_work_dir(self):
        """打开文件夹选择对话框，设置工作目录"""
        initial = self.work_dir_var.get().strip()
        if not initial or not os.path.isdir(initial):
            initial = os.path.expanduser("~")
        dir_path = filedialog.askdirectory(
            title="选择工作目录（生成的文档将保存到此目录）",
            initialdir=initial,
            parent=self.root,
        )
        if dir_path:
            self.work_dir_var.set(dir_path)
            self._save_config()
            self._log_gui("INFO", f"工作目录已设置为: {dir_path}")

    # =================== 桥接控制 ===================
    def _start_bridge(self):
        endpoint = self.endpoint_var.get().strip()
        if not endpoint:
            messagebox.showwarning("配置缺失", "请先填写 xiaozhi.me MCP 接入点 URL！")
            return
        if not endpoint.startswith(("ws://", "wss://")):
            messagebox.showwarning("格式错误", "MCP 接入点 URL 应以 ws:// 或 wss:// 开头")
            return

        self._save_config()

        # EXE 模式下用 --mcp-server 子进程；源码模式下也用 --mcp-server 参数
        self._log_gui("INFO", "正在启动桥接服务…")

        self.bridge_thread = BridgeThread(endpoint, self.log_queue)
        self.bridge_thread.start()

        self._running = True
        self._set_ui_state(running=True)

    def _stop_bridge(self):
        if self.bridge_thread:
            self._log_gui("INFO", "正在停止桥接服务…")
            self.bridge_thread.stop()
            self.bridge_thread = None

        self._running = False
        self._set_ui_state(running=False)
        self._log_gui("INFO", "桥接服务已停止")

    def _set_ui_state(self, running: bool):
        if running:
            self.status_var.set("● 运行中")
            self.status_label.config(fg="#2BA471")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.endpoint_entry.config(state=tk.DISABLED)
        else:
            self.status_var.set("○ 未连接")
            self.status_label.config(fg="#999999")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.endpoint_entry.config(state=tk.NORMAL)

    # =================== 测试 codebuddy ===================
    def _test_codebuddy(self):
        self._log_gui("INFO", "正在查找 codebuddy CLI …")
        self.test_btn.config(state=tk.DISABLED, text="测试中…")

        def run_test():
            try:
                found = find_codebuddy()
                if not found:
                    self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "ERROR",
                                        "未找到 codebuddy！请确认已安装 CodeBuddy CLI (npm install -g codebuddy)"))
                    return
                exe, prefix = found
                cmd = [exe] + prefix + ["--version"]
                self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "INFO",
                                    f"找到 codebuddy: {' '.join(cmd)}"))
                kwargs2 = dict(capture_output=True, text=True, timeout=10)
                if sys.platform == "win32":
                    kwargs2["creationflags"] = subprocess.CREATE_NO_WINDOW
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = subprocess.SW_HIDE
                    kwargs2["startupinfo"] = si
                result = subprocess.run(cmd, env=_get_fixed_env(), **kwargs2)
                if result.returncode == 0:
                    ver = result.stdout.strip().split("\n")[0] if result.stdout else "OK"
                    self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "SUCCESS",
                                        f"codebuddy 可用，版本: {ver}"))
                else:
                    self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "ERROR",
                                        f"codebuddy 返回错误: {result.stderr[:200]}"))
            except Exception as e:
                self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "ERROR",
                                    f"测试失败: {e}"))

            # 也检测 pypandoc
            try:
                import pypandoc
                pandoc_ver = getattr(pypandoc, '__version__', 'installed')
                self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "SUCCESS",
                                    f"pypandoc {pandoc_ver} 可用, MD→DOCX 秒级转换"))
            except ImportError:
                self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "WARN",
                                    "pypandoc 未安装, 将只生成 MD 文件"))
            except Exception:
                self.log_queue.put((datetime.now().strftime("%H:%M:%S"), "WARN",
                                    "无法检测 pypandoc，将只生成 MD 文件"))

        threading.Thread(target=run_test, daemon=True).start()
        # 恢复按钮
        self.root.after(3000, lambda: self.test_btn.config(
            state=tk.NORMAL, text="🔍 测试 codebuddy"
        ))

    # =================== 日志 ===================
    def _log_gui(self, level: str, msg: str):
        self.log_queue.put((datetime.now().strftime("%H:%M:%S"), level, msg))

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_logs(self):
        """每 200ms 从队列取日志并显示，同时处理特殊通知级别"""
        try:
            while True:
                ts, level, msg = self.log_queue.get_nowait()
                # --- 特殊通知处理 ---
                if level == "NOTIFY_PENDING":
                    # 有报告完成但 WS 断开，显示待通知状态
                    self._pending_report_count += 1
                    self._flash_title_bar()
                    self._append_log(ts, "NOTIFY_PENDING",
                                     f"🔔 有 {self._pending_report_count} 个报告待通知（WS 重连后自动推送）")
                    continue
                elif level == "NOTIFY_CLEAR":
                    self._pending_report_count = 0
                    self.root.title(APP_TITLE)
                    continue
                elif level == "SHOW_POPUP":
                    # Tkinter 原生弹窗（最可靠的桌面通知）
                    try:
                        data = json.loads(msg)
                        self._show_toast_popup(
                            data.get("title", "任务已完成"),
                            data.get("message", ""),
                            data.get("save_path", "")
                        )
                    except Exception:
                        self._show_toast_popup("任务已完成", "报告已生成，请在桌面查看。")
                    continue
                elif level == "NOTIFY_FALLBACK":
                    # 方案B：无法语音通知，触发通知（Tkinter弹窗 + Windows Toast）
                    self._pending_report_count = max(0, self._pending_report_count - 1)
                    try:
                        data = json.loads(msg)
                        out_name = data.get("output_name", "报告")
                        save_path = data.get("save_path", "")
                    except Exception:
                        out_name = "报告"
                        save_path = ""
                    self._append_log(ts, "NOTIFY_FALLBACK",
                                     f"🔔 报告【{out_name}】已完成\n   📁 {save_path}")
                    # 闪烁标题栏 & 状态栏
                    self._flash_title_bar()
                    self.status_var.set("🔔 报告已生成！")
                    self.status_label.config(fg="#FFA700")
                    # ★ Tkinter 原生弹窗（最可靠的桌面通知）
                    # 格式与 SHOW_POPUP 保持一致，确保去重机制能覆盖
                    toast_msg = f"《{out_name}》任务已完成。"
                    self._show_toast_popup("任务已完成", toast_msg, save_path)
                    def _reset_status():
                        if self._pending_report_count == 0:
                            self.status_var.set("● 运行中")
                            self.status_label.config(fg="#2BA471")
                    self.root.after(5000, _reset_status)
                    continue

                self._append_log(ts, level, msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_logs)

    def _append_log(self, ts: str, level: str, msg: str):
        self.log_text.config(state=tk.NORMAL)
        # 时间
        self.log_text.insert(tk.END, f"{ts}  ", "TIME")
        # 级别 + 内容
        tag = level if level in ("INFO", "WARN", "ERROR", "DEBUG", "MCP", "SUCCESS", "NOTIFY_PENDING", "NOTIFY_FALLBACK") else "INFO"
        self.log_text.insert(tk.END, f"[{level}] ", tag)
        self.log_text.insert(tk.END, f"{msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _draw_rounded_rect(self, canvas, x1, y1, x2, y2, r, fill_color, outline_color=""):
        """在 Canvas 上绘制圆角矩形（高密度多边形采样圆弧，消除锯齿）。
        
        Tkinter create_arc 不支持抗锯齿，圆弧边缘全是像素级阶梯。
        改用 create_polygon 以每圆角 36 个采样点逼近圆弧，
        每个像素步进 < 1px，人眼无法分辨阶梯。"""
        import math
        points = []
        steps = max(18, int(r * 3))  # 采样点数 = 半径 × 3，最少 18 个

        # 四个圆心坐标
        cx = [x1 + r, x2 - r, x2 - r, x1 + r]  # TL TR BR BL
        cy = [y1 + r, y1 + r, y2 - r, y2 - r]
        # 每个圆角的起始角度（弧度）
        start_angles = [math.pi, 3 * math.pi / 2, 0, math.pi / 2]

        for corner in range(4):
            sa = start_angles[corner]
            for i in range(steps + 1):
                a = sa + (math.pi / 2) * i / steps
                points.extend([cx[corner] + r * math.cos(a),
                               cy[corner] + r * math.sin(a)])

        items = []
        items.append(canvas.create_polygon(
            points, fill=fill_color, outline=fill_color, width=0,
            smooth=False))
        if outline_color:
            items.append(canvas.create_polygon(
                points, fill="", outline=outline_color, width=1))
        return items

    def _show_toast_popup(self, title: str, message: str, save_path: str = ""):
        """右下角弹窗通知：Canvas 绘制圆角，浅色背景，带系统提示音，用户手动关闭。

        布局：
          ┌─────────────────────────────────┐
          │  ● 任务已完成              ✕   │  ← 标题 + 关闭按钮
          │                                │
          │  《XXX》任务已完成。            │  ← 消息文字
          │  保存位置"E:/.../..." 打开目录  │  ← 路径 + 按钮
          └─────────────────────────────────┘
        """
        # 去重：相同内容的弹窗短时间内只弹一次
        popup_key = f"{title}|{message}"
        if popup_key in self._recent_popups:
            return
        self._recent_popups.add(popup_key)
        # 5 秒后清除去重记录
        self.root.after(5000, lambda: self._recent_popups.discard(popup_key))
        # 播放系统提示音（多重兜底）
        try:
            winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NOSTOP)
        except Exception:
            try:
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NOSTOP)
            except Exception:
                try:
                    winsound.Beep(1000, 300)
                except Exception:
                    pass

        # 浅色配色
        card_w, card_h = 430, 130
        radius = 12
        total_w, total_h = card_w + 2, card_h + 2

        bg_color = "#FFFFFF"
        fg_color = "#1D1D1D"
        sub_color = "#666666"
        accent = "#0052D9"
        link_color = "#0052D9"
        transparent = "#FF00FF"

        popup = tk.Toplevel(self.root)
        popup.title("")
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.attributes("-transparentcolor", transparent)
        popup.configure(bg=transparent)

        # 定位到屏幕右下角
        sw = popup.winfo_screenwidth()
        sh = popup.winfo_screenheight()
        margin = 30
        x = sw - total_w - margin
        y = sh - total_h - margin - 40
        popup.geometry(f"{total_w}x{total_h}+{x}+{y}")

        canvas = tk.Canvas(popup, width=total_w, height=total_h,
                           bg=transparent, highlightthickness=0)
        canvas.pack()

        # 白色圆角卡片（带细边框）
        self._draw_rounded_rect(canvas, 1, 1, card_w + 1, card_h + 1, radius, "#E5E5E5")
        self._draw_rounded_rect(canvas, 2, 2, card_w, card_h, radius - 1, bg_color)

        # 品牌色装饰条（左侧圆角竖条）
        bar_x, bar_w = 18, 4
        bar_y1, bar_y2 = 24, card_h - 24
        canvas.create_rectangle(bar_x, bar_y1, bar_x + bar_w, bar_y2,
                                fill=accent, outline=accent, width=0)

        # ========== 标题行（顶部） ==========
        canvas.create_text(34, 22, text=title, fill=fg_color,
                           font=("Microsoft YaHei UI", 13, "bold"),
                           anchor="nw")

        # ========== 消息行 ==========
        # message 如 "《XXX》任务已成功完成。" 显示在标题下方
        msg_y = 52
        canvas.create_text(34, msg_y, text=message, fill=sub_color,
                           font=("Microsoft YaHei UI", 10),
                           anchor="nw", width=card_w - 68)

        # ========== 底部操作行：路径 + "打开目录"按钮 + 关闭按钮 ==========
        bottom_y = card_h - 28  # 底部操作行 Y 坐标

        if save_path:
            import os as _os
            dir_path = _os.path.dirname(save_path) if _os.path.isfile(save_path) else save_path
            # 路径文本（左对齐）
            if _os.path.exists(dir_path):
                # 截断路径，避免太长
                display_path = dir_path
                # 估算路径文本宽度，如果太长就截断
                max_path_chars = 42
                if len(display_path) > max_path_chars:
                    display_path = "..." + display_path[-(max_path_chars - 3):]

                path_text = f"保存位置 \"{display_path}\"，"
                path_id = canvas.create_text(34, bottom_y, text=path_text,
                                             fill=sub_color,
                                             font=("Microsoft YaHei UI", 9),
                                             anchor="nw")
                # 测量路径文本宽度，确定"打开目录"按钮位置
                bbox = canvas.bbox(path_id)
                path_text_right = bbox[2] if bbox else 280

                # "打开目录" 文字按钮（蓝色可点击，紧挨路径文本右侧）
                open_text = "打开目录"
                open_id = canvas.create_text(path_text_right + 4, bottom_y, text=open_text,
                                             fill=link_color,
                                             font=("Microsoft YaHei UI", 9, "underline"),
                                             anchor="nw")
                canvas.tag_bind(open_id, "<Button-1>",
                                lambda e, p=dir_path: _os.startfile(p))
                def _open_enter(e, oid=open_id):
                    canvas.itemconfig(oid, fill="#266FE8")
                    canvas.config(cursor="hand2")
                def _open_leave(e, oid=open_id):
                    canvas.itemconfig(oid, fill=link_color)
                    canvas.config(cursor="")
                canvas.tag_bind(open_id, "<Enter>", _open_enter)
                canvas.tag_bind(open_id, "<Leave>", _open_leave)

        # ========== 关闭按钮（右上角，标题行高度） ==========
        close_x, close_y = card_w - 16, 16
        close_id = canvas.create_text(close_x, close_y, text="✕",
                                       fill="#999999", font=("Microsoft YaHei UI", 14),
                                       anchor="ne")
        canvas.tag_bind(close_id, "<Button-1>", lambda e: popup.destroy())
        def _close_enter(e): canvas.itemconfig(close_id, fill="#333333")
        def _close_leave(e): canvas.itemconfig(close_id, fill="#999999")
        canvas.tag_bind(close_id, "<Enter>", _close_enter)
        canvas.tag_bind(close_id, "<Leave>", _close_leave)

        # 淡入效果
        popup.attributes("-alpha", 0.0)
        def _fade_in(step=0):
            alpha = min(1.0, step * 0.2)
            try:
                popup.attributes("-alpha", alpha)
            except tk.TclError:
                return
            if step < 5:
                popup.after(30, lambda: _fade_in(step + 1))
        popup.after(10, _fade_in)

    def _flash_title_bar(self):
        """方案B：闪烁窗口标题栏提示用户有报告完成"""
        if self._flash_count >= 6:  # 闪3次（开-关-开-关-开-关）
            self._flash_count = 0
            if self._pending_report_count > 0:
                self.root.title(f"🔔 [{self._pending_report_count}] {APP_TITLE}")
            else:
                self.root.title(APP_TITLE)
            return
        if self._flash_count % 2 == 0:
            self.root.title(f"🔔 [{self._pending_report_count}] {APP_TITLE}")
        else:
            self.root.title(APP_TITLE)
        self._flash_count += 1
        self.root.after(500, self._flash_title_bar)

    # =================== 窗口关闭 ===================
    def on_close(self):
        if self._running:
            if messagebox.askokcancel("确认退出", "桥接正在运行中，确定要退出吗？"):
                self._stop_bridge()
            else:
                return
        self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()


# ============================================================
#  MCP Server 模式（--mcp-server 参数）
# ============================================================
def run_mcp_server():
    """以 stdio 传输运行 MCP Server，供桥接器子进程调用"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("WorkBuddy")

    import re as _re

    def _topic_to_name(topic: str, max_len: int = 30) -> str:
        """将话题转为合法的文件夹/文件名（不含日期前缀）。"""
        name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', topic)
        name = _re.sub(r'_+', '_', name)
        name = name.strip('_')
        if len(name) > max_len:
            name = name[:max_len]
        return name or "文档"

    @mcp.tool()
    def write_to_file(topic: str, version: str = "1") -> dict:
        """写文档工具。

        当用户要写报告、文档、文章、方案、总结、调研时，立即调用本工具。
        工具会在后台生成文件保存到桌面。
        文件夹名和文件名都从 topic 自动派生（日期_话题名称_V版本号）。
        调用后你只需要告诉用户"已经安排好了"。

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
        file_name_docx = f"{folder_name}_V{ver}.docx"
        while os.path.exists(os.path.join(out_dir, file_name_docx)):
            ver += 1
            file_name_md = f"{folder_name}_V{ver}.md"
            file_name_docx = f"{folder_name}_V{ver}.docx"

        prompt = (
            f"请撰写以下内容：{topic}。"
            f"内容详实、结构清晰。"
            f"请将最终文件保存为 Markdown 文档（.md）到：{out_dir}\\{file_name_md}"
        )

        found = find_codebuddy()
        if not found:
            print(f"[write_to_file] ⚠ find_codebuddy() 返回 None", file=sys.stderr, flush=True)
            return {
                "success": False,
                "error": "未找到 codebuddy 命令，请确认已安装 CodeBuddy CLI。",
                "message": "抱歉，后台写作工具没找到，请确认电脑上已安装 CodeBuddy CLI。",
            }
        exe, prefix = found
        cmd = [exe] + prefix + ["-p", "--permission-mode", "bypassPermissions", prompt]

        # --- 启动 codebuddy 后台进程 ---
        start_time = time.time()
        print(f"[write_to_file] prompt 长度: {len(prompt)} 字符, cmd: {' '.join(str(x) for x in cmd[:5])}...",
              file=sys.stderr, flush=True)
        try:
            log_path = os.path.join(out_dir, "log.txt")
            # 注意：Popen 的 stdout/stderr 不能指向 with 块内的文件对象，
            # 否则 with 退出后句柄关闭，子进程写入会崩溃。
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write(f"=== 启动时间: {time.strftime('%H:%M:%S', time.localtime(start_time))} ===\n")
                lf.write(f"prompt 长度: {len(prompt)} 字符\n")
                lf.write(f"cmd: {' '.join(cmd)}\n\n")
            popen_kwargs = dict(
                cwd=out_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                popen_kwargs["startupinfo"] = si
            proc = subprocess.Popen(cmd, env=_get_fixed_env(), **popen_kwargs)
            print(f"[write_to_file] ✅ codebuddy 已启动 (pid={proc.pid}, prompt={len(prompt)}字)",
                  file=sys.stderr, flush=True)

            # --- 监控 codebuddy 进程退出（每30s心跳 + 退出时 pypandoc 转换）---
            def _monitor_process():
                MAX_RUNTIME = 600  # 总超时10分钟，超时后强杀
                while proc.poll() is None:
                    try:
                        proc.wait(timeout=30)  # 每30秒检查一次
                    except subprocess.TimeoutExpired:
                        pass  # 进程仍在运行，继续心跳
                    now = time.time()
                    if proc.poll() is None:
                        elapsed_now = now - start_time
                        if elapsed_now > MAX_RUNTIME:
                            print(f"[write_to_file] ⚠️ codebuddy 超时 ({elapsed_now:.0f}s)，强制终止",
                                  file=sys.stderr, flush=True)
                            try:
                                proc.kill()
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                            _safe_log(log_path, f"\n[超时终止] 已运行 {elapsed_now:.0f}s, 超过上限 {MAX_RUNTIME}s, 已强杀\n")
                            break
                        print(f"[cb-heartbeat] 仍在运行... pid={proc.pid}, 已耗时 {elapsed_now:.0f}s",
                              file=sys.stderr, flush=True)
                        _safe_log(log_path,
                                  f"\n[HEARTBEAT {time.strftime('%H:%M:%S')}] 仍在运行, 已耗时 {elapsed_now:.0f}s\n")
                # --- 进程已退出（或被强杀）---
                exit_time = time.time()
                elapsed = exit_time - start_time
                exit_msg = (
                    f"[write_to_file] ⏱ codebuddy 进程退出 "
                    f"(pid={proc.pid}, 耗时 {elapsed:.1f}s, 退出码={proc.returncode})"
                )
                _safe_print(exit_msg)
                _safe_log(log_path, f"\n=== 进程退出: {time.strftime('%H:%M:%S', time.localtime(exit_time))} ===\n"
                         f"codebuddy 耗时: {elapsed:.1f}s, 退出码: {proc.returncode}\n")
                # 检查输出目录文件列表
                try:
                    if os.path.isdir(out_dir):
                        items = os.listdir(out_dir)
                        for item in items:
                            try:
                                size = os.path.getsize(os.path.join(out_dir, item))
                                _safe_print(f"[write_to_file] 📄 {item} ({size} bytes)")
                            except Exception:
                                pass
                except Exception:
                    pass
                # --- 找到实际的 MD 文件（codebuddy 可能不按指定文件名保存）---
                md_path = os.path.join(out_dir, file_name_md)
                docx_path = os.path.join(out_dir, file_name_docx)
                if not os.path.exists(md_path) or os.path.getsize(md_path) < 100:
                    # 兜底：搜索 out_dir 中所有的 .md 文件
                    try:
                        all_items = os.listdir(out_dir)
                        found_md = None
                        for item in all_items:
                            if item.endswith('.md') and item != file_name_md:
                                alt_path = os.path.join(out_dir, item)
                                if os.path.getsize(alt_path) >= 100:
                                    found_md = alt_path
                                    break
                        if found_md:
                            # 重命名为期望的文件名
                            os.rename(found_md, md_path)
                            _safe_print(f"[write_to_file] 🔄 MD 文件重命名: {os.path.basename(found_md)} → {file_name_md}")
                            _safe_log(log_path, f"\nMD 重命名: {os.path.basename(found_md)} → {file_name_md}\n")
                    except Exception as e:
                        _safe_print(f"[write_to_file] ⚠️ 查找/重命名 MD 文件异常: {e}")
                # --- pypandoc 秒转 DOCX（本地转换，无网络调用）---
                if os.path.exists(md_path) and os.path.getsize(md_path) > 100:
                    p_start = time.time()
                    try:
                        # 确保 pandoc 在 PATH 中（EXE 包中可能需要手动设置）
                        pandoc_candidates = [
                            os.path.join(os.path.dirname(sys.executable), "pandoc.exe"),  # EXE 同目录
                            r"C:\tools\pandoc",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "pandoc"),
                        ]
                        for pc in pandoc_candidates:
                            if os.path.isdir(pc):
                                os.environ["PATH"] = pc + ";" + os.environ["PATH"]
                            elif pc.endswith(".exe") and os.path.exists(pc):
                                os.environ["PATH"] = os.path.dirname(pc) + ";" + os.environ["PATH"]
                        import pypandoc
                        pypandoc.convert_file(md_path, 'docx', outputfile=docx_path)
                        p_elapsed = time.time() - p_start
                        if os.path.exists(docx_path):
                            docx_size = os.path.getsize(docx_path)
                            pandoc_msg = (
                                f"[write_to_file] 🚀 pypandoc 本地转换完成: MD→DOCX "
                                f"(耗时 {p_elapsed:.1f}s, DOCX={docx_size} bytes)"
                            )
                            _safe_print(pandoc_msg)
                            _safe_log(log_path, f"\npypandoc 本地转换: {p_elapsed:.1f}s, DOCX={docx_size} bytes, 成功\n")
                    except Exception as e:
                        p_elapsed = time.time() - p_start
                        _safe_print(f"[write_to_file] ⚠️ pypandoc 异常 ({p_elapsed:.1f}s): {e}")
                        _safe_log(log_path, f"\npypandoc 异常: {e}\n")
                # 清理临时文件
                _cleanup_artifacts(out_dir)
                # --- 写 done 信号（无论 DOCX 是否生成成功，有 MD 就行）---
                try:
                    has_output = False
                    if os.path.exists(docx_path) and os.path.getsize(docx_path) >= 500:
                        has_output = True
                    elif os.path.exists(md_path) and os.path.getsize(md_path) >= 100:
                        has_output = True
                    if has_output:
                        os.makedirs(SIGNAL_DIR, exist_ok=True)
                        sig_name = f"done_{topic_name}_{int(time.time())}.json"
                        sig_path = os.path.join(SIGNAL_DIR, sig_name)
                        with open(sig_path, "w", encoding="utf-8") as sf:
                            json.dump({
                                "output_name": topic_name,
                                "save_path": out_dir,
                            }, sf, ensure_ascii=False)
                        _safe_print(f"[write_to_file] ✅ done 信号已写入: {sig_name}")
                except Exception as e:
                    _safe_print(f"[write_to_file] ⚠️ 写 done 信号异常: {e}")
            threading.Thread(target=_monitor_process, daemon=True, name=f"cb-monitor-{topic_name}").start()

        except Exception as e:
            print(f"[write_to_file] ❌ 启动失败: {e}", file=sys.stderr, flush=True)
            return {
                "success": False,
                "error": f"启动 codebuddy 失败: {e}",
                "message": "抱歉，后台写作工具启动失败了，请查看 WorkBuddy MCP 日志。",
            }

        # --- 立即写 pending 信号文件（桥接器负责轮询检测完成）---
        # 这是主通道：pending 文件持久存在于 SIGNAL_DIR，
        # 即使 MCP 子进程因桥接器重连被杀死，桥接器仍能继续监控。
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        pending_sig = os.path.join(SIGNAL_DIR, f"pending_{topic_name}.json")
        with open(pending_sig, "w", encoding="utf-8") as psf:
            json.dump({
                "output_name": topic_name,
                "save_path": out_dir,
            }, psf, ensure_ascii=False)

        # --- 后台线程：兜底通道（桥接器主通道失效时补位）---
        def _watcher():
            import time as _t
            MAX_WAIT = 600
            CHECK_INTERVAL = 5
            elapsed = 0
            _md_seen_at = None  # 首次发现 MD 的时间
            _tried_pandoc = False  # 是否已经尝试过 pypandoc
            while elapsed < MAX_WAIT:
                _t.sleep(CHECK_INTERVAL)
                elapsed += CHECK_INTERVAL
                try:
                    if not os.path.isdir(out_dir):
                        continue
                    files = os.listdir(out_dir)
                    doc_done = False
                    md_found = None
                    md_path_found = None
                    for f in files:
                        fpath = os.path.join(out_dir, f)
                        if f.endswith('.docx') and os.path.getsize(fpath) >= 500:
                            doc_done = True
                            break
                        if f.endswith('.md') and os.path.getsize(fpath) >= 100:
                            md_found = f
                            md_path_found = fpath
                    if doc_done:
                        _cleanup_artifacts(out_dir)
                        docx_time_val = _t.strftime("%H:%M:%S", _t.localtime(_t.time()))
                        print(f"[watcher] ⏱ 兜底检测到报告完成 (DOCX): {topic_name}, 检测时间 {docx_time_val}, 耗时 {elapsed}s",
                              file=sys.stderr, flush=True)
                        sig_name = f"done_{topic_name}_{int(_t.time())}.json"
                        sig_path = os.path.join(SIGNAL_DIR, sig_name)
                        with open(sig_path, "w", encoding="utf-8") as sf:
                            json.dump({
                                "output_name": topic_name,
                                "save_path": out_dir,
                            }, sf, ensure_ascii=False)
                        return
                    # --- MD 文件存在但没有 DOCX ---
                    if md_found and not doc_done:
                        if _md_seen_at is None:
                            _md_seen_at = _t.time()
                            print(f"[watcher] ⏱ MD 文件检测到: {md_found} ({os.path.getsize(md_path_found)} bytes), 耗时 {elapsed}s",
                                  file=sys.stderr, flush=True)
                        md_age = _t.time() - _md_seen_at
                        # 如果 MD 存在超过 60s 且没尝试过 pypandoc，尝试转换
                        if md_age > 60 and not _tried_pandoc:
                            _tried_pandoc = True
                            docx_path = os.path.join(out_dir, file_name_docx)
                            print(f"[watcher] 🔄 尝试 pypandoc 转换: {md_found} → {file_name_docx}",
                                  file=sys.stderr, flush=True)
                            try:
                                # 确保 pandoc 在 PATH 中
                                pandoc_candidates = [
                                    os.path.join(os.path.dirname(sys.executable), "pandoc.exe"),
                                    r"C:\tools\pandoc",
                                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pandoc"),
                                ]
                                for pc in pandoc_candidates:
                                    if os.path.isdir(pc):
                                        os.environ["PATH"] = pc + ";" + os.environ["PATH"]
                                    elif pc.endswith(".exe") and os.path.exists(pc):
                                        os.environ["PATH"] = os.path.dirname(pc) + ";" + os.environ["PATH"]
                                import pypandoc
                                pypandoc.convert_file(md_path_found, 'docx', outputfile=docx_path)
                                if os.path.exists(docx_path) and os.path.getsize(docx_path) >= 500:
                                    print(f"[watcher] 🚀 pypandoc 转换成功! {os.path.getsize(docx_path)} bytes",
                                          file=sys.stderr, flush=True)
                                    _cleanup_artifacts(out_dir)
                                    sig_name = f"done_{topic_name}_{int(_t.time())}.json"
                                    sig_path = os.path.join(SIGNAL_DIR, sig_name)
                                    with open(sig_path, "w", encoding="utf-8") as sf:
                                        json.dump({
                                            "output_name": topic_name,
                                            "save_path": out_dir,
                                        }, sf, ensure_ascii=False)
                                    return
                            except Exception as e:
                                print(f"[watcher] ⚠️ pypandoc 转换失败: {e}", file=sys.stderr, flush=True)
                        # 如果 MD 存在超过 120s 且 pypandoc 也试过了，以 MD 完成
                        if md_age > 120:
                            print(f"[watcher] ⏱ MD 兜底完成 (无 DOCX): {topic_name}, MD 已存在 {md_age:.0f}s, 耗时 {elapsed}s",
                                  file=sys.stderr, flush=True)
                            sig_name = f"done_{topic_name}_{int(_t.time())}.json"
                            sig_path = os.path.join(SIGNAL_DIR, sig_name)
                            with open(sig_path, "w", encoding="utf-8") as sf:
                                json.dump({
                                    "output_name": topic_name,
                                    "save_path": out_dir,
                                }, sf, ensure_ascii=False)
                            return
                except Exception:
                    pass

        threading.Thread(target=_watcher, daemon=True, name=f"report-watch-{topic_name}").start()

        # --- 立即返回，告诉 LLM 任务已声明 ---
        return {
            "success": True,
            "save_path": out_dir,
            "output_name": topic_name,
            "message": f"已经帮您安排好了，请稍等。",
        }

    @mcp.tool()
    def check_report_status(folder_name: str) -> dict:
        """查询报告/文档的生成进度。当用户问"好了吗""写完了吗"时调用。

        参数:
            folder_name: 话题名称，与 write_to_file 的 topic 一致（如"开发项目激励方案"）

        返回:
            包含 status (completed/generating/not_found), files, message 的字典。
            status=completed 时说明报告已生成完毕，请告诉用户可以去查看了。
        """
        # --- 确定工作目录 ---
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

        # 搜索文件夹：日期_话题名（如 2026-07-27_开发项目激励方案）
        topic_name = _topic_to_name(folder_name)
        date_str = datetime.now().strftime("%Y-%m-%d")
        prefix = f"{date_str}_{topic_name}"
        candidates = []
        # 精确匹配当前日期前缀
        candidates.append(os.path.join(work_dir, prefix))
        # 也搜纯话题名（兼容旧格式）
        candidates.append(os.path.join(work_dir, topic_name))
        # 也搜原始输入（万一没被 sanitize）
        if topic_name != folder_name:
            candidates.append(os.path.join(work_dir, folder_name))
        out_dir = None
        for d in candidates:
            if os.path.isdir(d):
                out_dir = d
                break
        if out_dir is None:
            # 都没有，默认用新格式
            out_dir = os.path.join(work_dir, prefix)

        if not os.path.isdir(out_dir):
            return {
                "status": "not_found",
                "message": f"未找到文件夹【{folder_name}】，可能还没开始生成。",
            }

        # 查找 .docx 和 .md 文件
        all_files = os.listdir(out_dir)
        doc_files = [f for f in all_files if f.endswith('.docx')]
        md_files = [f for f in all_files if f.endswith('.md')]
        
        result_files = []
        for f in doc_files + md_files:
            fpath = os.path.join(out_dir, f)
            size = os.path.getsize(fpath)
            ext = os.path.splitext(f)[1]
            mtime = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(fpath)))
            result_files.append({
                "name": f, "ext": ext, "size_kb": round(size / 1024, 1),
                "file_time": mtime
            })
        
        # 判断是否真的完成了：有 ≥0.5KB 的 docx 文件才算完成
        substantial = [f for f in result_files if f["ext"] == ".docx" and f["size_kb"] >= 0.5]
        if substantial:
            timing_info = " | ".join([f"{r['ext']}:{r['file_time']}({r['size_kb']}KB)" for r in result_files])
            return {
                "status": "completed",
                "files": result_files,
                "message": (
                    f"报告已经写好了！【{folder_name}】文件夹里有 {len(result_files)} 个文件。"
                    f" 时间戳：{timing_info}"
                    f" 请对用户说：'报告写好了，去【{folder_name}】文件夹看看时间对比，不合适的我再修改。'"
                ),
            }
        elif result_files:
            return {
                "status": "generating",
                "files": result_files,
                "message": "文件已经开始生成但内容还比较少，正在写入中，请稍后再问。请告诉用户再等一会儿。",
            }

        # 没有 .docx 文件，检查 log.txt 看是否在运行
        log_path = os.path.join(out_dir, "log.txt")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                log_content = f.read()
            if log_content.strip():
                return {
                    "status": "generating",
                    "message": "报告正在后台生成中，还没写完，请稍后再问。请告诉用户再等等。",
                }

        return {
            "status": "generating",
            "message": "报告正在生成中，请稍后再问。",
        }

    # ---- 启动时预热 codebuddy CLI（后台线程，不阻塞 MCP）----
    def _prewarm_codebuddy():
        """预热 codebuddy CLI：首次调用会让 Node.js 加载技能/依赖/扫描系统。
        预热后后续真实任务可省去这 1~1.5 分钟冷启动。"""
        t0 = time.time()
        print("[prewarm] 正在预热 codebuddy CLI …", file=sys.stderr, flush=True)
        try:
            found = find_codebuddy()
            if not found:
                print("[prewarm] codebuddy 未找到，跳过预热", file=sys.stderr, flush=True)
                return
            exe, prefix = found
            cmd = [exe] + prefix + [
                "-p", "--permission-mode", "bypassPermissions",
                "echo warming_up_complete",
            ]
            kwargs = dict(
                capture_output=True, text=True, encoding="utf-8",
                timeout=300, env=_get_fixed_env(),
            )
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = si
            subprocess.run(cmd, **kwargs)
            elapsed = time.time() - t0
            print(f"[prewarm] ✅ 预热完成，耗时 {elapsed:.1f}s", file=sys.stderr, flush=True)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            print(f"[prewarm] ⚠ 预热超时 ({elapsed:.1f}s)，可能模型响应慢", file=sys.stderr, flush=True)
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[prewarm] ⚠ 预热失败 ({elapsed:.1f}s): {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_prewarm_codebuddy, daemon=True).start()

    mcp.run(transport="stdio")


# ============================================================
#  入口
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp-server":
        # MCP Server 模式（桥接器子进程）
        run_mcp_server()
    else:
        # GUI 模式
        app = McpGUI()
        app.run()
