import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import batch_notice


class BatchNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.binary, self.data = root / "cc-connect", root / "data"
        self.binary.write_text("not executable; subprocess is mocked")
        self.data.mkdir()
        self.stack = []
        for name, value in (("CC_BINARY", self.binary), ("CC_DATA_DIR", self.data)):
            mocked = patch.object(batch_notice, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.state = {"last_batch_ids": ["1"], "items": [{"id": "1", "status": "saved", "note": "saved.md"}]}
        self.report = {"batch_finished": True, "message": "已完成 10 条，剩余 20 条。"}
        self.snapshots = []

    def persist(self):
        self.snapshots.append(copy.deepcopy(self.state))

    def test_only_explicit_current_session_is_used_and_notice_is_not_duplicated(self):
        with patch.dict(batch_notice.os.environ, {"CC_PROJECT": "current", "CC_SESSION_KEY": "wechat:current"}, clear=True), \
             patch.object(batch_notice.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as send:
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "sent")
            argv = send.call_args.args[0]
            self.assertEqual(argv, [str(self.binary), "send", "--project", "current", "--session",
                                    "wechat:current", "--data-dir", str(self.data), "--stdin"])
            self.assertEqual(send.call_args.kwargs["input"], self.report["message"] + "\n")
            self.assertEqual(self.snapshots[0]["batch_notice"]["status"], "attempting")
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "already_attempted")
            send.assert_called_once()

    def test_missing_or_partial_session_never_selects_another_active_session(self):
        with patch.object(batch_notice.subprocess, "run") as send:
            with patch.dict(batch_notice.os.environ, {}, clear=True):
                self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "not_cc_connect")
            with patch.dict(batch_notice.os.environ, {"CC_PROJECT": "current"}, clear=True):
                self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "failed")
            send.assert_not_called()
        self.assertEqual(self.snapshots, [])

    def test_unfinished_batch_does_not_send_completion(self):
        with patch.object(batch_notice.subprocess, "run") as send:
            self.assertEqual(batch_notice.notify(self.state, {"batch_finished": False}, self.persist)["status"],
                             "batch_unfinished")
            send.assert_not_called()

    def test_failed_delivery_preserves_saved_work_and_does_not_automatically_retry(self):
        with patch.dict(batch_notice.os.environ, {"CC_PROJECT": "current", "CC_SESSION_KEY": "wechat:current"}, clear=True), \
             patch.object(batch_notice.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as send:
            before = copy.deepcopy(self.state["items"])
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "failed")
            self.assertEqual(self.state["items"], before)
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "already_attempted")
            send.assert_called_once()

    def test_timeout_keeps_unknown_delivery_without_resending_or_changing_saved_work(self):
        with patch.dict(batch_notice.os.environ, {"CC_PROJECT": "current", "CC_SESSION_KEY": "wechat:current"}, clear=True), \
             patch.object(batch_notice.subprocess, "run", side_effect=subprocess.TimeoutExpired("send", 30)) as send:
            before = copy.deepcopy(self.state["items"])
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "delivery_unknown")
            self.assertEqual(self.state["items"], before)
            self.assertEqual(batch_notice.notify(self.state, self.report, self.persist)["status"], "already_attempted")
            send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
