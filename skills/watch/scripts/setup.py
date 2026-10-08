#!/usr/bin/env python3
"""Setup / preflight for /watch.

Modes:
  setup.py --check      Silent preflight. Exit 0 if ready, 2/3/4 on failure.
  setup.py --json       Machine-readable status for Claude to parse.
  setup.py              Installer. Auto-installs deps, scaffolds .env, marks SETUP_COMPLETE.

Design:
- Silent on success: --check exits 0 with no output when everything's ready so
  that /watch doesn't spam "setup is complete" on every turn.
- Idempotent: re-running the installer is safe — it never clobbers existing
  keys and only appends missing ones.
- SETUP_COMPLETE=true in ~/.config/watch/.env tells us the user has been
  through a successful installer run at least once.
- Never sudo. On macOS, auto-install via brew. Elsewhere, print exact commands.
- Never write an API key to disk automatically — only scaffold placeholders.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# 优先使用完整版 ffmpeg（绕过 Trae 自带的精简版）
from ffmpeg_utils import find_ffmpeg, find_ffprobe

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from config import get_config  # noqa: E402


REQUIRED_BINARIES = ["ffmpeg", "ffprobe", "yt-dlp"]
CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"
ENV_TEMPLATE = """# /watch 配置文件
#
# 本地转写使用 FunASR + SenseVoiceSmall（阿里达摩院开源模型）
# 无需 API key，完全离线运行（首次运行需下载模型约 0.9GB）
#
# 安装依赖：
#   pip install funasr torch
# GPU 加速（可选，需 NVIDIA 显卡）：
#   pip install torch --index-url https://download.pytorch.org/whl/cu121

# 转写设备配置
# auto  - 自动检测（有 GPU 用 GPU，否则用 CPU）
# cuda  - 强制用 GPU
# cpu   - 强制用 CPU
WATCH_TRANSCRIBE_DEVICE=auto

# 默认 CPU 分段 + GPU 识别；auto 跟随识别设备，也可设为 cuda/cpu
WATCH_VAD_DEVICE=cpu

# 系统 Python 解释器路径（含 FunASR 的 Python）
# Trae 自带的 Python 3.10 缺少 FunASR，这里记录系统 Python 路径，
# 供 SKILL.md 调用脚本时使用，绕过 Trae Python 环境。
# 由 setup.py 自动检测并写入，无需手动编辑。
# WATCH_PYTHON=C:\\Users\\...\\python.exe

# 工作目录根路径（watch 插件产生的所有中间文件都会放在这里）
# 不设置时默认使用脚本所在目录（可能在 C 盘插件目录下）。
# 设置后，工作文件会生成在 <WATCH_WORK_DIR>/.watch-work/<时间戳>/ 下。
# 建议设置为用户项目根目录，例如：
#   WATCH_WORK_DIR=D:\\my-project
#   WATCH_WORK_DIR=C:\\Users\\Administrator\\Documents\\my-project
# WATCH_WORK_DIR=

