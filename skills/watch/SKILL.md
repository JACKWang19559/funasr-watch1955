---
name: watch
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, pulls the transcript from captions (or FunASR local transcription fallback), and hands the result to the agent so it can answer questions about what's in the video. Trigger when the user pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, etc.) or points at a local video file and asks about it.
---

# watch

You don't have a video input; this skill gives you one. A Python script gets captions first, optionally downloads the video, extracts frames as JPEGs (scene-aware, or fast keyframes at `efficient` detail), gets a timestamped transcript (native captions first, then FunASR local transcription as fallback), and prints frame paths. You then `Read` each frame path to see the images and combine them with the transcript to answer the user.

## Runtime defaults

Use CPU FSMN-VAD segmentation with CUDA SenseVoiceSmall ASR when available.
`WATCH_TRANSCRIBE_DEVICE=auto` and `WATCH_VAD_DEVICE=cpu` are the defaults.
`WATCH_VAD_DEVICE=auto` explicitly follows ASR placement. Without CUDA, use CPU.
Douyin URLs automatically use a separate browser cookie cache; selected links
with `modal_id` are normalized. Manual login or verification, when required,
uses `scripts/auto_cookies.py <URL> --interactive --timeout 180`. Do not print
cookie values or automate verification challenges.

For YouTube bot/login errors, locked browser cookies, missing challenge solvers,
or empty caption responses, read [references/youtube-browser.md](references/youtube-browser.md).
It provides dedicated manual login and continuous browser capture via
`--via-browser`. After a failed authenticated downloader attempt, switch to this
workflow instead of repeating client/API variations. Browser capture defaults
to 16 actual playback frames and preserves original audio speed at up to 4×
playback; exact `--timestamps` cues require a downloaded video.

## Scripts location

All scripts live under `./scripts/` (sibling of this SKILL.md). On **Windows**, use `py` to run Python scripts; on macOS/Linux use `python3`. Examples below use `py` (Windows default).

### Windows + Trae 环境：使用 WATCH_PYTHON

Trae 自带的 Python 3.10 缺少 FunASR（本地转写引擎）。`setup.py --json` 会自动检测含 FunASR 的系统 Python 路径，返回在 `watch_python` 字段中。

**执行流程**：
1. Step 0 运行 `py ./scripts/setup.py --json`（setup.py 本身不需要 FunASR，可正常运行）
2. 从返回的 JSON 中读取 `watch_python` 字段
3. 如果 `watch_python` 非空，**后续所有脚本调用使用该路径代替 `py`**，例如：
   ```
   "C:\Users\...\python.exe" ./scripts/watch.py "<source>"
   ```
4. 如果 `watch_python` 为 null，fallback 到 `py`

这个路径也存储在 `~/.config/watch/.env` 的 `WATCH_PYTHON` 变量中，由 `setup.py` 自动管理。

## Step 0 — Setup preflight (runs every invocation, silent on success)

On the first invocation in a session, run structured preflight:

```
py ./scripts/setup.py --json
```

Branch on two fields:

