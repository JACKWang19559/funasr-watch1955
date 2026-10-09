"""Bootstrap the installed watch skill with its dedicated local runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    scripts = Path(__file__).resolve().parent
    runtime_path = scripts / "runtime.json"
    if not runtime_path.is_file():
        raise SystemExit("Missing runtime.json; install the skill with codex-adapter/install_codex.py")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    python = Path(runtime["python"])
    if not python.is_file():
        raise SystemExit(f"Watch runtime is missing: {python}")
    env = os.environ.copy()
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    if runtime.get("ffmpeg"):
        ffmpeg = Path(runtime["ffmpeg"])
        env["FFMPEG_PATH"] = str(ffmpeg)
        env["FFPROBE_PATH"] = str(ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe"))
        env["PATH"] = str(ffmpeg.parent) + os.pathsep + env["PATH"]
    env["WATCH_PYTHON"] = str(python)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("MODELSCOPE_CACHE", runtime["model_cache"])
    for key, setting in (("model_dir", "WATCH_MODEL_DIR"), ("vad_model_dir", "WATCH_VAD_MODEL_DIR")):
        directory = runtime.get(key)
        if directory and (Path(directory) / "model.pt").is_file():
            env.setdefault(setting, directory)
    args = sys.argv[1:]
    target = "watch.py"
    if args and args[0] == "--setup":
        target = "setup.py"
        args = args[1:]
    elif args and args[0] == "--cookies":
        target = "auto_cookies.py"
        args = args[1:]
    elif args and args[0] in {"--browser-login", "--browser-capture"}:
        mode = "login" if args[0] == "--browser-login" else "capture"
        target = "youtube_browser.py"
        args = [mode, *args[1:]]
    return subprocess.call([str(python), "-u", str(scripts / target), *args], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
