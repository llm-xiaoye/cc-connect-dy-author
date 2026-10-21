from pathlib import Path
import unittest
from unittest.mock import patch

import helpers
from helpers import AUTHOR, question_body
import author_docs
import backfill_authors
import dy_author
import library
import pipeline
import question_bank
import unified_store

WITH_RAW = "7659111274168454401"
BODY_ONLY = "7659111274168454402"
PLAIN = "7659111274168454403"
COVERED = "7659111274168454404"


class SupplementTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.shared = self.root / "project-cache"
        self.config = helpers.make_config(self.root, self.vault, material_cache_dirs=[str(self.shared)])
        self.notes = {
            WITH_RAW: helpers.existing_note(self.vault, WITH_RAW, "知识库/AI/x/有原始材料.md"),
            BODY_ONLY: helpers.existing_note(self.vault, BODY_ONLY, "知识库/AI/x/只有正文.md"),
            PLAIN: helpers.existing_note(self.vault, PLAIN, "知识库/AI/x/普通.md"),
            COVERED: helpers.existing_note(self.vault, COVERED, "知识库/AI/x/已收录.md"),
        }
        helpers.video_bundle(self.shared, WITH_RAW, text="这是面试高频题的讲解。")
        helpers.video_bundle(self.shared, PLAIN, text="普通讲解，没有那两个字。")
        body_note = self.notes[BODY_ONLY]
        body_note.write_text(body_note.read_text().replace("正文。", "面试时这样回答。"))
        covered = self.vault / "知识库/面试题库/RAG/x/已有.md"
        covered.parent.mkdir(parents=True)
        covered.write_text(f"---\ntitle: 已有\ntype: 面试题\nsource_aweme_ids: ['{COVERED}']\n---\n\n# 已有\n")
        self.notes[COVERED].write_text(self.notes[COVERED].read_text().replace("正文。", "面试"))

    def run_plan(self):
        created = dy_author.initialize(self.config)
        Path(created["input_file"]).write_text("/dy-author 补录", encoding="utf-8")
        run, state, config = dy_author.load_run(created["run_dir"], self.config)
        return run, state, config, dy_author.plan(run, state, config)

    def test_plan_selects_uncovered_interview_notes_with_raw_first(self):
        run, state, config, result = self.run_plan()
        items = {i["id"]: i for i in state["items"]}
        self.assertEqual(set(items), {WITH_RAW, BODY_ONLY})
        self.assertEqual(items[WITH_RAW]["basis"], "原始材料")
        self.assertIn("笔记正文", items[BODY_ONLY]["basis"])

    def test_commit_writes_questions_and_author_without_touching_body(self):
        run, state, config, _ = self.run_plan()
        found = {"id": WITH_RAW, "sec_uid": AUTHOR, "nickname": "甲", "route": "fake"}
        with patch.object(backfill_authors.Lookup, "__call__", return_value=found):
            result = dy_author.next_item(run, state, config, prepare_all=True)
        ready = {r["id"]: r for r in result["ready"]}[WITH_RAW]
        self.assertEqual(ready["mode"], "supplement")
        self.assertIn("面试高频题", Path(ready["material_file"]).read_text())
        note = self.notes[WITH_RAW]
        body_before = library.read_note(note)[1]
        item_dir = Path(ready["item_dir"])
        (item_dir / "q.md").write_text(question_body(), encoding="utf-8")
        pipeline.write_json(item_dir / "interview.json", {"questions": [{
            "action": "new", "category": ["面试题库", "Agent", "意图识别"], "title": "意图识别怎么做",
            "tags": ["意图"], "insight": "分层路由", "body_file": "q.md"}]})
        saved = dy_author.commit(run, state, config, WITH_RAW)
        self.assertEqual(saved["status"], "saved")
        front, body = library.read_note(note)
        self.assertEqual(body, body_before)
        self.assertEqual(front["douyin_author_id"], AUTHOR)
        qdoc = self.vault / "知识库/面试题库/Agent/意图识别/意图识别怎么做.md"
        self.assertEqual(library.read_note(qdoc)[0]["source_aweme_ids"], [WITH_RAW])
        self.assertIn(AUTHOR, author_docs.author_docs(self.vault, self.config))
        index, log = unified_store.load(self.vault)
        self.assertEqual([r[2] for r in log], ["新增面试题"])
        self.assertIn("面试题", [r["cells"][1] for r in index])
        _, state2, _, _ = self.run_plan()
        self.assertEqual({i["id"] for i in state2["items"]}, {BODY_ONLY})

    def test_note_changed_after_prepare_is_rejected(self):
        run, state, config, _ = self.run_plan()
        with patch.object(backfill_authors.Lookup, "__call__", return_value={"reason": "none"}):
            result = dy_author.next_item(run, state, config, prepare_all=True)
        ready = {r["id"]: r for r in result["ready"]}[BODY_ONLY]
        item_dir = Path(ready["item_dir"])
        (item_dir / "q.md").write_text(question_body(), encoding="utf-8")
        pipeline.write_json(item_dir / "interview.json", {"questions": [{
            "action": "new", "category": ["面试题库", "项目与表达", "答法"], "title": "怎么答",
            "tags": ["x"], "insight": "y", "body_file": "q.md"}]})
        self.notes[BODY_ONLY].write_text(self.notes[BODY_ONLY].read_text() + "修改\n")
        with self.assertRaisesRegex(ValueError, "修改"):
            dy_author.commit(run, state, config, BODY_ONLY)
        self.assertEqual(question_bank.scan_questions(self.vault, self.config)[0]["title"], "已有")


if __name__ == "__main__":
    unittest.main()
