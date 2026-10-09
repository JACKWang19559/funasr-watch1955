"""Install the watch skill and generate machine-local runtime paths."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    adapter = root / "codex-adapter"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python with the project dependencies installed")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"), help="Full FFmpeg executable path")
    parser.add_argument("--model-cache", type=Path, default=Path.home() / ".cache" / "funasr-watch")
    parser.add_argument("--model-dir", type=Path, help="Optional local SenseVoiceSmall directory")
    parser.add_argument("--vad-model-dir", type=Path, help="Optional local FSMN-VAD directory")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--dest", type=Path, default=codex_home / "skills" / "watch")
    parser.add_argument("--force", action="store_true", help="Update an existing watch installation")
    args = parser.parse_args()
    # Keep venv symlinks intact: resolving them can select the system interpreter.
    python = Path(args.python).expanduser().absolute()
    if not python.is_file():
        parser.error(f"Python not found: {python}")
    if not args.ffmpeg:
        parser.error("FFmpeg is missing. Install it or pass --ffmpeg <absolute path>.")
    ffmpeg = Path(args.ffmpeg).expanduser().absolute()
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    if not ffmpeg.is_file() or not ffprobe.is_file():
        parser.error("Both ffmpeg and ffprobe must exist in the selected directory")
    target = args.dest.expanduser().absolute()
    if target.exists() and not args.force:
        parser.error(f"Skill already exists at {target}; use --force to update it")
    runtime = {"python": str(python), "ffmpeg": str(ffmpeg),
               "model_cache": str(args.model_cache.expanduser().absolute())}
    for key in ("model_dir", "vad_model_dir"):
        directory = getattr(args, key)
        if directory:
            directory = directory.expanduser().absolute()
            if not (directory / "model.pt").is_file():
                parser.error(f"Missing model.pt in {directory}")
            runtime[key] = str(directory)
    scripts = target / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for source in (root / "skills" / "watch" / "scripts").glob("*.py"):
        shutil.copy2(source, scripts / source.name)
    shutil.copy2(adapter / "codex_watch.py", scripts / "codex_watch.py")
    shutil.copy2(adapter / "SKILL.md", target / "SKILL.md")
    references = root / "skills" / "watch" / "references"
    if references.is_dir():
        shutil.copytree(references, target / "references", dirs_exist_ok=True)
    (scripts / "runtime.json").write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Installed watch skill: {target}")
    print("Default placement: ASR=auto, VAD=cpu (existing preferences are preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
