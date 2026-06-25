# WebvideoASR

基于 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) 模型的本地音频/视频转录工具，支持 macOS 和 Windows。

## 功能

- **local** — 批量转写本地音频/视频文件（wav, mp3, mp4, m4a, flac 等）
- **webvideo** — Web 界面，粘贴链接即可转写 Bilibili、YouTube、小红书、抖音、快手等平台视频

## 平台与设备

| 平台 | GPU 后端 | CPU 后端 |
|---|---|---|
| macOS（Apple Silicon M 系列） | MLX | PyTorch |
| macOS（Intel） | 不支持 | PyTorch |
| Windows（NVIDIA 显卡） | CUDA | PyTorch |
| Windows（无 NVIDIA 显卡） | 不支持 | PyTorch |

## 安装

### 1. 克隆项目

```bash
git clone https://github.com/Eyfen09/WebvideoAsr && cd WebvideoAsr
```

### 2. 安装依赖

```bash
# macOS Apple Silicon（GPU / MLX）
uv sync --extra mac-gpu

# Windows（GPU / CUDA）
uv sync --extra win-gpu

# CPU 模式（macOS / Windows 通用）
uv sync --extra cpu
```

### 3. 下载模型

**模型：Qwen3-ASR**

```bash
# 方式 A：HuggingFace
huggingface-cli download Qwen/Qwen3-ASR-1.7B --local-dir model/Qwen3-ASR-1.7B

# 方式 B：ModelScope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir model/Qwen3-ASR-1.7B
```

> 模型目录最终结构：
> ```
> model/Qwen3-ASR-1.7B/
> ```

## 使用

### 本地转写

```bash
# 转写单个文件
uv run main.py local path/to/audio.mp3

# 转写整个目录（递归）
uv run main.py local path/to/dir/

# 强制使用 CPU
uv run main.py local path/to/dir/ --cpu

# 显式指定 GPU
uv run main.py local path/to/dir/ --gpu
```

配置文件：[local/config.yaml](local/config.yaml)

### Web 界面

```bash
# 启动服务（默认 GPU）
uv run main.py webvideo

# 强制使用 CPU
uv run main.py webvideo --cpu
```

浏览器打开 `http://127.0.0.1:8765`，粘贴视频链接即可转写。

支持的平台：Bilibili、YouTube、小红书、抖音、快手等。

## 项目结构

```
webvideoASR/
├── main.py                  # 入口，子命令分发（local / webvideo）
├── pyproject.toml           # 项目配置与依赖
├── local/                   # 本地音频转写模块
│   ├── cli.py               # 命令行入口
│   ├── pipeline.py          # 批量转写编排
│   └── config.yaml          # 本地转写配置
├── webvideo/                # Web 视频转写模块
│   ├── cli.py               # 服务器启动
│   ├── app.py               # FastAPI 应用
│   ├── pipeline.py          # 转写流水线（下载→转写→输出）
│   ├── downloader.py        # 媒体下载
│   ├── resolver.py          # 链接解析
│   ├── workers.py           # 后台进程管理
│   ├── repository.py        # SQLite 数据层
│   ├── config.py            # 配置定义
│   ├── integrations/        # 平台适配
│   │   ├── bilibili.py
│   │   ├── youtube.py
│   │   ├── xiaohongshu.py
│   │   ├── douyin.py
│   │   └── kuaishou.py
│   ├── extractors/          # 链接提取
│   ├── similarity/          # 去重匹配
│   ├── browser_backends/    # 浏览器自动化后端
│   ├── static/              # 前端 JS/CSS
│   └── templates/           # HTML 模板
├── asr_core/                # 核心 ASR 引擎（共享模块）
│   ├── engine.py            # 会话创建与转写统一接口
│   ├── config.py            # 配置模型与 Device 枚举
│   ├── formatting.py        # 结果格式化
│   └── backends/
│       ├── mlx_backend.py   # macOS MLX 后端
│       └── torch_backend.py # PyTorch 后端（CUDA / CPU）
└── tests/                   # 测试
```
