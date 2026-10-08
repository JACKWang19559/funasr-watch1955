"""Acquire and cache Douyin cookies in a separate local browser profile."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import getpass
from http.cookiejar import Cookie, MozillaCookieJar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from filelock import FileLock

from config import CONFIG_DIR, read_env_file


class CookieError(RuntimeError):
    pass


def setting(name: str, default: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    return read_env_file().get(name, default)


def domain_matches(host: str, domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    host = host.lower().rstrip(".")
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_douyin(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and domain_matches(parsed.hostname or "", "douyin.com")


def normalize_url(source: str) -> str:
    if not is_douyin(source):
        return source
    parsed = urlparse(source)
    video_id = parse_qs(parsed.query).get("modal_id", [""])[0]
    if re.fullmatch(r"\d+", video_id):
        return f"https://www.douyin.com/video/{video_id}"
    return source


def video_id(source: str) -> str | None:
    if not is_douyin(source):
        return None
    match = re.search(r"/video/(\d+)", urlparse(normalize_url(source)).path)
    return match[1] if match else None


def media_info(detail: dict, source: str, user_agent: str) -> dict | None:
    expected = video_id(source)
    if not expected or str(detail.get("aweme_id", "")) != expected:
        return None
    video = detail.get("video") or {}
    formats = []
    variants = [video, *(video.get("bit_rate") or [])]
    seen = set()
    for variant in variants:
        bitrate = variant.get("bit_rate")
        if not isinstance(bitrate, (int, float)):
            bitrate = 0
        for address in ("play_addr", "play_addr_h264", "play_addr_bytevc1"):
            address_info = variant.get(address) or {}
            for url in address_info.get("url_list", []):
                if urlparse(url).scheme not in {"http", "https"} or url in seen:
                    continue
                seen.add(url)
                formats.append({"url": url, "format_id": f"browser-{len(formats)}", "ext": "mp4",
                                "height": address_info.get("height") or variant.get("height") or video.get("height"),
                                "width": address_info.get("width") or variant.get("width") or video.get("width"),
                                "tbr": bitrate / 1000,
                                "source_preference": -10 if domain_matches(urlparse(url).hostname or "", "douyin.com") else 1,
                                "filesize": address_info.get("data_size")})
    if not formats:
        return None
    return {"id": expected, "title": detail.get("desc") or f"Douyin {expected}",
            "uploader": (detail.get("author") or {}).get("nickname"),
            "duration": (video.get("duration") or detail.get("duration") or 0) / 1000,
            "formats": formats, "webpage_url": normalize_url(source),
            "extractor": "DouyinBrowser", "extractor_key": "Douyin",
            "http_headers": {"User-Agent": user_agent, "Referer": "https://www.douyin.com/"}}


def enabled(source: str) -> bool:
    return is_douyin(source) and setting("WATCH_AUTO_COOKIES", "true").lower() not in {"0", "false", "off", "no"}


def protect(path: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r",
             f"{getpass.getuser()}:(OI)(CI)F" if path.is_dir() else f"{getpass.getuser()}:F",
             "*S-1-5-18:(OI)(CI)F" if path.is_dir() else "*S-1-5-18:F"],
            capture_output=True,
        )
        if result.returncode:
            raise CookieError("Could not restrict the local cookie cache permissions")
    else:
        path.chmod(0o700 if path.is_dir() else 0o600)


def cookie_file_matches(path: Path, source: str) -> bool:
    host = urlparse(source).hostname or ""
    try:
        jar = MozillaCookieJar(str(path))
        jar.load(ignore_discard=True)
        return any(domain_matches(host, cookie.domain) and not cookie.is_expired() for cookie in jar)
    except (OSError, ValueError):
        return False


def export_cookies(cookies: list[dict], path: Path) -> int:
    jar = MozillaCookieJar(str(path))
    for entry in cookies:
        domain = entry.get("domain", "")
        if not domain_matches(domain.lstrip("."), "douyin.com"):
            continue
        expires = int(entry.get("expires", -1))
        if expires > 0 and expires <= time.time():
            continue
        jar.set_cookie(Cookie(
            version=0, name=entry["name"], value=entry["value"],
            port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith("."),
            domain_initial_dot=domain.startswith("."),
            path=entry.get("path", "/"), path_specified=True,
            secure=entry.get("secure", False), expires=expires if expires > 0 else None,
            discard=expires <= 0, comment=None, comment_url=None,
            rest={"HTTPOnly": None} if entry.get("httpOnly") else {}, rfc2109=False,
        ))
    if not len(jar):
        raise CookieError("The page did not issue any Douyin cookies; an interactive visit may be required")
    temporary = path.with_suffix(".tmp")
    jar.save(str(temporary), ignore_discard=True, ignore_expires=False)
    protect(temporary)
    temporary.replace(path)
    return len(jar)


@dataclass(frozen=True)
class CookieSession:
    path: Path
    user_agent: str
    count: int
    cached: bool

    def arguments(self) -> list[str]:
        args = ["--cookies", str(self.path)]
        if self.user_agent:
            args += ["--user-agent", self.user_agent]
        return args + ["--referer", "https://www.douyin.com/"]


def _browser_cookies(source: str, profile: Path, *, interactive: bool, timeout: float) -> tuple[list[dict], str, dict | None]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise CookieError("Automatic cookies require: pip install playwright") from None
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile), channel=setting("WATCH_COOKIE_BROWSER", "msedge"),
                headless=not interactive, locale="zh-CN", viewport={"width": 1280, "height": 800},
            )
        except Exception:
            raise CookieError("Cannot launch the cookie browser; install Edge or configure WATCH_COOKIE_BROWSER") from None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            details = []

            def capture(response):
                if "/aweme/detail/" in urlparse(response.url).path and response.status == 200:
                    try:
                        detail = response.json().get("aweme_detail")
                        if isinstance(detail, dict) and str(detail.get("aweme_id", "")) == video_id(source):
                            details.append(detail)
                    except (ValueError, AttributeError):
                        pass

            page.on("response", capture)
            try:
                page.goto(normalize_url(source), wait_until="domcontentloaded", timeout=timeout * 1000)
            except BrowserTimeout:
                pass  # A page can issue useful cookies before navigation finishes.
            deadline = time.monotonic() + (timeout if interactive else min(timeout, 15))
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                names = {cookie["name"] for cookie in context.cookies("https://www.douyin.com/")}
                if {"ttwid", "__ac_signature"} & names and not interactive and (details or not video_id(source)):
                    page.wait_for_timeout(2000)
                    break
            if not is_douyin(page.url):
                raise CookieError("The browser was redirected away from Douyin")
            user_agent = page.evaluate("navigator.userAgent")
            info = media_info(details[-1], source, user_agent) if details else None
            return context.cookies(), user_agent, info
        except CookieError:
            raise
        except Exception as exc:
            raise CookieError(f"Cookie browser visit failed ({type(exc).__name__})") from None
        finally:
            context.close()


def acquire(source: str, *, force: bool = False, interactive: bool = False,
            timeout: float = 45, cache_dir: Path | None = None) -> CookieSession:
    if not is_douyin(source):
        raise CookieError("Automatic cookie acquisition currently supports Douyin URLs only")
    root = cache_dir or CONFIG_DIR / "auto-cookies"
    root.mkdir(parents=True, exist_ok=True)
    protect(root)
    path = root / "douyin.txt"
    metadata = root / "douyin.json"
    try:
        max_age = max(60, min(86400, int(setting("WATCH_COOKIE_MAX_AGE", "21600"))))
    except ValueError:
        max_age = 21600
    with FileLock(str(root / "douyin.lock"), timeout=60):
        if not force and not interactive and path.exists() and metadata.exists():
            try:
                info = json.loads(metadata.read_text(encoding="utf-8"))
                age = time.time() - info["created"]
                if 0 <= age < max_age and cookie_file_matches(path, source):
                    return CookieSession(path, info.get("user_agent", ""), info["count"], True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        cookies, user_agent, video = _browser_cookies(source, root / "edge-profile", interactive=interactive, timeout=timeout)
        count = export_cookies(cookies, path)
        info = {"created": time.time(), "user_agent": user_agent, "count": count}
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps(info), encoding="utf-8")
        protect(temporary)
        temporary.replace(metadata)
        if video:
            video_path = root / f"video-{video['id']}.json"
            temporary = video_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"created": time.time(), "info": video}), encoding="utf-8")
            protect(temporary)
            temporary.replace(video_path)
        print(f"[watch] automatically acquired {count} Douyin cookies", file=sys.stderr)
        return CookieSession(path, user_agent, count, False)


def resolve_video(source: str, *, cache_dir: Path | None = None) -> Path:
    expected = video_id(source)
    if not expected:
        raise CookieError("A canonical Douyin video ID is needed for browser resolution")
    root = cache_dir or CONFIG_DIR / "auto-cookies"
    video_path = root / f"video-{expected}.json"
    for attempt in range(2):
        try:
            data = json.loads(video_path.read_text(encoding="utf-8"))
            if 0 <= time.time() - data["created"] < 900 and data["info"]["id"] == expected:
                for fmt in data["info"].get("formats", []):
                    fmt["source_preference"] = -10 if domain_matches(urlparse(fmt["url"]).hostname or "", "douyin.com") else 1
                info_path = root / f"resolved-{expected}.info.json"
                temporary = info_path.with_suffix(f".{uuid4().hex}.tmp")
                temporary.write_text(json.dumps(data["info"]), encoding="utf-8")
                protect(temporary)
                temporary.replace(info_path)
                return info_path
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if attempt == 0:
            acquire(source, force=True, cache_dir=cache_dir)
    raise CookieError("The browser could not obtain this video's media; manual login or verification may be required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Acquire Douyin cookies locally without printing cookie values")
    parser.add_argument("source", nargs="?", default="https://www.douyin.com/")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--interactive", action="store_true", help="Open a separate browser window for manual login or verification")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    try:
        result = acquire(args.source, force=args.refresh, interactive=args.interactive, timeout=args.timeout)
    except CookieError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ready": True, "file": str(result.path), "count": result.count, "cached": result.cached}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
