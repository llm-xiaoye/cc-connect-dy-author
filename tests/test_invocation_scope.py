import contextlib
import hashlib
import io
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import dy_author
import author_works
import pipeline
from test_dy_author import QueueBase, FIRST, SECOND, listing


SHARE = (
    "1.23 复制打开抖音，看看【赢泟老周（企业AI落地）的图文作品】"
    "我们落地制造业企业agent构架全景。很多工厂的现... "
    "https://v.douyin.com/cgZocvcgTeE/ 11/05 J@v.sE :8pm ZmQ:/"
)
SCOPE_ERROR = "AUTHOR_SCOPE_REQUIRES_EXPLICIT_COMMAND"


class InvocationScopeTests(QueueBase):
    def test_plain_share_and_links_are_rejected(self):
        for raw in (SHARE, "https://v.douyin.com/cgZocvcgTeE/",
                    f"https://www.douyin.com/note/{FIRST}", "补录"):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                dy_author.parse_input(raw)

    def test_explicit_commands_preserve_share_and_supplement(self):
        for command in ("/dy-author", "$dy-author"):
            with self.subTest(command=command):
                self.assertEqual(dy_author.parse_input(command + "\n" + SHARE), ("author", SHARE))
                self.assertEqual(dy_author.parse_input(command + " 补录"), ("supplement", None))

    def test_history_mentions_and_command_lookalikes_are_not_authorization(self):
        for raw in ("上次用了 /dy-author，今天只处理 " + SHARE,
                    "/dy-author-other " + SHARE,
                    "请解释 /dy-author " + SHARE):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                dy_author.parse_input(raw)

    def test_plain_share_fails_before_network_or_vault_mutation(self):
        run, state, config = self.create_run(SHARE)
        original = (run / "state.json").read_bytes()
        with patch.object(pipeline, "normalize_url") as resolve, \
             patch.object(author_works, "enumerate_work", new_callable=AsyncMock) as enumerate_author, \
             patch.object(dy_author.library, "write_extras") as write:
            with self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                dy_author.plan(run, state, config)
            resolve.assert_not_called()
            enumerate_author.assert_not_called()
            write.assert_not_called()
        self.assertEqual((run / "state.json").read_bytes(), original)
        self.assertFalse(state["planned"])

    def legacy_run(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", "t1"), (SECOND, "video", "t2")))
        (run / "input.txt").write_text(SHARE, encoding="utf-8")
        state["input_sha256"] = hashlib.sha256(SHARE.encode()).hexdigest()
        dy_author.write_state(run, state)
        return run, state, config

    def test_planned_legacy_run_cannot_bypass_scope_or_prepare(self):
        run, state, config = self.legacy_run()
        original = (run / "state.json").read_bytes()
        with patch.object(pipeline, "prepare") as prepare, \
             patch.object(dy_author, "prepare_parallel") as parallel:
            for prepare_all in (False, True):
                with self.subTest(prepare_all=prepare_all), self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                    dy_author.next_item(run, state, config, prepare_all=prepare_all, workers=2)
            with self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                dy_author.plan(run, state, config)
            prepare.assert_not_called()
            parallel.assert_not_called()
        self.assertEqual((run / "state.json").read_bytes(), original)

    def test_worker_cannot_bypass_parent_scope_and_wrong_checkpoint_is_rejected(self):
        run, state, config = self.legacy_run()
        with patch.object(pipeline, "prepare") as prepare:
            with self.assertRaisesRegex(ValueError, SCOPE_ERROR):
                dy_author.prepare_worker(str(run), state, state["items"][0], config)
            prepare.assert_not_called()
        valid_run, valid_state, _, _ = self.plan(listing((FIRST, "video", "t1")))
        with self.assertRaisesRegex(ValueError, "AUTHOR_RUN_SCOPE_MISMATCH"):
            dy_author.checked_input(valid_run / ".workers" / SECOND, valid_state)

    def test_changed_explicit_input_cannot_reuse_old_queue(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", "t1")))
        (run / "input.txt").write_text(f"/dy-author https://www.douyin.com/video/{SECOND}", encoding="utf-8")
        for action in (dy_author.plan, dy_author.next_item):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(ValueError, "AUTHOR_INPUT_CHANGED"):
                action(run, state, config)

    def test_scope_block_preserves_saved_notes_and_allows_completed_draft_commit(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", "t1"), (SECOND, "video", "t2")))
        with self.patched():
            ready = dy_author.next_item(run, state, config, prepare_all=True)
        first = next(item for item in ready["ready"] if item["id"] == FIRST)
        self.write_note(first)
        (run / "input.txt").write_text(SHARE, encoding="utf-8")
        self.assertEqual(dy_author.commit(run, state, config, FIRST)["status"], "saved")
        note = Path(dy_author.existing(FIRST, config))
        original_note = note.read_bytes()
        with self.assertRaisesRegex(ValueError, SCOPE_ERROR):
            dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(note.read_bytes(), original_note)
        self.assertEqual(dy_author.report(state)["counts"]["saved"], 1)
        self.assertTrue(Path(first["material_file"]).is_file())

    def test_legacy_progress_cannot_send_batch_notice(self):
        run, state, config = self.legacy_run()
        config_file = self.root / "cli-config.json"
        pipeline.write_json(config_file, config)
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["dy_author.py", "--config", str(config_file),
                                       "progress", "--run", str(run), "--notify"]), \
             patch("batch_notice.notify") as notify, contextlib.redirect_stderr(stderr), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dy_author.main(), 1)
            notify.assert_not_called()
        self.assertIn(SCOPE_ERROR, stderr.getvalue())

    def test_legacy_catalog_cli_cannot_start_more_analysis(self):
        run, state, config = self.legacy_run()
        config_file = self.root / "cli-config.json"
        pipeline.write_json(config_file, config)
        with patch.object(sys, "argv", ["dy_author.py", "--config", str(config_file),
                                       "catalog-search", "--run", str(run), "--id", FIRST]), \
             patch.object(dy_author, "catalog_search") as search, \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dy_author.main(), 1)
            search.assert_not_called()
