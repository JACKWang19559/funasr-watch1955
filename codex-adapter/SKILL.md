---
name: watch
description: Analyze a video URL or local video using yt-dlp, FFmpeg frames and captions, with FunASR local transcription. Use when the user asks to watch, summarize, transcribe, or answer questions about a video, including 看看这个视频 or 总结视频内容.
---

# Watch videos with FunASR

The installed `scripts/runtime.json` records this machine's Python interpreter,
FFmpeg and model cache. Read it to obtain the interpreter path. Resolve
`scripts/codex_watch.py` relative to this SKILL.md and invoke both using absolute
paths. Run from the user's workspace so `.watch-work/` stays there.

On the first invocation in a session, run:

```text
<runtime.python> <absolute skill directory>/scripts/codex_watch.py --setup --json
```

If `can_proceed` is true, process the user's source verbatim:

```text
<runtime.python> <absolute skill directory>/scripts/codex_watch.py <URL or absolute local video path>
```

If dependencies are missing, fix the reported problem in that interpreter and
repeat the preflight. Preserve existing preferences when `first_run` is false.
The installed defaults are balanced frame sampling, CUDA ASR when available,
and CPU VAD. Settings live in `~/.config/watch/.env`; `WATCH_VAD_DEVICE=auto`
explicitly places VAD on the ASR device. Without CUDA, ASR uses CPU.

Useful controls:

- `--detail transcript|efficient|balanced|token-burner`: transcript only,
  fast keyframes, scene-aware sampling, or uncapped frames. Use token-burner
  only when requested.
- `--start T --end T`: focus on an interval; times accept seconds or MM:SS.
- `--timestamps 0:45,2:10`: force frames at relevant source timestamps.
- `--max-frames N --resolution W`: cap images and adjust text legibility.
- `--out-dir <absolute directory>`: choose the working directory.

Read the generated report, then view its JPEGs in chronological order with the
available image tool. Combine the transcript and visual evidence to answer the
actual question with relevant timestamps. State when sparse frames limit visual
coverage or transcription is unavailable. Keep working files for follow-ups and
reuse them instead of downloading again.

For YouTube bot/login errors, locked/encrypted cookies, unavailable challenge
solvers, or captions that return an empty body, read
[references/youtube-browser.md](references/youtube-browser.md). It provides
dedicated manual login and `--via-browser` capture without exporting cookies or
running downloaded JS components. Switch to this fallback after a failed
authenticated downloader attempt rather than repeating client/API variations.

Ground names and counts in the spoken content and readable frames. A thumbnail
logo is a clue, not proof of an app's identity. Separate the creator's historical
opinions from current product facts. Use actual player/frame timestamps;
description chapters can be outdated after edits. ASR chunk ranges bound the
speech and are not exact sentence timestamps. Do not cite an entire transcript
at `[00:00]` as if it had precise timing.

Douyin selected-page links with `modal_id` are normalized automatically. Cookies
are acquired in a separate local browser profile, cached for six hours, and
refreshed once if rejected. If the detail API still rejects the session, the
browser resolves the requested video's public media. Keep cookie values local;
do not print them or include them in chat.

Use the same launcher with `--cookies --refresh <URL>` to refresh manually.
When the site requires login or verification, use
`--cookies --interactive --timeout 180 <URL>` for the user to complete it in the
separate profile. Do not automate a verification challenge.

`WATCH_AUTO_COOKIES=false` disables automatic acquisition. Explicit
`WATCH_COOKIE_FILE` or `WATCH_BROWSER` settings take precedence. The `--cookies`
commands above are Douyin-specific. Other sites use downloader authentication;
YouTube additionally supports the browser workflow linked above.
