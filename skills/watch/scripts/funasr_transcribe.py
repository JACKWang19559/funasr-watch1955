#!/usr/bin/env python3
"""FunASR 本地语音转写模块，替代 Whisper API。

使用阿里达摩院 FunASR + SenseVoiceSmall 模型实现本地语音转写，
无需 API key，完全离线运行（首次运行需下载模型）。

SenseVoiceSmall 特点：
- 模型权重约 0.9GB（随版本变化）
- 中文识别效果最佳（CER 7.81%）
- CPU 上 17x 实时，GPU 上更快
- 支持标点恢复和时间戳输出

用法：
    from funasr_transcribe import transcribe_video
    segments, backend = transcribe_video(video_path, audio_out_path)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 优先使用完整版 ffmpeg（绕过 Trae 自带的精简版）
from ffmpeg_utils import find_ffmpeg, find_ffprobe


# SenseVoiceSmall 模型 ID（从 ModelScope 自动下载）
MODEL_ID = os.environ.get("WATCH_MODEL_DIR") or "iic/SenseVoiceSmall"
VAD_MODEL_ID = os.environ.get("WATCH_VAD_MODEL_DIR") or "fsmn-vad"

# 单例模型实例（避免重复加载）
_model = None


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        from config import read_env_file
        value = read_env_file().get(name, default)
    return value.strip().lower() or default


def _get_device() -> str:
    """Resolve ASR placement; auto prefers CUDA with a CPU fallback."""
    device = _setting("WATCH_TRANSCRIBE_DEVICE", "auto")
    if device == "auto":
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"
    return device


def _get_vad_device(asr_device: str) -> str:
    """Default to CPU segmentation; auto explicitly follows the ASR device."""
    device = _setting("WATCH_VAD_DEVICE", "cpu")
    return asr_device if device == "auto" else device


def _get_model():
    """Load SenseVoiceSmall and FSMN-VAD once, on independent devices."""
    global _model
    if _model is not None:
        return _model
    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise SystemExit(f"FunASR is not installed. Install with: pip install funasr (import error: {exc})")

    device = _get_device()
    vad_device = _get_vad_device(device)
    print(f"[watch] loading FunASR model (ASR={device}, VAD={vad_device})…", file=sys.stderr)

    def build(asr: str, vad: str):
        return AutoModel(
            model=MODEL_ID,
            vad_model=VAD_MODEL_ID,
            device=asr,
            vad_kwargs={"device": vad},
            disable_update=True,
            punc_model=None,
            spk_model=None,
        )

    try:
        _model = build(device, vad_device)
    except Exception as exc:
        if device.startswith("cuda") or vad_device.startswith("cuda"):
            print(f"[watch] GPU model load failed ({exc}), falling back to CPU…", file=sys.stderr)
            try:
                _model = build("cpu", "cpu")
            except Exception as fallback_exc:
                raise SystemExit(f"Failed to load FunASR model on CPU: {fallback_exc}") from fallback_exc
            device = vad_device = "cpu"
        else:
            raise SystemExit(f"Failed to load FunASR model: {exc}") from exc
    print(f"[watch] FunASR model loaded (ASR={device}, VAD={vad_device})", file=sys.stderr)
    return _model


def extract_audio(video_path: str, out_path: Path) -> Path:
    """从视频提取音频（mono 16kHz wav，FunASR 要求的格式）。

    与 whisper.py 的 extract_audio 不同，这里输出 wav 格式而非 mp3，
    因为 FunASR 原生支持 wav，且无需考虑 API 上传大小限制。

    Args:
        video_path: 视频文件路径
        out_path: 音频输出路径

    Returns:
        音频文件路径

    Raises:
        SystemExit: 如果 ffmpeg 未安装或提取失败
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise SystemExit(
            "ffmpeg is not installed. Install with: winget install Gyan.FFmpeg"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        # Windows 反斜杠路径在新版 ffmpeg（≥7.0）上会触发 muxer 初始化 bug，
        # 转成正斜杠绕过
        "-i", str(Path(video_path).resolve()).replace("\\", "/"),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(out_path.resolve()).replace("\\", "/"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"ffmpeg audio extraction failed: {result.stderr.strip()}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit(
            "ffmpeg produced no audio — video may have no audio track"
        )
    return out_path


def _clean_text(text: str) -> str:
    """过滤 SenseVoiceSmall 输出中的特殊标签和多余空白。

    SenseVoiceSmall 模型会在文本中插入以下标签：
    - 语言标签：<|zh|>、<|en|>、<|ja|> 等
    - 情绪标签：<|HAPPY|>、<|NEUTRAL|>、<|SAD|>、<|EMO_UNKNOWN|> 等
    - 事件标签：<|BGM|>、<|Speech|> 等
    - ASR 后缀标签：<|woitn|> 等

    这些标签对用户无用，需要过滤掉。

    Args:
        text: 原始文本

    Returns:
        清理后的纯文本
    """
    # 匹配所有 <|...|> 格式的标签
    cleaned = re.sub(r"<\|[^|]+\|>", "", text)
    # 合并多余空白（连续空格变单个，去除首尾空白）
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _parse_segments(result: list) -> list[dict]:
    """解析 FunASR 输出为标准 segments 格式。

    FunASR 返回的结构包含 sentence_info（带时间戳的句子级分段），
    转换为 {"start": float, "end": float, "text": str} 格式。
    文本会经过 _clean_text 过滤特殊标签。

    Args:
        result: FunASR 推理结果列表

    Returns:
        segments 列表，每段包含 start/end/text
    """
    segments: list[dict] = []

    for item in result:
        # 优先使用 sentence_info（带时间戳的句子级分段）
        sentence_info = item.get("sentence_info", [])
        if sentence_info:
            for sent in sentence_info:
                text = _clean_text(sent.get("text") or "")
                if not text:
                    continue
                # 时间戳是毫秒，转为秒
                start = float(sent.get("start", 0)) / 1000.0
                end = float(sent.get("end", 0)) / 1000.0
                segments.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "text": text,
                })
        else:
            # 回退：使用完整文本（无时间戳）
            text = _clean_text(item.get("text") or "")
            if text:
                segments.append({"start": 0.0, "end": 0.0, "text": text})

    return segments


