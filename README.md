# 小智AI · 智能办公助手

> 基于 ESP32-S3 + 小智 AI 聊天机器人的语音办公助手。喊一声"栗娜"，用语音写报告、做调研、出方案，报告直接生成 Word 文档保存到桌面。

## 简介

本项目在开源[小智 AI 聊天机器人](https://github.com/78/xiaozhi-esp32)基础上，增加了**桌面桥接器（WorkBuddy MCP）**，实现：

> **ESP32 语音 → 云端理解意图 → 本地 codebuddy 写文档 → 桌面通知提醒**

你只需要对着 ESP32 设备说：

> *"栗娜，帮我写一份开发项目激励方案"*

几分钟后，桌面右下角弹出通知，Word 文档已生成在桌面上。

---

## 架构

```
┌──────────┐    WebSocket    ┌──────────────┐    反向WS    ┌────────────────────┐
│  ESP32   │ ←────────────→ │ xiaozhi.me   │ ←─────────→ │  WorkBuddy MCP      │
│ (语音输入)│                │  云端 MCP 接入 │               │  桌面桥接器(GUI)     │
│          │   语音合成 ←    │              │    MCP调用 → │                     │
│ 唤醒词:  │                │  LLM 意图识别  │               │  ┌───────────────┐  │
│  栗娜    │                │  + 函数调用    │               │  │ FastMCP Server│  │
└──────────┘                └──────────────┘               │  └──────┬────────┘  │
                                                           │         │ stdio      │
                                                           │  ┌──────▼────────┐  │
                                                           │  │ codebuddy CLI │  │
                                                           │  │ → 写MD文档     │  │
                                                           │  │ → pypandoc    │  │
                                                           │  │   转DOCX      │  │
                                                           │  └───────────────┘  │
                                                           └────────────────────┘
                                                                   │
                                                         桌面通知 ←┘
```

**核心链路**：

| 环节 | 做什么 | 耗时 |
|------|--------|------|
| ESP32 语音采集 + ASR | 语音→文字 | 实时流式 |
| 云端 LLM 意图识别 | 理解"要写报告"→调用 `write_report` | ~1-2s |
| MCP 工具调度 | 启 codebuddy 后台写文档 | 立即返回 |
| codebuddy LLM 生成 MD | 撰写完整文档内容 | 3-8 min |
| pypandoc 本地转换 | MD → DOCX | <1s |
| 桌面通知 | 右下角弹窗+提示音 | 即时 |

---

## 功能

- 🎤 **语音唤醒**：说"栗娜"唤醒设备，无需按键
- 📝 **语音写文档**：一句话生成报告/方案/调研/总结
- 📄 **自动转 Word**：Markdown → DOCX 本地秒转，无需联网
- 🔔 **桌面通知**：报告完成后右下角弹窗+提示音
- 🖥️ **GUI 管理**：系统托盘运行，绿色小图标，一键启停
- 📦 **单文件 EXE**：PyInstaller 打包，无需安装 Python
- 🔌 **远端 LLM 解耦**：文档内容由 codebuddy (AI 编程助手) 生成，xiaozhi 云端只负责意图识别

---

## 硬件

| 项目 | 说明 |
|------|------|
| 主控 | ESP32-S3（微雪 WAVESHARE ESP32-S3 Audio Board） |
| 麦克风 | 板载 I2S MEMS 麦克风 |
| 扬声器 | I2S 功放输出 |
| 唤醒词 | **栗娜**（Custom Wake Word, Multinet7） |
| 音频超时 | 120 秒（`AUDIO_POWER_TIMEOUT_MS`） |

支持 70+ 种开源硬件（详见[原始项目 README_zh.md](README_zh.md)），本项目默认适配微雪 ESP32-S3 Audio Board。

---

## 快速开始

### 1. 配置 xiaozhi.me MCP 接入点

在 [xiaozhi.me](https://xiaozhi.me) 控制台创建 MCP Agent，获取接入点 URL，填入 `scripts/workbuddy_mcp/mcp_config.json`：

```json
{
  "endpoint": "wss://api.xiaozhi.me/mcp/?token=你的Token",
  "work_dir": "E:/0000"
}
```

- `endpoint`：xiaozhi.me 提供的 MCP WebSocket 接入点
- `work_dir`：报告输出目录（不填默认桌面）

### 2. 启动桌面桥接器

**方式一：一键启动（推荐开发调试）**

双击 `scripts/workbuddy_mcp/run.bat`

**方式二：单文件 EXE（推荐日常使用）**

在 `scripts/workbuddy_mcp/` 目录下双击 `build_exe.bat` 打包，然后在 `dist/WorkBuddy_MCP.exe` 双击运行。

启动后系统托盘出现绿色图标，右键可查看日志、启停服务。

### 3. 烧录固件

```bash
# 使用编译脚本（推荐）
.\build_flash.bat

# 或手动
idf.py build
idf.py -p COM3 flash
```

固件基于小智 2.2.4，唤醒词已改为"栗娜"。编译环境：ESP-IDF v5.5.4。

### 4. 开始使用

1. 确保 ESP32 已连网 + 桌面桥接器已启动
2. 对设备说：**"栗娜，帮我写一份XXX方案"**
3. 等待几分钟，桌面右下角弹出"报告完成"通知
4. 打开桌面（或配置的工作目录）查看生成的 Word 文档

---

## MCP 工具

桥接器提供以下 MCP 工具供云端 LLM 调用：

### `write_report(topic, version)`

写文档工具。LLM 理解用户意图后调用此工具，工具立即返回（不阻塞），后台生成文档。

| 参数 | 类型 | 说明 |
|------|------|------|
| `topic` | string | 必填，报告主题，如"开发项目激励方案" |
| `version` | string | 可选，版本号，默认"1"，自动递增 |

**输出目录结构**：

```
工作目录/
├── 2026-07-28_开发项目激励方案/
│   ├── 2026-07-28_开发项目激励方案_V1.md     ← codebuddy 生成
│   └── 2026-07-28_开发项目激励方案_V1.docx   ← pypandoc 自动转换
```

---

## 目录结构

```
├── main/                          # ESP32 固件 C 源码
│   ├── audio/                     # 音频服务（I2S输入输出）
│   ├── boards/                    # 板级配置
│   └── application.cc             # 主应用逻辑
├── partitions/                    # 分区表
├── scripts/
│   └── workbuddy_mcp/             # ★ MCP 桌面桥接器
│       ├── mcp_gui.py             # GUI 主程序（Tkinter，内置桥接器）
│       ├── workbuddy_mcp.py       # FastMCP Server（stdio 传输）
│       ├── mcp_config.json        # xiaozhi.me 接入点配置
│       ├── build_exe.bat          # PyInstaller 打包脚本
│       ├── run.bat                # 一键启动脚本
│       └── requirements.txt       # Python 依赖
├── sdkconfig                      # ESP-IDF 编译配置
├── CMakeLists.txt                 # 构建入口
├── build_flash.bat                # 编译+烧录脚本
├── build_flash.ps1                # PowerShell 编译脚本
└── README.md                      # 本文件
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 固件 | C, ESP-IDF v5.5.4, FreeRTOS |
| 语音识别 | ESP-SR Multinet7 (离线) |
| 音频编解码 | OPUS |
| 通信 | WebSocket (wss://api.xiaozhi.me) |
| 云端 | xiaozhi.me MCP 平台 |
| MCP 桥接 | Python 3.10+, FastMCP, websockets |
| GUI | Tkinter |
| 文档生成 | codebuddy CLI (Markdown) → pypandoc (DOCX) |
| 打包 | PyInstaller (单文件 EXE) |

---

## 致谢

本项目基于以下开源项目：

- [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — 小智 AI 聊天机器人固件
- [xiaozhi.me](https://xiaozhi.me) — 云端 MCP 平台
- [FastMCP](https://github.com/jlowin/fastmcp) — Python MCP 框架
- [pypandoc](https://github.com/JessicaTegner/pypandoc) — Python pandoc 绑定

---

## License

MIT
