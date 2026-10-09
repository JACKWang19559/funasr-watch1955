"""Verify the installed skill has its browser helper and linked references."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class CodexInstallTests(unittest.TestCase):
    def test_install_includes_browser_reference_and_working_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.touch()
            (root / "ffprobe.exe").touch()
            target = root / "watch"
            installed = subprocess.run([
                sys.executable, str(ROOT / "codex-adapter/install_codex.py"),
                "--python", sys.executable, "--ffmpeg", str(ffmpeg),
                "--dest", str(target), "--model-cache", str(root / "models"),
            ], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue((target / "scripts/youtube_browser.py").is_file())
            self.assertTrue((target / "references/youtube-browser.md").is_file())
            runtime = json.loads((target / "scripts/runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(runtime["python"]), Path(sys.executable).absolute())
            launcher = target / "scripts/codex_watch.py"
            result = subprocess.run([sys.executable, str(launcher), "--help"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--via-browser", result.stdout)
            result = subprocess.run([sys.executable, str(launcher), "--browser-login", "--help"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--profile", result.stdout)

    def test_launcher_selects_ffprobe_with_matching_executable_suffix(self):
        spec = importlib.util.spec_from_file_location("watch_launcher_test", ROOT / "codex-adapter/codex_watch.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "codex_watch.py"
            for name, probe in (("ffmpeg.exe", "ffprobe.exe"), ("ffmpeg", "ffprobe")):
                with self.subTest(ffmpeg=name):
                    (root / "runtime.json").write_text(json.dumps({
                        "python": sys.executable, "ffmpeg": str(root / name), "model_cache": str(root / "models"),
                    }), encoding="utf-8")
                    with patch.object(launcher, "__file__", str(script)), \
                            patch.object(sys, "argv", [str(script), "--help"]), \
                            patch.object(launcher.subprocess, "call", return_value=0) as run:
                        self.assertEqual(launcher.main(), 0)
                    self.assertEqual(run.call_args.kwargs["env"]["FFPROBE_PATH"], str(root / probe))


if __name__ == "__main__":
    unittest.main()