# Default watch behavior (the /watch first-run wizard sets this for you).
# Allowed values: transcript | efficient | balanced | token-burner
# Keep the value on its own line with no trailing comment.
# WATCH_DETAIL=balanced
"""


def _which(name: str) -> str | None:
    """查找可执行文件，对 ffmpeg/ffprobe 优先返回完整版路径。

    在 Windows 上，PATH 中的 ffmpeg 可能是某些应用自带的精简版（如 Trae IDE 的
    ffmpeg 只用于视频合并，没有 image2 muxer）。对 ffmpeg/ffprobe 使用
    ffmpeg_utils.find_* 函数，它会检测完整性并优先返回完整版。
    """
    if name == "ffmpeg":
        return find_ffmpeg()
    if name == "ffprobe":
        return find_ffprobe()
    return shutil.which(name)


def _check_binaries() -> list[str]:
    return [b for b in REQUIRED_BINARIES if not _which(b)]


_PERM_WARNED: set[str] = set()


def _check_file_permissions(path: Path) -> None:
    """Warn to stderr (once per path per process) if a secrets file is
    world/group readable."""
    key = str(path)
    if key in _PERM_WARNED:
        return
    try:
        mode = path.stat().st_mode
        if mode & 0o044:
            _PERM_WARNED.add(key)
            sys.stderr.write(
                f"[watch] WARNING: {path} is readable by other users. "
                f"Run: chmod 600 {path}\n"
            )
            sys.stderr.flush()
    except OSError:
        pass


def _read_env_key(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if not CONFIG_FILE.exists():
        return None
    _check_file_permissions(CONFIG_FILE)
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() != name:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1]
            return raw or None
    except OSError:
        return None
    return None


def _check_funasr_import(python_path: str) -> bool:
    """检查指定 Python 解释器是否安装了 funasr。

    使用 importlib.util.find_spec 检测，比直接 import 快得多
    （不需要加载 PyTorch 等重依赖，避免超时）。

    Args:
        python_path: Python 可执行文件路径

    Returns:
        True 如果该 Python 安装了 funasr
    """
    try:
        result = subprocess.run(
            [python_path, "-c",
             "import importlib.util; "
             "exit(0 if importlib.util.find_spec('funasr') else 1)"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _get_python_candidates() -> list[Path]:
    """获取候选 Python 路径列表，按版本从新到旧排序。

    扫描 Windows 和 Unix-like 系统的常见 Python 安装路径。

    Returns:
        Python 可执行文件路径列表（Path 对象）
    """
    candidates: list[Path] = []

    if sys.platform == "win32":
        # Windows 常见路径
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))

        # 用户级安装: %LOCALAPPDATA%\Programs\Python\Python3XX\python.exe
        if local_app.exists():
            python_dir = local_app / "Programs" / "Python"
            if python_dir.exists():
                for child in sorted(python_dir.iterdir(), reverse=True):
                    if child.is_dir() and child.name.startswith("Python3"):
                        candidates.append(child / "python.exe")

        # 系统级安装: C:\Program Files\Python3XX\python.exe
        if program_files.exists():
            for child in sorted(program_files.iterdir(), reverse=True):
                if child.is_dir() and child.name.startswith("Python3"):
                    candidates.append(child / "python.exe")

        # 其他常见路径
        for ver in range(13, 7, -1):  # 3.13 到 3.8
            candidates.append(Path(rf"C:\Python3{ver}\python.exe"))
    else:
        # Unix-like 系统
        for ver in range(13, 7, -1):
            candidates.append(Path(f"/usr/bin/python3.{ver}"))
            candidates.append(Path(f"/usr/local/bin/python3.{ver}"))
        candidates.append(Path("/usr/bin/python3"))
        candidates.append(Path("/usr/local/bin/python3"))

    return candidates


def _find_python_with_funasr() -> str | None:
    """查找含有 FunASR 的系统 Python 路径。

    优先级：
    1. 环境变量 WATCH_PYTHON（如果设置且验证通过）
    2. .env 中的 WATCH_PYTHON（如果设置且验证通过）
    3. 扫描常见 Python 安装路径，返回第一个能导入 funasr 的

    Returns:
        Python 可执行文件路径，或 None 如果未找到
    """
    # 1. 检查环境变量
    env_python = os.environ.get("WATCH_PYTHON", "")
    if env_python and Path(env_python).exists():
        if _check_funasr_import(env_python):
            return env_python

    # 2. 检查 .env 中的 WATCH_PYTHON
    stored = _read_env_key("WATCH_PYTHON")
    if stored and Path(stored).exists():
        if _check_funasr_import(stored):
            return stored

    # 3. 扫描常见安装路径
    for py_path in _get_python_candidates():
        if py_path.exists() and _check_funasr_import(str(py_path)):
            return str(py_path)

    return None


def _write_watch_python(python_path: str) -> None:
    """将系统 Python 路径写入 .env 的 WATCH_PYTHON 变量。

    幂等操作：如果 WATCH_PYTHON 已存在且值相同，不写入；
    如果存在但值不同，更新；如果不存在，追加。

    Args:
        python_path: Python 可执行文件路径
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if CONFIG_FILE.exists():
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()

    # 查找 WATCH_PYTHON 行
    found = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("WATCH_PYTHON="):
            existing = stripped.split("=", 1)[1].strip()
            if existing == python_path:
                # 值相同，无需更新
                return
            lines[i] = f"WATCH_PYTHON={python_path}"
            found = True
            break

    if not found:
        # 追加到文件末尾
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"WATCH_PYTHON={python_path}")

    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _have_funasr() -> bool:
    """检查 FunASR 是否可用（当前 Python 或系统 Python）。

    Trae 自带的 Python 3.10 缺少 FunASR，但系统 Python 3.11 可能已安装。
    只要任一 Python 能导入 funasr，就返回 True。
    """
    # 当前 Python 能导入
    try:
        import funasr  # noqa: F401
        return True
    except ImportError:
        pass
    # 系统 Python 能导入
    return _find_python_with_funasr() is not None


