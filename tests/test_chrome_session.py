import argparse
from contextlib import redirect_stdout
from http.cookiejar import Cookie, CookieJar
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import chrome_session
import dy_author
import pipeline


def cookies(*, domain=".douyin.com", expires=None):
    jar = CookieJar()
    jar.set_cookie(Cookie(0, "sessionid", "PRIVATE_SESSION_VALUE", None, False,
                          domain, True, True, "/", True, False, expires, False,
                          None, None, {}, False))
    return jar


class ChromeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.platform = patch.object(chrome_session.sys, "platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.root, self.cache = chrome_session.chrome_roots(self.home)
        self.root.mkdir(parents=True)

    def profile(self, name):
        result = self.root / name
        result.mkdir()
        return result.resolve()

    def test_os_home_is_used_when_zcode_replaces_home(self):
        profile = self.profile("Profile 2")
        (self.root / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 2"}}))
        with patch.dict(os.environ, {"HOME": str(self.home / "zcode-home")}), \
             patch("pwd.getpwuid", return_value=SimpleNamespace(pw_dir=str(self.home))):
            self.assertEqual(chrome_session.profile_candidates(), [profile])

    def test_auto_selection_reuses_session_and_matching_cache_without_windows(self):
        first, logged_in = self.profile("Default"), self.profile("Profile 1")
        (self.root / "Local State").write_text(json.dumps({"profile": {
            "last_used": "Default", "info_cache": {"Default": {}, "Profile 1": {}}}}))
        reader = Mock(side_effect=lambda browser, profile: cookies() if profile == str(logged_in) else CookieJar())
        with patch.object(pipeline.douyin_access.subprocess, "Popen") as popen:
            config, status = chrome_session.resolve_config({"browser": "chrome"}, cookie_reader=reader, home=self.home)
        self.assertEqual(config["browser_profile"], str(logged_in))
        self.assertEqual(config["browser_cache_dir"], str(self.cache / "Profile 1/Cache/Cache_Data"))
        self.assertFalse(config["auto_browser_recovery"])
        self.assertEqual(status["status"], "available")
        self.assertFalse(status["server_validated"])
        self.assertEqual(reader.call_args_list[0].args, ("chrome", str(first)))
        self.assertNotIn("PRIVATE_SESSION_VALUE", json.dumps(status))
        popen.assert_not_called()

    def test_missing_expired_or_unrelated_session_requests_manual_login(self):
        self.profile("Default")
        for jar in (CookieJar(), cookies(expires=int(time.time()) - 10), cookies(domain=".notdouyin.com")):
            with self.subTest(jar=type(jar).__name__):
                _, status = chrome_session.resolve_config({"browser": "chrome"},
                                                          cookie_reader=lambda *args: jar, home=self.home)
                self.assertEqual(status["status"], "login_required")
                self.assertIn("手动", status["message"])

    def test_read_error_is_not_reported_as_logged_out_and_secrets_are_not_returned(self):
        self.profile("Default")
        _, status = chrome_session.resolve_config({"browser": "chrome"}, home=self.home,
            cookie_reader=Mock(side_effect=RuntimeError("PRIVATE_SESSION_VALUE")))
        self.assertEqual(status["status"], "read_error")
        self.assertIn("未据此判定", status["message"])
        self.assertNotIn("PRIVATE_SESSION_VALUE", json.dumps(status))

    def test_missing_profile_never_reads_cookies_or_opens_chrome(self):
        reader = Mock()
        with patch.object(pipeline.douyin_access.subprocess, "Popen") as popen:
            _, status = chrome_session.resolve_config({"browser": "chrome"}, cookie_reader=reader, home=self.home)
        self.assertEqual(status["status"], "profile_unavailable")
        reader.assert_not_called()
        popen.assert_not_called()

    def test_explicit_profile_is_preserved(self):
        self.profile("Default")
        selected = self.profile("Profile 1")
        config, status = chrome_session.resolve_config({"browser": "chrome", "browser_profile": str(selected)},
                                                       cookie_reader=lambda *args: cookies(), home=self.home)
        self.assertEqual(config["browser_profile"], str(selected))
        self.assertFalse(config["browser_profile_auto"])
        self.assertEqual(status["status"], "available")

    def test_media_detail_failure_does_not_open_a_window_by_default(self):
        item = {"id": "7676060613507943716", "url": "https://www.douyin.com/video/7676060613507943716"}
        config = {"browser": "chrome", "cache_dir": str(self.home)}
        failure = pipeline.image_posts.StageError("detail", "network failure", "DETAIL_UNAVAILABLE")
        with patch.object(dy_author, "call_pipeline", side_effect=failure), \
             patch.object(pipeline.douyin_access, "refresh_browser_session") as browser:
            with self.assertRaises(pipeline.image_posts.StageError):
                dy_author.prepare_item(self.home, {"items": [item]}, item, config)
        browser.assert_not_called()
        self.assertNotIn("browser_recovery", item)

    def test_current_silent_setting_overrides_old_snapshot_and_preserves_saved_items(self):
        vault = self.home / "vault"
        vault.mkdir()
        config = {"vault": str(vault), "cache_dir": str(self.home / "runs-cache"),
                  "browser": None, "auto_browser_recovery": True}
        created = dy_author.initialize(config)
        run = Path(created["run_dir"])
        (run / "input.txt").write_text("/dy-author https://www.douyin.com/video/7676060613507943716")
        state = pipeline.load_json(run / "state.json")
        state["items"] = [{"id": "7676060613507943716", "status": "saved", "note": "existing.md"}]
        dy_author.write_state(run, state)
        config["auto_browser_recovery"] = False
        config_file = self.home / "config.json"
        pipeline.write_json(config_file, config)
        with patch.object(sys, "argv", ["dy_author.py", "--config", str(config_file), "next", "--run", str(run)]), \
             patch.object(dy_author, "next_item", return_value={"status": "checked"}) as next_item, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(dy_author.main(), 0)
        self.assertFalse(next_item.call_args.args[2]["auto_browser_recovery"])
        self.assertEqual(pipeline.load_json(run / "state.json")["items"], state["items"])


if __name__ == "__main__":
    unittest.main()
