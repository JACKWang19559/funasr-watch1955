"""Offline regressions for capture completeness, reuse and ASR time ranges."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/watch/scripts"))
import funasr_transcribe as asr
from transcribe import filter_range, format_transcript
from youtube_browser import BrowserCaptureError, _cached, covers_video, youtube_id


class WatchFallbackTests(unittest.TestCase):
    def test_capture_must_cover_actual_video_without_gaps(self):
        self.assertTrue(covers_video([[30, 90], [0, 60], [60, 120]], 120))
        self.assertFalse(covers_video([[0, 30]], 960))
        self.assertFalse(covers_video([[0, 30], [90, 120]], 120))
        self.assertFalse(covers_video([[1, 120]], 120))
        self.assertFalse(covers_video([[0, 120]], float("inf")))

    def test_video_identity_and_host(self):
        for url in ("https://www.youtube.com/watch?v=1hhfwBR_6lg&t=45",
                    "https://youtu.be/1hhfwBR_6lg", "https://m.youtube.com/shorts/1hhfwBR_6lg"):
            self.assertEqual(youtube_id(url), "1hhfwBR_6lg")
        for url in ("https://youtube.com.example.org/watch?v=1hhfwBR_6lg",
                    "https://www.youtube.com/results?search_query=apps", "file:///video.mp4"):
            with self.assertRaises(BrowserCaptureError):
                youtube_id(url)

    def test_cache_rejects_incomplete_modified_or_wrong_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            audio = root / "audio.webm"
            audio.write_bytes(b"previously validated audio")
            record = {"complete": True, "info": {"id": "1hhfwBR_6lg"},
                      "video_path": str(audio), "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}
            manifest = root / "capture.json"
            manifest.write_text(json.dumps(record), encoding="utf-8")
            self.assertIsNotNone(_cached(root, "1hhfwBR_6lg", 0))
            self.assertIsNone(_cached(root, "abcdefghijk", 0))
            self.assertIsNone(_cached(root, "1hhfwBR_6lg", 3))  # No saved frames.
            audio.write_bytes(b"damaged audio")
            self.assertIsNone(_cached(root, "1hhfwBR_6lg", 0))
            record["complete"] = False
            manifest.write_text(json.dumps(record), encoding="utf-8")
            self.assertIsNone(_cached(root, "1hhfwBR_6lg", 0))

    def test_asr_without_native_timing_keeps_source_window_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "input.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x01\x00" * 16000 * 131)
            lengths = []

            class Model:
                def generate(self, input, **kwargs):
                    with wave.open(input, "rb") as stream:
                        lengths.append(stream.getnframes() / stream.getframerate())
                    return [{"text": f"<|zh|>section {len(lengths)}<|Speech|>"}]

            with patch.object(asr, "_get_model", return_value=Model()), \
                    patch.object(asr, "extract_audio", return_value=audio):
                segments, backend = asr.transcribe_video(str(audio), root / "output.wav")
            self.assertEqual(lengths, [60, 60, 11])
            self.assertEqual(backend, "funasr")
            self.assertEqual([(s["start"], s["end"]) for s in segments], [(0, 60), (60, 120), (120, 131)])
            selected = filter_range(segments, 65, 70)
            self.assertEqual(len(selected), 1)
            self.assertIn("[01:00–02:00] section 2", format_transcript(selected))

    def test_cached_frames_exclude_the_player_end_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            audio = root / "audio.webm"
            audio.write_bytes(b"validated audio")
            frames = []
            for index, timestamp in enumerate((0.1, 60, 120)):
                frame = root / f"frame-{index}.jpg"
                frame.write_bytes(b"frame")
                frames.append({"path": str(frame), "timestamp_seconds": timestamp})
            record = {"complete": True, "info": {"id": "1hhfwBR_6lg", "duration": 120},
                      "video_path": str(audio), "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                      "browser_frames": frames, "frame_resolution": 512}
            (root / "capture.json").write_text(json.dumps(record), encoding="utf-8")
            cached = _cached(root, "1hhfwBR_6lg", 3)
            self.assertEqual([f["timestamp_seconds"] for f in cached["browser_frames"]], [0.1, 60])
            self.assertIsNotNone(_cached(root, "1hhfwBR_6lg", 3, 512))
            self.assertIsNone(_cached(root, "1hhfwBR_6lg", 3, 1024))

    def test_native_sentence_times_keep_the_source_chunk_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "input.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x01\x00" * 16000 * 61)

            class Model:
                def generate(self, **kwargs):
                    return [{"sentence_info": [{"text": "native timing", "start": 100, "end": 300}]}]

            with patch.object(asr, "_get_model", return_value=Model()), \
                    patch.object(asr, "extract_audio", return_value=audio):
                segments, _ = asr.transcribe_video(str(audio), root / "output.wav")
            self.assertEqual([(s["start"], s["end"]) for s in segments], [(0.1, 0.3), (60.1, 60.3)])
            self.assertTrue(all("timestamp_kind" not in s for s in segments))


if __name__ == "__main__":
    unittest.main()
