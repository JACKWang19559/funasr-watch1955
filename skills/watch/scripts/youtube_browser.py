"""YouTube fallback: dedicated manual login and continuous browser audio capture.

Capture original MediaSource audio bytes, not accelerated speaker output. No
cookie export, signed-URL extraction, remote JS component, or DRM processing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse
import wave

from filelock import FileLock, Timeout as LockTimeout

from auto_cookies import protect, setting
from config import CONFIG_DIR
from ffmpeg_utils import find_ffmpeg


class BrowserCaptureError(RuntimeError):
    pass


def youtube_id(source: str) -> str:
    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise BrowserCaptureError("Browser capture requires a YouTube video URL")
    if host == "youtu.be":
        value = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = parsed.path.strip("/").split("/")
        value = parse_qs(parsed.query).get("v", [""])[0]
        if len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
            value = parts[1]
    else:
        raise BrowserCaptureError("This browser fallback supports YouTube only")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise BrowserCaptureError("A single YouTube video ID is required")
    return value


def profile_path(value: str | Path | None = None) -> Path:
    path = Path(value or setting("WATCH_YOUTUBE_PROFILE") or
                CONFIG_DIR / "browser-profiles" / "youtube").expanduser().resolve()
    normalized = path.as_posix().lower()
    if any(marker in normalized for marker in
           ("/microsoft/edge/user data", "/google/chrome/user data")):
        raise BrowserCaptureError("Use a dedicated watch profile, not the personal browser profile")
    return path


def _channel(value: str | None = None) -> str:
    return value or setting("WATCH_COOKIE_BROWSER", "msedge")


def _browser_executable(channel: str) -> str:
    names = {"msedge": ("Microsoft/Edge/Application/msedge.exe", "msedge", "microsoft-edge"),
             "chrome": ("Google/Chrome/Application/chrome.exe", "google-chrome", "chrome")}
    if channel not in names:
        raise BrowserCaptureError("Manual login supports installed msedge or chrome")
    relative, *commands = names[channel]
    for name in commands:
        found = shutil.which(name)
        if found:
            return found
    for key in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        if os.environ.get(key):
            candidate = Path(os.environ[key]) / relative
            if candidate.is_file():
                return str(candidate)
    raise BrowserCaptureError(f"Install {channel} or choose an installed browser channel")


def open_login(source: str, *, profile: str | Path | None = None,
               channel: str | None = None) -> dict:
    identifier = youtube_id(source)
    directory = profile_path(profile)
    directory.mkdir(parents=True, exist_ok=True)
    protect(directory)
    subprocess.Popen([
        _browser_executable(_channel(channel)), f"--user-data-dir={directory}",
        "--no-first-run", "--no-default-browser-check", "--new-window",
        f"https://www.youtube.com/watch?v={identifier}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"login_pending": True, "profile": str(directory),
            "instruction": "Sign in manually, play the requested video, then close this dedicated browser window before capture."}


CAPTURE_INIT = r"""(() => {
  window.__watchAudio = [];
  const original = MediaSource.prototype.addSourceBuffer;
  MediaSource.prototype.addSourceBuffer = function(mime) {
    const sb = original.call(this, mime);
    if (mime.startsWith('audio/')) {
      const entry = {mime, chunks: [], ranges: []};
      window.__watchAudio.push(entry);
      const append = sb.appendBuffer.bind(sb);
      sb.appendBuffer = function(buffer) {
        entry.chunks.push(new Uint8Array(
          buffer instanceof ArrayBuffer ? buffer : buffer.buffer,
          buffer.byteOffset || 0, buffer.byteLength).slice());
        return append(buffer);
      };
      sb.addEventListener('updateend', () => {
        for (let i = 0; i < sb.buffered.length; i++)
          entry.ranges.push([sb.buffered.start(i), sb.buffered.end(i)]);
      });
    }
    return sb;
  };
})();"""


def merge_ranges(ranges: list[list[float]], tolerance: float = 0.05) -> list[list[float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if not all(math.isfinite(x) for x in (start, end)) or end <= start:
            continue
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def covers_video(ranges: list[list[float]], duration: float) -> bool:
    merged = merge_ranges(ranges)
    return (math.isfinite(duration) and duration > 0 and len(merged) == 1
            and merged[0][0] <= 0.1 and merged[0][1] >= duration - 0.25)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decode_duration(audio: Path, wav: Path) -> float:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise BrowserCaptureError("FFmpeg is required to validate captured audio")
    result = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", audio.as_posix(),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav.as_posix(),
    ], capture_output=True, timeout=120)
    if result.returncode:
        raise BrowserCaptureError("FFmpeg could not decode the captured audio")
    with wave.open(str(wav), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def _cached(out_dir: Path, identifier: str, frame_limit: int,
            resolution: int | None = None) -> dict | None:
    try:
        manifest = json.loads((out_dir / "capture.json").read_text(encoding="utf-8"))
        if not manifest.get("complete") or manifest["info"]["id"] != identifier:
            return None
        audio = Path(manifest["video_path"]).resolve()
        if not audio.is_relative_to(out_dir) or not audio.is_file() or _hash(audio) != manifest["sha256"]:
            return None
        frames = [frame for frame in manifest.get("browser_frames", [])
                  if frame.get("timestamp_seconds", 0) < manifest["info"].get("duration", math.inf) - 0.25]
        if frame_limit and resolution is not None and manifest.get("frame_resolution") != resolution:
            return None
        if frame_limit and (not frames or not all(
            Path(f["path"]).resolve().is_relative_to(out_dir) and Path(f["path"]).is_file()
            for f in frames)):
            return None
        if frame_limit and len(frames) > frame_limit:
            frames = ([frames[0]] if frame_limit == 1 else
                      [frames[round(i * (len(frames) - 1) / (frame_limit - 1))]
                       for i in range(frame_limit)])
        manifest["browser_frames"] = frames if frame_limit else []
        return manifest
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _save_audio(page, out_dir: Path) -> list[dict]:
    stats = page.evaluate("""() => window.__watchAudio.map(x => ({
      mime:x.mime, count:x.chunks.length, ranges:x.ranges
    }))""")
    for index, item in enumerate(stats):
        extension = ".webm" if "webm" in item["mime"] else ".m4a"
        path = out_dir / f"audio-source-{index}{extension}"
        with path.open("wb") as stream:
            for start in range(0, item["count"], 64):
                chunks = page.evaluate("""([index,start]) =>
                  window.__watchAudio[index].chunks.slice(start,start+64).map(a => {
                    let s='';
                    for(let i=0;i<a.length;i+=16384)
                      s+=String.fromCharCode(...a.subarray(i,i+16384));
                    return btoa(s);
                  })""", [index, start])
                for chunk in chunks:
                    stream.write(base64.b64decode(chunk))
        item.update(path=str(path), bytes=path.stat().st_size, ranges=merge_ranges(item["ranges"]))
    return stats


def capture(source: str, out_dir: Path, *, profile: str | Path | None = None,
            channel: str | None = None, rate: float = 4.0, max_frames: int = 16,
            resolution: int = 960, timeout: float | None = None) -> dict:
    identifier = youtube_id(source)
    if not math.isfinite(rate) or not 1 <= rate <= 4:
        raise BrowserCaptureError("Playback rate must be between 1 and 4")
    if max_frames < 0 or resolution < 64 or (timeout is not None and timeout <= 0):
        raise BrowserCaptureError("Invalid frame, resolution or timeout setting")
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = _cached(out_dir, identifier, max_frames, resolution)
    if cached:
        print("[watch] reusing verified browser audio and frames", file=sys.stderr)
        return cached
    directory = profile_path(profile)
    directory.mkdir(parents=True, exist_ok=True)
    protect(directory)
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise BrowserCaptureError("Browser fallback requires playwright in runtime.python") from None
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise BrowserCaptureError("FFmpeg is required for browser capture")

    try:
        with FileLock(str(directory) + ".watch.lock", timeout=0), \
                FileLock(str(out_dir / ".capture.lock"), timeout=0), sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(directory), channel=_channel(channel), headless=True,
                    locale="zh-CN", viewport={"width": 1280, "height": 800})
            except Exception:
                raise BrowserCaptureError("Cannot open the dedicated browser profile. Close its manual login window; do not close unrelated browser windows.") from None
            try:
                context.add_init_script(CAPTURE_INIT)
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(f"https://www.youtube.com/watch?v={identifier}&t=0s",
                              wait_until="domcontentloaded", timeout=45000)
                except BrowserTimeout:
                    pass
                page.wait_for_function("window.ytInitialPlayerResponse", timeout=20000)
                metadata = page.evaluate("""() => {
                  const p=window.ytInitialPlayerResponse, d=p.videoDetails || {};
                  return {id:d.videoId,title:d.title,uploader:d.author,
                    duration:Number(d.lengthSeconds),live:!!d.isLiveContent,
                    status:p.playabilityStatus?.status};
                }""")
                if metadata["status"] != "OK":
                    raise BrowserCaptureError("This video is not playable in the dedicated profile. Use --browser-login and complete any login or verification manually.")
                if metadata["id"] != identifier or metadata["live"]:
                    raise BrowserCaptureError("Expected a finite, non-live video matching the requested ID")
                page.wait_for_timeout(1000)
                playing = page.evaluate("document.querySelector('#movie_player')?.getPlayerState() === 1")
                if not playing:
                    button = page.locator("button.ytp-large-play-button")
                    if button.is_visible():
                        button.click(timeout=15000)
                    else:
                        page.locator("button.ytp-play-button").click(timeout=15000)
                page.wait_for_function("document.querySelector('video')?.currentTime > 0", timeout=20000)
                if page.evaluate("document.querySelector('#movie_player').classList.contains('ad-showing')"):
                    raise BrowserCaptureError("An advertisement is active; this fallback cannot verify its audio as the requested video")
                duration = page.evaluate("document.querySelector('video').duration")
                if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                    raise BrowserCaptureError("The player did not report a finite video duration")
                metadata.update(duration=duration, url=source)
                metadata.pop("status", None)
                metadata.pop("live", None)
                page.evaluate("rate => {document.querySelector('video').playbackRate=rate}", rate)
                deadline = time.monotonic() + (timeout or duration / rate * 2 + 90)
                last_time, last_progress, last_log = -1.0, time.monotonic(), 0.0
                interval = duration / max(1, max_frames)
                next_frame = 0.0
                frames: list[dict] = []
                failure = None
                while True:
                    state = page.evaluate("""() => {
                      const v=document.querySelector('video');
                      return {time:v.currentTime,ended:v.ended,paused:v.paused,
                        ad:document.querySelector('#movie_player').classList.contains('ad-showing')};
                    }""")
                    now = time.monotonic()
                    if state["ad"]:
                        failure = "An advertisement interrupted capture; the audio is not verified as complete"
                        break
                    if state["time"] > last_time + 0.1:
                        last_time, last_progress = state["time"], now
                    if now - last_log >= 20:
                        print(f"[watch] browser audio: {state['time']:.1f}/{duration:.1f}s", file=sys.stderr, flush=True)
                        last_log = now
                    if max_frames and not state["ended"] and len(frames) < max_frames and state["time"] >= next_frame:
                        path = out_dir / f"frame-{len(frames):04d}.jpg"
                        # Pause while taking a screenshot so its timestamp describes the image.
                        page.evaluate("document.querySelector('video').pause()")
                        actual_time = page.evaluate("document.querySelector('video').currentTime")
                        pixels = page.locator("video").first.screenshot()
                        frames.append({"path": str(path), "timestamp_seconds": actual_time,
                                       "reason": "browser-continuous-sample"})
                        if not state["ended"]:
                            page.evaluate("document.querySelector('video').play()")
                        image_result = subprocess.run([
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "image2pipe", "-i", "pipe:0", "-vf",
                            f"scale=w='min({resolution},iw)':h='min(1998,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                            "-frames:v", "1", "-q:v", "3", path.as_posix(),
                        ], input=pixels, capture_output=True, timeout=20)
                        if image_result.returncode:
                            raise BrowserCaptureError("FFmpeg could not save the browser frame")
                        next_frame += interval
                    if state["ended"]:
                        break
                    if now > deadline or now - last_progress > 40:
                        failure = "Capture timed out or playback stalled; partial audio is not complete"
                        break
                    if state["paused"]:
                        page.evaluate("document.querySelector('#movie_player').playVideo()")
                    page.wait_for_timeout(500)
                page.wait_for_timeout(500)
                buffers = _save_audio(page, out_dir)
                valid = [b for b in buffers if covers_video(b["ranges"], duration)]
                manifest = {"complete": False, "info": metadata, "subtitle_path": None,
                            "downloaded": False, "browser_frames": frames,
                            "frame_resolution": resolution,
                            "buffers": buffers, "method": "browser-original-audio", "playback_rate": rate}
                if not failure and valid:
                    selected = max(valid, key=lambda b: b["bytes"])
                    audio = Path(selected["path"])
                    decoded = decode_duration(audio, out_dir / "validated-audio.wav")
                    if abs(decoded - duration) <= max(0.5, duration * 0.001):
                        manifest.update(complete=True, video_path=str(audio),
                                        decoded_duration=decoded, sha256=_hash(audio))
                    else:
                        failure = "Decoded audio duration differs from the actual player duration"
                if not manifest["complete"]:
                    manifest["error"] = failure or "No single audio buffer covers the complete video"
                (out_dir / "capture.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                if not manifest["complete"]:
                    raise BrowserCaptureError(manifest["error"])
                return manifest
            finally:
                context.close()
    except LockTimeout:
        raise BrowserCaptureError("This dedicated profile or output directory is already in use") from None
    except BrowserTimeout:
        raise BrowserCaptureError("The browser did not reach playable video state. Complete any login/consent manually with --browser-login.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["login", "capture"])
    parser.add_argument("source")
    parser.add_argument("--profile")
    parser.add_argument("--channel", choices=["msedge", "chrome"])
    parser.add_argument("--out-dir", default=".watch-work/youtube-browser")
    parser.add_argument("--rate", type=float, default=4)
    parser.add_argument("--max-frames", type=int, default=16)
    parser.add_argument("--resolution", type=int, default=960)
    parser.add_argument("--timeout", type=float)
    args = parser.parse_args()
    try:
        if args.mode == "login":
            result = open_login(args.source, profile=args.profile, channel=args.channel)
        else:
            result = capture(args.source, Path(args.out_dir), profile=args.profile,
                             channel=args.channel, rate=args.rate, max_frames=args.max_frames,
                             resolution=args.resolution, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BrowserCaptureError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
