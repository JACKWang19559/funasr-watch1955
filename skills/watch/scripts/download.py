#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from auto_cookies import acquire, cookie_file_matches, enabled, normalize_url, resolve_video, setting, CookieError


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# 浏览器 cookie 来源（用于拉取需要登录才能访问的字幕，如 B 站）。
# 可通过环境变量 WATCH_BROWSER 覆盖。支持 chrome/edge/firefox/brave/chromium/opera/safari/vivaldi。
# 设为空字符串则禁用 cookie 读取。
#
# 如果浏览器 cookie 解密失败（如 Edge 120+ 的 App-Bound Encryption），
# 可用浏览器扩展导出 cookies.txt 文件，然后设置环境变量 WATCH_COOKIE_FILE 指向它。
# 优先级：WATCH_COOKIE_FILE > WATCH_BROWSER
#
# 注意：这些值也可以写在 ~/.config/watch/.env 文件里（由 config.py 管理）。
# _cookie_args() 会动态读取 .env，所以这里只用环境变量作为初始默认值。
DEFAULT_BROWSER = ""


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _cookie_args(source: str = "", *, force: bool = False) -> list[str]:
    """Prefer explicit credentials, then the automatic site-specific cache."""
    cookie_file = setting("WATCH_COOKIE_FILE")
    if not force and cookie_file and cookie_file_matches(Path(cookie_file).expanduser(), source):
        return ["--cookies", str(Path(cookie_file).expanduser())]
    browser = setting("WATCH_BROWSER", DEFAULT_BROWSER)
    if not force and browser:
        return ["--cookies-from-browser", browser]
    if enabled(source):
        try:
            return acquire(source, force=force).arguments()
        except CookieError as exc:
            raise SystemExit(f"Automatic cookies failed: {exc}. Run codex_watch.py --cookies --interactive <URL> if manual verification is needed.") from None
    return []


_AUTO_REFRESHED: set[str] = set()
_BROWSER_RESOLVED: set[str] = set()


def _replace_cookie_args(cmd: list[str], arguments: list[str]) -> list[str]:
    result: list[str] = []
    index = 0
    while index < len(cmd):
        if cmd[index] in {"--cookies", "--cookies-from-browser", "--user-agent", "--referer"}:
            index += 2
        else:
            result.append(cmd[index])
            index += 1
    position = result.index("--")
    return result[:position] + arguments + result[position:]


def _run_ytdlp(cmd: list[str], source: str) -> subprocess.CompletedProcess:
    cmd = cmd[:1] + ["--socket-timeout", "20", "--retries", "1", "--extractor-retries", "0"] + cmd[1:]
    site = urlparse(source).hostname or ""
    if site in _BROWSER_RESOLVED and enabled(source):
        return _run_browser_resolved(cmd, source)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    output = (result.stdout or "") + (result.stderr or "")
    sys.stderr.write(output)
    needs_cookies = any(message in output.lower() for message in ("fresh cookies", "http error 403", "cookies are needed"))
    if needs_cookies and enabled(source) and site not in _AUTO_REFRESHED:
        _AUTO_REFRESHED.add(site)
        print("[watch] site rejected the session; refreshing cookies once…", file=sys.stderr)
        cmd = _replace_cookie_args(cmd, _cookie_args(source, force=True))
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        sys.stderr.write((result.stdout or "") + (result.stderr or ""))
    output = (result.stdout or "") + (result.stderr or "")
    if any(message in output.lower() for message in ("fresh cookies", "http error 403", "cookies are needed")) and enabled(source):
        return _run_browser_resolved(cmd, source)
    return result


def _run_browser_resolved(cmd: list[str], source: str) -> subprocess.CompletedProcess:
    try:
        info_path = resolve_video(source)
    except CookieError as exc:
        raise SystemExit(f"Browser video resolution failed: {exc}") from None
    position = cmd.index("--")
    cmd = cmd[:position] + ["--http-chunk-size", "5M", "--load-info-json", str(info_path)]
    print("[watch] using video media resolved by the local browser…", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stderr.write((result.stdout or "") + (result.stderr or ""))
    if result.returncode == 0:
        _BROWSER_RESOLVED.add(urlparse(source).hostname or "")
    return result


def _pick_subtitle(out_dir: Path) -> Path | None:
    """选择最佳字幕文件，优先中文，其次英文，最后任意可用字幕。

    支持 VTT 和 SRT 两种格式（Bilibili 提供 SRT，YouTube 提供 VTT）。

    Args:
        out_dir: 字幕文件所在目录

    Returns:
        字幕文件路径，或 None 如果没有字幕
    """
    # 同时收集 VTT 和 SRT 文件
    candidates = sorted(
        list(out_dir.glob("video*.vtt")) + list(out_dir.glob("video*.srt"))
    )
    if not candidates:
        return None
    # 优先中文字幕（zh., zh-CN., zh-Hans. 等）
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".zh.", ".zh-CN.", ".zh-Hans.", ".zh-Hant.", ".zh-orig."))
    ]
    if preferred:
        return preferred[0]
    # 其次英文字幕
    preferred = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB.", ".en-orig."))
    ]
    return preferred[0] if preferred else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def fetch_captions(url: str, out_dir: Path) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    url = normalize_url(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        # 优先中文字幕，其次英文，最后任意语言
        # sub-format 按 srt/vtt/best 顺序尝试，避免 Bilibili SRT 转 VTT 失败
        # 不强制 convert-subs，保留原始格式（transcribe.py 同时支持 VTT 和 SRT）
        "--sub-langs", "zh.*,en.*,all",
        "--sub-format", "srt/vtt/best",
        "--no-playlist",
        "--ignore-errors",
    ]
    # 按 URL 域名选择 cookie 来源（B 站用 cookie 文件，其他网站用浏览器 cookie）
    cmd += _cookie_args(url)
    cmd += [
        "-o", output_template,
        "--",
        url,
    ]
    _run_ytdlp(cmd, url)
    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    url = normalize_url(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    # audio_only 时优先纯音频流，回退到最佳音视频合并流（部分平台如抖音
    # 不提供独立音频流，需要下载完整视频再由 ffmpeg 提取音频）
    fmt = "ba/b" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        # 优先中文字幕，其次英文，最后任意语言
        # sub-format 按 srt/vtt/best 顺序尝试，避免 Bilibili SRT 转 VTT 失败
        # 不强制 convert-subs，保留原始格式（transcribe.py 同时支持 VTT 和 SRT）
        "--sub-langs", "zh.*,en.*,all",
        "--sub-format", "srt/vtt/best",
        "--no-playlist",
        "--ignore-errors",
    ]
    # 按 URL 域名选择 cookie 来源（B 站用 cookie 文件，其他网站用浏览器 cookie）
    cmd += _cookie_args(url)
    cmd += [
        "-o", output_template,
        "--",
        url,
    ]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    result = _run_ytdlp(cmd, url)
    video = _pick_video(out_dir)
    if video is None:
        host = (urlparse(url).hostname or "").lower()
        hint = ""
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            hint = (". For a playable YouTube video, retry with --via-browser; "
                    "if login is required, use codex_watch.py --browser-login <URL> "
                    "and close that dedicated window before retrying")
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode}){hint}"
        )

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
) -> dict:
    if is_url(source):
        return download_url(source, out_dir, audio_only=audio_only)
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
