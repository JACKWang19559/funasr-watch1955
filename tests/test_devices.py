import contextlib
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/watch/scripts"))
import config
import funasr_transcribe as ft


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.settings = patch.object(config, "read_env_file", return_value={})
        self.settings.start()
        self.singleton = patch.object(ft, "_model", None)
        self.singleton.start()
        self.cuda = Mock(return_value=True)
        self.torch = patch.dict(sys.modules, {"torch": SimpleNamespace(cuda=SimpleNamespace(is_available=self.cuda))})
        self.torch.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.settings.stop)
        self.addCleanup(self.singleton.stop)
        self.addCleanup(self.torch.stop)

    def test_defaults_choose_cuda_asr_and_cpu_vad(self):
        self.assertEqual(ft._get_device(), "cuda")
        self.assertEqual(ft._get_vad_device("cuda"), "cpu")
        builder = Mock(return_value=object())
        with patch.dict(sys.modules, {"funasr": SimpleNamespace(AutoModel=builder)}), contextlib.redirect_stderr(io.StringIO()):
            first = ft._get_model()
            self.assertIs(ft._get_model(), first)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(builder.call_args.kwargs["device"], "cuda")
        self.assertEqual(builder.call_args.kwargs["vad_kwargs"], {"device": "cpu"})

    def test_auto_without_cuda_uses_cpu(self):
        self.cuda.return_value = False
        self.assertEqual(ft._get_device(), "cpu")
        self.assertEqual(ft._get_vad_device("cpu"), "cpu")

    def test_explicit_vad_auto_follows_asr_and_env_overrides_file(self):
        self.settings.return_value = {"WATCH_TRANSCRIBE_DEVICE": "cpu", "WATCH_VAD_DEVICE": "cuda"}
        os.environ["WATCH_TRANSCRIBE_DEVICE"] = "cuda:0"
        os.environ["WATCH_VAD_DEVICE"] = "auto"
        self.assertEqual(ft._get_device(), "cuda:0")
        self.assertEqual(ft._get_vad_device("cuda:0"), "cuda:0")
        self.assertEqual(ft._get_vad_device("cpu"), "cpu")

    def test_cuda_load_failure_falls_back_to_both_cpu(self):
        os.environ["WATCH_TRANSCRIBE_DEVICE"] = "cuda:0"
        builder = Mock(side_effect=[RuntimeError("test GPU load failure"), object()])
        with patch.dict(sys.modules, {"funasr": SimpleNamespace(AutoModel=builder)}), contextlib.redirect_stderr(io.StringIO()):
            ft._get_model()
        self.assertEqual(builder.call_args_list[1].kwargs["device"], "cpu")
        self.assertEqual(builder.call_args_list[1].kwargs["vad_kwargs"], {"device": "cpu"})

    def test_cpu_failure_is_reported_without_retry(self):
        os.environ["WATCH_TRANSCRIBE_DEVICE"] = "cpu"
        builder = Mock(side_effect=RuntimeError("test CPU load failure"))
        with patch.dict(sys.modules, {"funasr": SimpleNamespace(AutoModel=builder)}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "Failed to load FunASR model"):
                ft._get_model()
        self.assertEqual(builder.call_count, 1)


if __name__ == "__main__":
    unittest.main()
