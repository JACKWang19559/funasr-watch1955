# YouTube browser fallback

Read this when yt-dlp cannot retrieve a video that is playable in the user's
browser. Keep the original video URL as the source of evidence. yt-dlp is the
main downloader (a descendant of youtube-dl); installing another downloader
does not by itself resolve login or signature challenges.

## Choose the next step

- Use the normal launcher first. Existing `WATCH_COOKIE_FILE` or `WATCH_BROWSER`
  can provide an already authorized downloader session.
- A locked browser cookie database or App-Bound Encryption is not solved by
  repeatedly reading the same personal profile. Use a dedicated profile below.
- After a logged-in downloader attempt still fails with a bot check, an
  `n`/signature challenge, or “page needs to be reloaded”, switch to browser
  capture. Node alone may not supply yt-dlp's EJS solver. Do not silently fetch
  and execute remote solver components in a logged-in session or work around an
  approval rejection by using another installer.
- A listed caption track or HTTP 200 is not proof of usable subtitles. Check
  that the body contains timed text. After one empty response, use local ASR
  rather than repeatedly probing timedtext/get_transcript clients.
- Avoid searching thumbnail logos and unrelated mirrors before obtaining the
  available original speech. Cross-check names against readable title cards.

## Dedicated login

The Codex launcher uses `runtime.python` and accepts the commands below. Paths
are placeholders: resolve the launcher relative to the installed skill and run
in the user's workspace. For Trae/direct CLI, use the Python interpreter from
preflight and the direct commands shown below. `--profile`/`--browser-profile`
must name a dedicated watch profile.
Omit it to use `~/.config/watch/browser-profiles/youtube`, or set
`WATCH_YOUTUBE_PROFILE` for a previously authorized dedicated profile.

```text
<python> <launcher> --browser-login <URL> --profile <dedicated-profile>
```

Direct CLI equivalent, from the repository root:

```text
<python> skills/watch/scripts/youtube_browser.py login <URL> --profile <dedicated-profile>
```

This opens an ordinary installed Edge window, without automation/debugging
flags. Ask the user to sign in manually, play the requested video, and close
that window. Wait for their response when manual login is required. Google may
reject login inside an automated or embedded browser; repeating that login is
not useful. Do not automate a verification challenge. Do not terminate unrelated
browser processes or change system browser/security settings.

The dedicated profile retains the session for follow-ups. Cookie values remain
in the profile; do not export, print, upload, or copy the profile into the skill.
Login does not guarantee download success: the fallback uses browser playback.

## Capture and analyze

```text
<python> <launcher> <URL> --via-browser --browser-profile <dedicated-profile> --out-dir <work-dir>
```

Direct CLI equivalent:

```text
<python> skills/watch/scripts/watch.py <URL> --via-browser --browser-profile <dedicated-profile> --out-dir <work-dir>
```

This captures original audio while playing continuously at up to 4×, samples
actual video frames, validates completeness, and runs local FunASR. Add
`--detail transcript` to skip frames; `--browser-rate 1` to use normal playback.
The default browser frame cap is 16; `--max-frames` overrides it. `--start` and
`--end` filter the resulting report, but capture still plays the entire video.
`--timestamps` is unavailable in this mode because continuous playback cannot
promise an exact requested screenshot; use a downloaded video for exact cues.

For capture alone, without ASR:

```text
<python> <launcher> --browser-capture <URL> --profile <dedicated-profile> --out-dir <capture-dir> --rate 4 --max-frames 16
```

Dependencies are Playwright, installed Edge (default) or Chrome, and
the existing FFmpeg runtime. Use `WATCH_COOKIE_BROWSER=chrome` for Chrome, or
`--channel chrome` on the standalone login/capture commands. Install missing
dependencies only in `runtime.python`; this workflow does not need a remote
EJS component. Preserve existing runtime and GPU/VAD preferences.

## What makes a capture trustworthy

- The helper clicks the actual play control. Merely calling `video.play()`
  before YouTube initializes can leave the player paused without media.
- It copies audio `SourceBuffer.appendBuffer` payloads before playback. These
  are original audio bytes, so playback rate does not accelerate the saved
  speech or its timestamps. This is different from recording speaker output.
- It does not repeatedly seek or switch playback quality. Rapid seeking left
  large audio gaps in the observed workflow; switching quality created another
  source buffer. A format URL can still return 403 while ordinary playback
  works, and SABR responses are not necessarily standalone MP4 files.
- Each audio source buffer is saved with the matching container extension.
  At least one must cover the whole actual player duration. The helper also
  decodes the audio and compares its sample duration with the player duration;
  a WebM header can advertise the entire video even when only the first seconds
  were saved. Do not concatenate incomplete or incompatible buffers as proof
  of full coverage.
- `capture.json` marks `complete` only after both checks pass. A stalled or
  incomplete capture remains incomplete and must not be summarized as fully
  watched. On timeout, inspect the saved diagnosis and stop repeating unchanged
  attempts. Encrypted media, unavailable audio buffers, live video, or inability
  to play are limitations of this fallback; report them or use another
  authorized source.
- Only metadata, original audio, frames, and coverage information are saved.
  Cookies, authorization headers, caption tokens, and signed media URLs are
  not written to the report. Reuse a verified capture in the same output
  directory for follow-ups. Do not redownload solely to regenerate a summary.

FunASR runs on bounded 60-second windows when needed. When it does not provide
native sentence timing, the transcript labels the window `[MM:SS–MM:SS]` and
stores `timestamp_kind: window` in `transcript.json`. These ranges locate a
section; use actual sampled frames or a focused source check for precise claims.