def transcribe_video(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], str]:
    """用 FunASR 本地转写视频音频，返回带时间戳的 segments。

    接口与 whisper.py 的 transcribe_video 兼容，便于无缝替换。
    backend 和 api_key 参数仅为兼容性保留，实际不使用。

    Args:
        video_path: 视频文件路径
        audio_out: 音频提取的输出路径（wav 格式）
        backend: 忽略（仅为接口兼容）
        api_key: 忽略（仅为接口兼容）

    Returns:
        (segments, backend_name) 元组
        segments: [{"start": float, "end": float, "text": str}, ...]
        backend_name: "funasr"

    Raises:
        SystemExit: 如果转写失败
    """
    print("[watch] extracting audio for FunASR…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    audio_bytes = audio_path.stat().st_size
    print(
        f"[watch] audio: {audio_bytes / 1024:.0f} kB — transcribing with FunASR…",
        file=sys.stderr,
    )

    # 加载模型（首次运行会下载）
    model = _get_model()

    # 执行转写
    try:
        result = model.generate(
            input=str(audio_path),
            # 启用时间戳输出
            use_timestamp=True,
            # 批量大小（GPU 可设大些，CPU 设小些）
            batch_size_s=300,
        )
    except Exception as exc:
        raise SystemExit(f"FunASR transcription failed: {exc}")

    # 解析结果
    segments = _parse_segments(result)

    if not segments:
        raise SystemExit("FunASR returned no transcript segments")

    print(
        f"[watch] transcribed {len(segments)} segments via funasr",
        file=sys.stderr,
    )
    return segments, "funasr"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: funasr_transcribe.py <video-path> [<audio-out.wav>]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("audio.wav")

    segments, backend = transcribe_video(video, audio_out)
    print(json.dumps({"backend": backend, "segments": segments}, indent=2))
