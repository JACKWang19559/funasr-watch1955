# funasr-watch

为 Codex、Trae 和其他 AI Agent 提供视频理解能力：使用 yt-dlp 获取视频和字幕，FFmpeg 抽取画面，FunASR + SenseVoiceSmall 在本地完成语音转写。模型下载完成后，本地视频的转写无需联网或 API Key。

基于 [bradautomates/claude-video](https://github.com/bradautomates/claude-video) 改造，保留原生字幕优先、场景抽帧和工作目录自动检测。

## 功能

- 支持本地视频及 yt-dlp 支持的视频平台。
- 优先获取原生字幕，缺失时使用 SenseVoiceSmall + FSMN-VAD 转写。
- 默认采用 **CPU 语音分段 + GPU 语音识别**；CUDA 不可用时使用 CPU。
- 抖音自动获取 Cookie、缓存和失效刷新，支持精选页 `modal_id` 链接。
- 独立浏览器会话解析公开视频媒体地址，处理 yt-dlp 详情接口仍返回 403 的情况。
- 提供 Codex 安装脚本；保留 Trae 插件入口和直接命令行使用。
- 生成画面、时间点及 `transcript.txt`，后续分析可复用已有文件。

## 安装依赖

需要 Python 3.10+、完整版本的 FFmpeg/ffprobe 和 Microsoft Edge（抖音自动 Cookie 的默认浏览器）。

```bash
# 使用当前仓库地址，本地目录命名为 funasr-watch。
git clone https://github.com/JACKWang19559/funasr-watch1955.git funasr-watch
cd funasr-watch
python -m venv .venv
```

Windows PowerShell：

```powershell
winget install --id Gyan.FFmpeg -e
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

macOS/Linux 激活虚拟环境：

```bash
source .venv/bin/activate
# macOS: brew install ffmpeg
# Linux: 使用系统包管理器安装 ffmpeg
```

先按 [PyTorch 官方说明](https://pytorch.org/get-started/locally/) 安装与驱动匹配的 `torch` 和 `torchaudio`，再安装项目依赖。当前 Windows 环境验证过以下 CUDA 12.1 组合：

```bash
python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
python -m pip install -r requirements.txt
python -c "import torch; print(torch.cuda.is_available())"
```

没有 NVIDIA CUDA GPU 时，安装对应的 CPU 版本即可。`nvidia-smi` 中的 CUDA 版本表示驱动支持能力；实际使用的 CUDA 运行时取决于 PyTorch 安装包。

默认使用系统 Edge，无需额外下载浏览器。使用 Playwright Chromium 时：

```bash
python -m playwright install chromium
# 同时将 WATCH_COOKIE_BROWSER 设为空字符串，使用 Playwright 自带 Chromium。
```

## 在 Codex 中安装

在安装好依赖的虚拟环境中运行：

```bash
python codex-adapter/install_codex.py
```

安装到 `$CODEX_HOME/skills/watch`，未设置 `CODEX_HOME` 时为 `~/.codex/skills/watch`。重新加载技能后，可以使用 `$watch <视频URL或本地路径>`。

安装脚本自动记录所用 Python、FFmpeg 和模型缓存的绝对路径，不依赖开发者机器。FFmpeg 不在 PATH 时可以指定：

```powershell
python codex-adapter/install_codex.py --ffmpeg 'C:\tools\ffmpeg\bin\ffmpeg.exe'
```

其他选项：

- `--python <路径>`：指定已安装依赖的 Python；默认当前解释器。
- `--model-cache <目录>`：指定模型缓存位置。
- `--model-dir <目录>` / `--vad-model-dir <目录>`：复用含 `model.pt` 的本地模型目录。
- `--dest <目录>`：自定义技能安装目录。
- `--force`：更新已有安装，保留全局用户偏好和模型缓存。

SenseVoice 的分词器在部分 Windows 环境中不能读取含中文的模型路径；可将模型放到 ASCII 路径，或创建 ASCII 路径的目录联接后传给 `--model-dir`。

## 在 Trae 中安装

将仓库复制到 Trae 的插件目录（如 Windows 的 `~/.trae-cn/plugins/funasr-watch/`），确保 `.trae-plugin/plugin.json` 和 `skills/` 保持相对结构，重启后使用 `watch` 技能。Python 依赖安装在系统 Python 或独立虚拟环境中；`setup.py` 会检测并记录 `WATCH_PYTHON`，绕过缺少依赖的 Trae 内置 Python。

## 命令行使用

```bash
python skills/watch/scripts/setup.py --json
python skills/watch/scripts/watch.py "<视频URL或本地路径>"
python skills/watch/scripts/watch.py video.mp4 --start 0:45 --end 1:00
python skills/watch/scripts/watch.py "<视频URL>" --detail transcript
```

已安装的 Codex 技能也可通过其 `scripts/codex_watch.py` 入口运行，它会设置所需解释器、FFmpeg 和模型路径。

| 参数 | 作用 |
|---|---|
| `--detail transcript` | 仅字幕/转写；有原生字幕时可跳过视频下载 |
| `--detail efficient` | 快速关键帧抽取，最多 50 帧 |
| `--detail balanced` | 默认场景感知抽帧，最多 100 帧 |
| `--detail token-burner` | 不设帧数上限 |
| `--start T --end T` | 分析指定时间段，支持秒、MM:SS、HH:MM:SS |
| `--timestamps 0:45,2:10` | 在指定时间点抽帧 |
| `--max-frames N --resolution W` | 调整帧数上限和图像宽度 |
| `--fps F` | 指定采样帧率，最高 2 fps |
| `--out-dir DIR` | 指定产物目录 |
| `--no-whisper` | 禁用本地 FunASR 转写回退，保留兼容参数名 |
| `--no-dedup` | 保留相似帧 |

工作目录优先级：`--out-dir` > `WATCH_WORK_DIR` > Trae 的 `SAFE_RM_ALLOWED_PATH` > 当前目录。默认在工作区的 `.watch-work/<时间戳>/` 保存视频、音频、帧和转写。

## 默认 CPU/GPU 混合分工

配置文件为 `~/.config/watch/.env`：

```dotenv
# ASR: auto 优先 CUDA；cuda/cuda:0 指定 GPU；cpu 指定 CPU
WATCH_TRANSCRIBE_DEVICE=auto
# VAD: 默认 CPU；auto 跟随 ASR，也可显式指定 cuda/cpu
WATCH_VAD_DEVICE=cpu
WATCH_DETAIL=balanced
```

| 方案 | ASR 配置 | VAD 配置 | 本机推理实测 |
|---|---|---|---:|
| 纯 CPU | `cpu` | `cpu` | 38.55 秒 |
| 识别和分段都使用 GPU | `auto` | `auto` | 32.75 秒 |
| **默认混合分工** | **`auto`** | **`cpu`** | **30.86 秒** |

测试硬件为 Intel i7-11800H + RTX 3050 Laptop 4GB；使用 FunASR 1.4.16、PyTorch 2.5.1+cu121、相同的 621.32 秒音频，预热后每个方案测一次。耗时不含下载、解码、模型加载及抽帧；混合方案在该样本中约快 6%，其他硬件和音频可能不同。详见 [测试条件与结果](benchmarks/device-comparison.json)。

GPU 模型加载失败时会尝试 CPU 回退。原生字幕可用时仍优先使用字幕，无需运行识别模型。SenseVoiceSmall 当前权重约 0.9GB，首次运行需要下载模型。

## 抖音自动 Cookie

默认使用独立的本地 Edge 配置访问目标页面，获取 Cookie 并保存在 `~/.config/watch/auto-cookies/`。缓存默认有效六小时，会话被拒绝后刷新一次。如果 yt-dlp 的详情接口仍失败，使用浏览器正常加载页面得到的媒体地址下载。

```dotenv
WATCH_AUTO_COOKIES=true
WATCH_COOKIE_BROWSER=msedge
WATCH_COOKIE_MAX_AGE=21600
```

设置 `WATCH_AUTO_COOKIES=false` 可关闭该功能。设置 `WATCH_COOKIE_BROWSER=chrome` 使用系统 Chrome；空字符串选择 Playwright Chromium。

Cookie 缓存限制为当前用户访问（Windows 还允许 SYSTEM），日志不输出 Cookie 值。日常浏览器配置只会在显式设置 `WATCH_BROWSER` 后读取。显式 `WATCH_COOKIE_FILE`（Netscape cookies.txt）优先，其次为 `WATCH_BROWSER`，最后为抖音自动 Cookie。

手动刷新或让用户完成网站要求的登录/验证：

```bash
python skills/watch/scripts/auto_cookies.py "<抖音视频URL>" --refresh
python skills/watch/scripts/auto_cookies.py "<抖音视频URL>" --interactive --timeout 180
```

Codex 入口的对应命令为 `codex_watch.py --cookies --refresh <URL>` 或 `codex_watch.py --cookies --interactive --timeout 180 <URL>`。验证码由用户完成；浏览器会话不能保证获取登录受限、地域受限或无权访问的视频。

Bilibili 等其他平台可配置 `WATCH_COOKIE_FILE` 或 `WATCH_BROWSER`，使用具有相应访问权限的会话。

## 开发与验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q skills/watch/scripts codex-adapter
python -m pip check
```

单元测试使用模拟浏览器会话和虚构 Cookie，不会访问个人浏览器或联网下载视频。已另行验证抖音下载、CPU VAD + CUDA ASR 的完整本地视频转写，以及 Codex 安装入口。

目录结构：

```text
.trae-plugin/               Trae 插件清单
skills/watch/              通用技能及视频处理脚本
codex-adapter/              Codex 技能模板、安装脚本和启动器
benchmarks/                性能测试条件与结果
tests/                     Cookie 和设备选择回归测试
requirements.txt           Python 依赖
```

## 故障排查

| 问题 | 处理方法 |
|---|---|
| `ffmpeg not found` | 安装完整 FFmpeg，并加入 PATH 或通过安装参数指定 |
| GPU 未启用 | 检查 `torch.cuda.is_available()`，安装与驱动匹配的 torch/torchaudio |
| 无法启动 Cookie 浏览器 | 安装 Edge，或配置其他浏览器及 Playwright Chromium |
| 网站要求登录/验证码 | 使用 `--interactive`，由用户完成后重试 |
| SenseVoice 分词器加载失败 | 检查 Windows 模型路径，优先使用 ASCII 路径 |
| 转写没有逐句时间戳 | 当前模型可能返回整段文本，转写文件中的时间精度取决于模型输出 |

## 许可证与致谢

MIT License，沿用 [上游许可证](https://github.com/bradautomates/claude-video/blob/main/LICENSE)。

- 原项目：[bradautomates/claude-video](https://github.com/bradautomates/claude-video)
- 语音识别：[modelscope/FunASR](https://github.com/modelscope/FunASR)
- 模型：[SenseVoiceSmall](https://www.modelscope.cn/models/iic/SenseVoiceSmall)