def is_first_run() -> bool:
    """True if the installer hasn't completed successfully yet."""
    return _read_env_key("SETUP_COMPLETE") != "true"


def _scaffold_env() -> bool:
    """Create ~/.config/watch/.env with placeholders if missing."""
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return True


def _write_setup_complete() -> None:
    """Idempotently append SETUP_COMPLETE=true to .env.

    Used only after a fully successful install (deps + key). Future sessions
    detect this marker to skip wizard-style UI and stay silent.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = ""
    if CONFIG_FILE.exists():
        existing = CONFIG_FILE.read_text(encoding="utf-8")
        for line in existing.splitlines():
            if line.strip().startswith("SETUP_COMPLETE="):
                return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        CONFIG_FILE.write_text(existing + "SETUP_COMPLETE=true\n", encoding="utf-8")
    else:
        CONFIG_FILE.write_text(ENV_TEMPLATE + "\nSETUP_COMPLETE=true\n", encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _brew_pkg(missing: list[str]) -> list[str]:
    pkgs: list[str] = []
    for bin_name in missing:
        if bin_name in ("ffmpeg", "ffprobe"):
            if "ffmpeg" not in pkgs:
                pkgs.append("ffmpeg")
        elif bin_name == "yt-dlp":
            if "yt-dlp" not in pkgs:
                pkgs.append("yt-dlp")
        else:
            pkgs.append(bin_name)
    return pkgs


def _install_macos(missing: list[str]) -> tuple[bool, str]:
    if _which("brew") is None:
        return False, (
            "Homebrew is not installed. Install it from https://brew.sh, then re-run setup. "
            "Or install manually: `brew install " + " ".join(_brew_pkg(missing)) + "`"
        )
    pkgs = _brew_pkg(missing)
    if not pkgs:
        return True, "nothing to install"
    cmd = ["brew", "install", *pkgs]
    print(f"[setup] running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return False, f"brew install failed with exit code {result.returncode}"
    return True, f"installed via brew: {', '.join(pkgs)}"


def _install_hint_linux(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("apt: `sudo apt install ffmpeg` or dnf: `sudo dnf install ffmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("`pipx install yt-dlp` (recommended) or `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


def _install_hint_windows(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("winget: `winget install Gyan.FFmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("winget: `winget install yt-dlp.yt-dlp` or pip: `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


def _status() -> dict:
    """Structured preflight snapshot.

    检查二进制工具和 FunASR 是否就绪。FunASR 是本地转写引擎，
    替代了之前的 Whisper API，无需 API key。

    watch_python 字段返回含有 FunASR 的系统 Python 路径，
    供 SKILL.md 调用脚本时使用，绕过 Trae 自带的 Python 3.10。
    """
    missing = _check_binaries()
    # 优先查找系统 Python（含 FunASR），避免在 Trae Python 中误判
    watch_python = _find_python_with_funasr()
    has_funasr = watch_python is not None
    setup_complete = not is_first_run()

    if not missing and has_funasr:
        status = "ready"
    elif missing and not has_funasr:
        status = "needs_install_and_funasr"
    elif missing:
        status = "needs_install"
    else:
        status = "needs_funasr"

    # 只要二进制齐全且已完成 setup，就可以运行（FunASR 缺失只是无法转写）
    can_proceed = (not missing) and (has_funasr or setup_complete)

    cfg = get_config()
    return {
        "status": status,
        "can_proceed": can_proceed,
        "first_run": not setup_complete,
        "setup_complete": setup_complete,
        "missing_binaries": missing,
        "has_funasr": has_funasr,
        "watch_python": watch_python,
        "config_file": str(CONFIG_FILE),
        "watch_detail": cfg["detail"],
        "platform": platform.system(),
    }


def cmd_check() -> int:
    """Silent-on-success preflight.

    Exit 0 with no output when /watch can run. FunASR 缺失不阻止运行，
    但无字幕视频将无法转写。

    退出码：
      0 → 就绪
      2 → 二进制工具缺失
      3 → 首次运行且 FunASR 未安装
      4 → 二进制和 FunASR 都缺失
    """
    s = _status()
    if s["can_proceed"]:
        return 0

    parts = []
    if s["missing_binaries"]:
        parts.append(f"missing binaries: {', '.join(s['missing_binaries'])}")
    if not s["has_funasr"] and not s["setup_complete"]:
        parts.append("FunASR not installed (pip install funasr)")
    installer = Path(__file__).resolve()
    sys.stderr.write(
        f"[watch] setup incomplete ({'; '.join(parts)}). "
        f"Run: py {installer}\n"
    )
    sys.stderr.flush()

    if s["missing_binaries"] and not s["has_funasr"]:
        return 4
    if s["missing_binaries"]:
        return 2
    return 3


def cmd_json() -> int:
    json.dump(_status(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_install() -> int:
    """安装向导：检查依赖、scaffold .env、检测系统 Python。

    在 Windows 上，Trae 自带的 Python 3.10 缺少 FunASR。本函数会扫描
    系统 Python 安装路径，找到含 FunASR 的 Python 并写入 .env 的
    WATCH_PYTHON 变量，供 SKILL.md 调用脚本时使用。
    """
    missing = _check_binaries()
    if missing:
        system = platform.system()
        if system == "Darwin":
            ok, msg = _install_macos(missing)
            print(f"[setup] {msg}", file=sys.stderr)
            if not ok:
                return 2
            still_missing = _check_binaries()
            if still_missing:
                print(f"[setup] still missing after install: {', '.join(still_missing)}", file=sys.stderr)
                return 2
        elif system == "Linux":
            print("[setup] dependencies missing on Linux — please install:", file=sys.stderr)
            print("  " + _install_hint_linux(missing), file=sys.stderr)
            return 2
        elif system == "Windows":
            print("[setup] dependencies missing on Windows — please install:", file=sys.stderr)
            print("  " + _install_hint_windows(missing), file=sys.stderr)
            return 2
        else:
            print(f"[setup] unsupported platform ({system}) for auto-install. Install manually:", file=sys.stderr)
            print(f"  missing: {', '.join(missing)}", file=sys.stderr)
            return 2

    created = _scaffold_env()
    if created:
        print(f"[setup] created config: {CONFIG_FILE}")
    else:
        print(f"[setup] config exists: {CONFIG_FILE}")

    # 检测含 FunASR 的系统 Python，写入 .env 的 WATCH_PYTHON
    system_python = _find_python_with_funasr()
    if system_python:
        _write_watch_python(system_python)
        print(f"[setup] system python (with funasr): {system_python}", file=sys.stderr)

    has_funasr = _have_funasr()
    if has_funasr:
        _write_setup_complete()
        print("[setup] ready. transcription backend: funasr (local)")
        return 0

    print("")
    print("[setup] one step left: install FunASR for local transcription.")
    print("")
    print("  pip install funasr torch")
    print("")
    print("  GPU 加速（可选，需 NVIDIA 显卡）:")
    print("  pip install torch --index-url https://download.pytorch.org/whl/cu121")
    print("")
    print("  无需 API key，完全本地运行。未安装时无字幕视频将只返回关键帧。")
    return 3


def main() -> int:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--check":
            return cmd_check()
        if arg == "--json":
            return cmd_json()
    return cmd_install()


if __name__ == "__main__":
    raise SystemExit(main())