- **`can_proceed: true` and `first_run: false`** → setup is already done (the user may have deliberately skipped FunASR — that's allowed). Proceed to Step 1 without comment.
- **`first_run: true`** → genuine first-time setup. Do these in order:
  1. If `missing_binaries` is non-empty, run the installer first and confirm the binaries land.
  2. Run the installer once more if needed so it scaffolds `~/.config/watch/.env`.
  3. Encourage FunASR installation (`pip install funasr torch`) and ask the watch-preference questions below, then write the selected values into `~/.config/watch/.env` and set `SETUP_COMPLETE=true`.
- **`can_proceed: false` and `first_run: false`** → setup was finished before but the environment regressed. Run the installer to remediate, then proceed. Don't re-ask preferences.

**重要：读取 `watch_python` 字段。** 从 JSON 返回中获取 `watch_python` 路径（含 FunASR 的系统 Python）。如果非空，**后续所有 `py ./scripts/...` 命令都改用该路径**，例如 `"<watch_python>" ./scripts/watch.py ...`。这在 Windows + Trae 环境中必需，因为 Trae 自带的 Python 3.10 缺少 FunASR。

On follow-up calls in the same session, use the silent check:

```
py ./scripts/setup.py --check
```

This is a <100ms lookup. Exit 0 means /watch can run. On non-zero exit, follow the table:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing binaries (`ffmpeg` / `ffprobe` / `yt-dlp`) | Run installer |
| `3` | Genuine first run with FunASR not installed | Run installer to scaffold `.env`, then encourage `pip install funasr torch` (the user may decline — proceed with `--no-whisper`) |
| `4` | Both missing | Run installer, then encourage FunASR installation |

The installer is idempotent — safe to re-run:

```
py ./scripts/setup.py
```

On macOS with Homebrew, it auto-installs `ffmpeg` and `yt-dlp`. On Linux/Windows, it prints the exact install commands for the user to run. It scaffolds `~/.config/watch/.env` with commented placeholders and default watch settings at `0600` perms. On Windows, it also detects the system Python path (含 FunASR) and writes it to `WATCH_PYTHON` in `.env`.

**If FunASR is still not installed after install:** tell the user to run `pip install funasr torch` in their system Python. If they don't want to set up FunASR, proceed with `--no-whisper` and tell them videos without native captions will come back frames-only.

**First-run watch preference:** after the installer has scaffolded `~/.config/watch/.env`, use `AskUserQuestion` to ask one question:

- Default detail (one dial). Present these as `AskUserQuestion` options in this exact order — lightest to heaviest — and keep `(recommended)` on `balanced` even though it is not first:
  - `transcript` — no frames at all, transcript only (skips video download when captions exist).
  - `efficient` — fast keyframe pass (cap 50).
  - `balanced` (recommended) — scene-aware frames (cap 100, default).
  - `token-burner` — scene-aware, uncapped (maximum fidelity; high token cost).

Write the answer directly into `~/.config/watch/.env`:

```
WATCH_DETAIL=balanced
```

Once dependencies, the API-key choice, and this preference are handled, write or update `SETUP_COMPLETE=true` in the same file. Do not ask this preference question again when `SETUP_COMPLETE=true`.

Within a single session, you can skip Step 0 on follow-up calls — once `--check` returned 0, nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User asks "watch this video" / "看看这个视频" / "what's in this video".

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Universal rate cap: 2 fps.** The script never samples faster than 2 fps, even when a budget or `--fps` would imply more.
- **The frame ceiling is set by the detail mode** (`WATCH_DETAIL` in `~/.config/watch/.env`, or `--detail`), not a single global cap:
  - `transcript` → no frames
  - `efficient` → up to **50** (keyframes)
  - `balanced` (default) → up to **100** (scene-aware)
  - `token-burner` → **uncapped** (scene-aware; a soft warning prints past 250 frames)
  - `--max-frames N` overrides whichever cap the mode would otherwise use.
- **Full-video frame budget by duration.** Token cost grows with frame count, so the script targets a budget by duration:
  - ≤30s → ~12-30 frames
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → up to the detail cap, sparsely spaced (warning printed)
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a sparse scan.

## How to invoke

**Step 1 — parse the user input.** Separate the video source (URL or path) from any question the user asked. Example: `watch https://youtu.be/abc what language is this in?` → source = `https://youtu.be/abc`, question = `what language is this in?`.

**Step 2 — run the watch script.** Pass the source verbatim. Use the `RunCommand` tool:

```
py ./scripts/watch.py "<source>"
```

**Windows + Trae 环境**：如果 Step 0 返回了 `watch_python`，用该路径代替 `py`：
```
"<watch_python>" ./scripts/watch.py "<source>"
```

**工作目录自动检测**：watch.py 会自动检测用户项目根目录作为工作目录根，无需手动传 `--out-dir`。检测优先级：
1. `--out-dir` 参数（最高优先级，显式指定）
2. `WATCH_WORK_DIR` 环境变量/`.env` 配置（用户自定义）
3. `SAFE_RM_ALLOWED_PATH` 自动检测 Trae 用户项目根目录（自动）
4. `Path.cwd()`（脚本所在目录，最后回退）

工作文件会自动生成在 `<工作目录根>/.watch-work/<时间戳>/` 下，包含视频、帧、音频和字幕文件。

**自定义工作目录（可选）**：如果用户希望工作文件生成在特定目录，可在 `~/.config/watch/.env` 中设置：
```
WATCH_WORK_DIR=D:\my-project
```

Optional flags:
- `--detail transcript|efficient|balanced|token-burner` — fidelity/speed dial.
- `--start T` / `--end T` — focus on a section. Accepts `SS`, `MM:SS`, or `HH:MM:SS`. When either is set, fps auto-scales denser.
- `--timestamps T1,T2,…` — grab a frame at each of these absolute timestamps. Use this after reading the transcript to capture deictic moments the presenter flags ("look here", "as you can see", "notice this").
- `--max-frames N` — override the preset cap for tighter token budget (e.g. `--max-frames 40`)
- `--resolution W` — change frame width in px (default 512; bump to 1024 only if the user needs to read on-screen text)
- `--fps F` — override auto-fps (clamped to 2 fps max)
- `--out-dir DIR` — explicitly set working directory for all intermediate files (overrides auto-detection). 默认自动检测：`<WATCH_WORK_DIR 或 Trae 工作区>/.watch-work/<timestamp>`。
- `--no-whisper` — disable the FunASR transcription fallback entirely (frames-only if no captions)
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the previous kept one.
- `--via-browser` — use YouTube continuous browser capture when the normal downloader fails.
- `--browser-profile DIR` / `--browser-rate 1..4` — select the dedicated profile and browser playback rate.

### Focusing on a section (higher frame rate)

When the user asks about a specific moment — "what happens at the 2 minute mark?", "zoom into 0:45 to 1:00", "the first 10 seconds" — pass `--start` and/or `--end`. The script switches to focused-mode budgets, which are denser than full-video budgets (still capped at 2 fps, and still bounded by the detail-mode cap):

- ≤5s → 2 fps (up to 10 frames)
- 5-15s → 2 fps (up to 30 frames)
- 15-30s → ~2 fps (up to 60 frames)
- 30-60s → ~1.3 fps (up to 80 frames)
- 60-180s → ~0.6 fps (100 frames, capped)

Focused mode is the right call for:
- Any moment/range the user names explicitly ("around 2:30", "the intro", "the last 30 seconds").
- Any video longer than ~10 minutes where the user's question is about a specific part.
- Re-runs after a full scan didn't have enough detail in some region.

Transcript is auto-filtered to the same range. Frame timestamps are absolute (real video timeline, not offset-from-start).

Examples:
```
py ./scripts/watch.py video.mp4 --start 50 --end 60
py ./scripts/watch.py "$URL" --start 2:15 --end 2:45 --fps 2
py ./scripts/watch.py "$URL" --start 1:12:00
```

**Step 3 — Read every frame path the script lists.** The `Read` tool renders JPEGs directly as images. Read all frames in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS` timestamp so you can align them to the transcript.

**Step 4 — answer the user.** You now have two streams of evidence:
- **Frames** — what's on screen at each timestamp
- **Transcript** — what's said at each timestamp. The report's header shows the source (`captions` = native subs; `funasr` = local ASR). When native sentence timing is unavailable, 60-second source windows are labeled `[MM:SS–MM:SS]`; these are chunk ranges, not exact sentence or word timestamps.

If the user asked a specific question, answer it directly citing timestamps. If they didn't ask anything, summarize what happens in the video — structure, key moments, notable visuals, spoken content.

This holds for `transcript` detail too: even with no frames, produce a **summary** like the other modes — do not paste the full transcript into chat. Synthesize structure, key moments, and spoken content with timestamps; quote only the lines that matter. Offer the raw transcript only if the user explicitly asks for it.

Ground names and counts in speech and readable frames. A thumbnail logo alone
does not establish an app's identity. Separate the creator's historical opinions
from current product facts. Use actual player/frame times; description chapters
may be stale after edits. Do not present chunk timestamps as precise sentence times.

**Step 5 — retain reusable files.** Keep the working directory for follow-ups.
Reuse verified media, frames and transcripts rather than downloading again.
Remove retained files only when the user requests cleanup or they are known to
be unnecessary.

## Detail and frames

Default behavior comes from `~/.config/watch/.env`:

- `WATCH_DETAIL=transcript|efficient|balanced|token-burner` (default: `balanced`)

At `transcript` detail, captions are enough to return a report without downloading video. If captions are missing, the script downloads audio only and tries FunASR local transcription. If no transcript can be produced, it reports the limitation clearly; re-run with `--detail balanced` for frames.

At `efficient` detail, the script downloads the video and extracts **keyframes only** (`ffmpeg -skip_frame nokey`) — a near-instant pass that lands frames on scene cuts. If a clip has fewer than 4 keyframes it falls back to uniform sampling.

At `balanced` / `token-burner` detail, the script extracts **scene-aware** frames: ffmpeg scene-change selection first, falling back to uniform sampling only when the video is effectively static. `balanced` caps at 100 frames; `token-burner` is uncapped. Frame report lines include both timestamp and selection reason. Extracted images are clamped to a maximum 1998px height for Read compatibility.

## Transcript-cue frames

Visual frame selection (scene/keyframe) can miss the moments a presenter explicitly flags — "look here", "as you can see", "notice this", "watch what happens" — because pointing at a slide is often a *low* visual change. `--timestamps` lets you force a frame at those exact moments. **You** decide which moments matter, by reading the transcript:

1. Run once at `--detail transcript` (or any detail) to get the timestamped transcript.
2. Scan it for deictic cues — phrases where the speaker directs attention to something on screen. This is a judgment call (ignore rhetorical "look, the point is…"); that's why it's done by you, not a regex.
3. Re-run with `--timestamps 4:32,7:10,9:55` (absolute source times). For a URL, point the second run at the **downloaded local file** in the work dir so it doesn't re-download.

Behavior:
- **Additive by default.** Cue frames (`reason=transcript-cue`) are merged into whatever `--detail` already selected, in chronological order.
- **Pinned and counted first.** Cue frames are reserved against the frame cap before the detail engine runs, so they're never evicted by even-sampling.
- **Honors focus mode.** With `--start/--end`, any cue timestamp outside the window is dropped (reported in the summary). Coordinates are always absolute source time.
- **Cue-only frames.** `--detail transcript --timestamps …` skips scene/keyframe sampling and returns *only* the cue frames (it will download the video to do so, since frames need pixels).

## Transcription

The script gets a timestamped transcript in one of two ways:

1. **Native captions (free, preferred).** yt-dlp pulls manual or auto-generated subtitles from the source platform if available.
2. **FunASR local transcription fallback.** If no captions came back (or the source is a local file), the script extracts audio (`ffmpeg -vn -ac 1 -ar 16000 -b:a 64k`, ~0.5 MB/min) and transcribes it locally using FunASR + SenseVoiceSmall:
   - **No API key required** — completely offline (first run downloads the model ~234MB).
   - **SenseVoiceSmall** — 阿里达摩院开源模型，中文识别效果最佳（CER 7.81%）。
   - **GPU 加速** — 自动检测 CUDA，有 GPU 用 GPU，否则用 CPU。

FunASR 安装：`pip install funasr torch`。GPU 加速（可选）：`pip install torch --index-url https://download.pytorch.org/whl/cu121`。设备配置在 `~/.config/watch/.env` 的 `WATCH_TRANSCRIBE_DEVICE`（auto/cuda/cpu）。使用 `--no-whisper` 跳过转写。

**标签自动过滤：** SenseVoiceSmall 模型输出会包含语言/情绪/事件标签（`<|zh|>`, `<|HAPPY|>`, `<|BGM|>`, `<|woitn|>` 等）。`funasr_transcribe.py` 的 `_clean_text()` 函数会自动用正则 `<\|[^|]+\|>` 过滤这些标签，输出干净的纯文本字幕。

**字幕保存：** 转写完成后，字幕会同时打印到 stdout 并保存到工作目录的 `transcript.txt` 文件中（格式：`[MM:SS] 文本内容`），方便后续查看和复用。

## Failure modes and handling

- **Setup preflight failed** → run `py ./scripts/setup.py` (auto-installs ffmpeg/yt-dlp via brew on macOS, scaffolds the `.env`, detects system Python with FunASR on Windows).
- **No transcript available** → captions missing AND (FunASR not installed OR transcription failed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → yt-dlp's error goes to stderr. If it's a login-required or region-locked video, tell the user plainly; do not keep retrying.
- **FunASR transcription fails** → the error is printed to stderr (likely: FunASR not installed, or model download failed). Tell the user to run `pip install funasr torch` in their system Python. The report will say "none available" only if transcription completely fails.

## Token efficiency

This skill burns tokens primarily on frames. Order of magnitude:
- 80 frames at 512px wide is roughly 50-80k image tokens depending on aspect ratio.
- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- Bumping `--resolution` to 1024 roughly quadruples the image tokens per frame. Only do it when necessary.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when transcription is needed, a mono 16 kHz audio clip
- Runs FunASR locally to transcribe the extracted audio — **no data leaves the machine** (completely offline after model download)
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory (`.watch-work/<timestamp>` under the auto-detected Trae workspace, `WATCH_WORK_DIR`, `--out-dir`, or the script directory) so the agent can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store configuration (`WATCH_TRANSCRIBE_DEVICE`, `WATCH_PYTHON`, `WATCH_DETAIL`, `WATCH_WORK_DIR`) and a `SETUP_COMPLETE` marker. As a fallback, also reads `.env` in the current working directory

**What this skill does NOT do:**
- Does not upload the video or audio to any external API — all transcription happens locally with FunASR
- Does not access any platform account (no login, no session cookies, no posting) — yt-dlp only ever requests public data
- Does not log, cache, or write API keys to stdout, stderr, or output files (no API keys needed — FunASR is local)
- Does not persist anything outside the working directory and `~/.config/watch/.env` — clean up the working directory when you're done (Step 5)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (caption selection + parsing), `scripts/whisper.py` (FunASR thin wrapper), `scripts/funasr_transcribe.py` (FunASR local transcription), `scripts/ffmpeg_utils.py` (ffmpeg path resolution), `scripts/setup.py` (preflight + installer)

Review scripts before first use to verify behavior.
