import contextlib
from http.cookiejar import MozillaCookieJar
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/watch/scripts'))
import auto_cookies as cookies
import download

SOURCE = 'https://www.douyin.com/video/7671999742519348543'


def fixture(value='test-value'):
    return {'name': 'ttwid', 'value': value, 'domain': '.douyin.com', 'path': '/',
            'expires': time.time() + 3600, 'secure': True, 'httpOnly': True}


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.permissions = patch.object(cookies, 'protect')
        self.permissions.start()
        self.settings = patch.object(cookies, 'read_env_file', return_value={})
        self.settings.start()
        self.environment = patch.dict(os.environ, {
            'WATCH_AUTO_COOKIES': 'true', 'WATCH_COOKIE_FILE': '',
            'WATCH_BROWSER': '', 'WATCH_COOKIE_MAX_AGE': '21600'})
        self.environment.start()
        download._AUTO_REFRESHED.clear()
        download._BROWSER_RESOLVED.clear()

    def tearDown(self):
        self.environment.stop()
        self.settings.stop()
        self.permissions.stop()
        self.temp.cleanup()

    def test_url_normalization_and_domain_boundaries(self):
        selected = 'https://www.douyin.com/jingxuan/knowledge?modal_id=7671999742519348543'
        self.assertEqual(cookies.normalize_url(selected), SOURCE)
        for unrelated in ['https://douyin.com.evil.test/?modal_id=123', 'https://evildouyin.com/', 'file:///douyin.com']:
            self.assertFalse(cookies.is_douyin(unrelated))
            self.assertEqual(cookies.normalize_url(unrelated), unrelated)
        self.assertFalse(cookies.domain_matches('example.com', '.sub.example.com'))

    def test_export_scopes_cookies_and_preserves_httponly(self):
        path = self.root / 'cookies.txt'
        unrelated = dict(fixture(), domain='.example.com')
        expired = dict(fixture(), name='expired', expires=time.time() - 100)
        self.assertEqual(cookies.export_cookies([fixture(), unrelated, expired], path), 1)
        jar = MozillaCookieJar(str(path))
        jar.load(ignore_discard=True)
        item = next(iter(jar))
        self.assertEqual(item.name, 'ttwid')
        self.assertTrue(item.has_nonstandard_attr('HTTPOnly'))
        self.assertTrue(cookies.cookie_file_matches(path, SOURCE))
        self.assertFalse(cookies.cookie_file_matches(path, 'https://example.com/'))

    def test_cache_reuse_force_refresh_and_no_secret_logging(self):
        captured = io.StringIO()
        with patch.object(cookies, '_browser_cookies', return_value=([fixture('do-not-log-this')], 'Edge-Test', None)) as browser, contextlib.redirect_stderr(captured):
            first = cookies.acquire(SOURCE, cache_dir=self.root)
            self.assertFalse(first.cached)
            self.assertTrue(cookies.acquire(SOURCE, cache_dir=self.root).cached)
            self.assertEqual(browser.call_count, 1)
            cookies.acquire(SOURCE, force=True, cache_dir=self.root)
            self.assertEqual(browser.call_count, 2)
        self.assertNotIn('do-not-log-this', captured.getvalue())
        self.assertNotIn('do-not-log-this', (self.root / 'douyin.json').read_text())

    def test_expired_cache_is_replaced(self):
        with patch.object(cookies, '_browser_cookies', return_value=([fixture()], 'test-UA', None)) as browser:
            cookies.acquire(SOURCE, cache_dir=self.root)
            path = self.root / 'douyin.json'
            metadata = json.loads(path.read_text())
            metadata['created'] = time.time() - 90000
            path.write_text(json.dumps(metadata))
            self.assertFalse(cookies.acquire(SOURCE, cache_dir=self.root).cached)
            self.assertEqual(browser.call_count, 2)

    def test_explicit_empty_and_disable_settings_are_respected(self):
        with patch.dict(os.environ, {'WATCH_BROWSER': '', 'WATCH_AUTO_COOKIES': 'false'}):
            self.assertEqual(cookies.setting('WATCH_BROWSER', 'edge'), '')
            self.assertFalse(cookies.enabled(SOURCE))
            self.assertEqual(download._cookie_args(SOURCE), [])

    def test_auth_failure_refreshes_only_once(self):
        command = ['yt-dlp', '--cookies', 'old.txt', '--user-agent', 'old-UA', '--', SOURCE]
        failed = subprocess.CompletedProcess(command, 1, '', 'HTTP Error 403: Forbidden')
        succeeded = subprocess.CompletedProcess(command, 0, '', '')
        download._AUTO_REFRESHED.clear()
        with patch.object(download, 'enabled', return_value=True), patch.object(download, '_cookie_args', return_value=['--cookies', 'new.txt']) as refreshed, patch.object(download, '_run_browser_resolved', return_value=failed), patch.object(download.subprocess, 'run', side_effect=[failed, succeeded, failed]) as runner, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(download._run_ytdlp(command, SOURCE).returncode, 0)
            second = runner.call_args_list[1].args[0]
            self.assertNotIn('old.txt', second)
            self.assertNotIn('old-UA', second)
            self.assertEqual(second[-2:], ['--', SOURCE])
            download._run_ytdlp(command, SOURCE)
            self.assertEqual(refreshed.call_count, 1)
            self.assertEqual(runner.call_count, 3)

    def test_unrelated_failures_do_not_refresh_cookies(self):
        failed = subprocess.CompletedProcess([], 1, '', 'Video is unavailable')
        download._AUTO_REFRESHED.clear()
        with patch.object(download.subprocess, 'run', return_value=failed) as runner, patch.object(download, '_cookie_args') as refreshed, contextlib.redirect_stderr(io.StringIO()):
            download._run_ytdlp(['yt-dlp', '--', SOURCE], SOURCE)
            self.assertEqual(runner.call_count, 1)
            refreshed.assert_not_called()

    def test_media_capture_rejects_other_video_and_non_http_urls(self):
        detail = {'aweme_id': '7671999742519348543', 'desc': 'test video', 'video': {
            'duration': 10000, 'height': 720, 'width': 1280,
            'play_addr': {'url_list': ['https://media.example.test/video.mp4', 'file:///secret', 'javascript:alert(1)']}}}
        info = cookies.media_info(detail, SOURCE, 'test-UA')
        self.assertEqual(info['duration'], 10)
        self.assertEqual(len(info['formats']), 1)
        self.assertIsNone(cookies.media_info(dict(detail, aweme_id='123'), SOURCE, 'test-UA'))

    def test_media_capture_handles_nested_bitrate_variants(self):
        detail = {'aweme_id': cookies.video_id(SOURCE), 'video': {'duration': 1000,
                  'play_addr': {'url_list': ['https://media.example.test/main.mp4']},
                  'bit_rate': [{'bit_rate': 600000, 'play_addr': {'height': 480, 'width': 854,
                               'url_list': ['https://media.example.test/low.mp4']}}]}}
        info = cookies.media_info(detail, SOURCE, 'test-UA')
        self.assertEqual(info['formats'][0]['tbr'], 0)
        self.assertEqual(info['formats'][1]['tbr'], 600)
        self.assertEqual(info['formats'][1]['height'], 480)

    def test_browser_resolution_uses_cached_video_only_for_matching_id(self):
        expected = cookies.video_id(SOURCE)
        path = self.root / f'video-{expected}.json'
        path.write_text(json.dumps({'created': time.time(), 'info': {'id': expected, 'title': 'cached'}}))
        with patch.object(cookies, 'acquire') as browser:
            result = cookies.resolve_video(SOURCE, cache_dir=self.root)
            self.assertEqual(json.loads(result.read_text())['id'], expected)
            browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
