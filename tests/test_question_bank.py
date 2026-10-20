import hashlib
import json
from pathlib import Path
import unittest

import helpers
from helpers import question_body
import library
import pipeline
import question_bank as qb
import unified_store

A = "7659111274168454401"
B = "7659111274168454402"


class QuestionBankTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)
        self.item = self.root / "item"
        self.item.mkdir()

    def interview(self, questions):
        pipeline.write_json(self.item / "interview.json", {"questions": questions})
        return qb.load_interview(self.item / "interview.json", self.item)

    def new_question(self, title="为什么需要重排", body=None, category=("面试题库", "RAG", "检索与重排")):
        (self.item / "q.md").write_text(body or question_body(), encoding="utf-8")
        return {"action": "new", "category": list(category), "title": title, "tags": ["RAG"],
                "insight": "召回求全、重排求准", "body_file": "q.md"}

    def stage(self, vid, questions, note="知识库/AI/RAG/笔记.md"):
        changes, ei, el = {}, [], []
        summary = qb.stage_questions(self.vault, self.config, changes, ei, el, vid=vid,
                                     url=f"https://www.douyin.com/video/{vid}", note_path=self.vault / note,
                                     note_title="笔记", questions=questions)
        with library.locked(self.vault):
            library.metadata_changes(self.vault, self.config, changes, [], ei, el)
            library.transact(self.vault, changes)
        return summary

    def test_new_document_shape_and_catalog_rows(self):
        summary = self.stage(A, self.interview([self.new_question()]))
        path = Path(summary[0]["path"])
        self.assertEqual(path.relative_to(self.vault).as_posix(), "知识库/面试题库/RAG/检索与重排/为什么需要重排.md")
        front, body = library.read_note(path)
        self.assertEqual(front["type"], "面试题")
        self.assertNotIn("aweme_id", front)
        self.assertEqual((front["source_aweme_ids"], front["source_urls"]), ([A], [f"https://www.douyin.com/video/{A}"]))
        headings = [line for line in body.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## " + s for s in qb.SECTIONS] + ["## 相关笔记", "## 我的补充"])
        index, log = unified_store.load(self.vault)
        self.assertEqual(index[0]["cells"][1], "面试题")
        self.assertEqual(log[0][2], "新增面试题")
        self.assertEqual(log[0][4], "面试题库 / RAG / 检索与重排")
        self.assertEqual(library.scan(self.vault), [])

    def test_merge_checks_sha_unions_sources_and_keeps_personal_notes(self):
        path = Path(self.stage(A, self.interview([self.new_question()]))[0]["path"])
        path.write_text(path.read_text(encoding="utf-8") + "我的亲自补充。\n", encoding="utf-8")
        relative = path.relative_to(self.vault).as_posix()
        (self.item / "m.md").write_text(question_body("合并后的新补充。"), encoding="utf-8")
        merge = {"action": "merge", "target": relative, "expected_sha256": "0" * 64, "title": "为什么需要重排",
                 "tags": ["RAG", "重排"], "insight": "两阶段分工", "body_file": "m.md"}
        with self.assertRaisesRegex(ValueError, "被修改"):
            self.stage(B, self.interview([merge]))
        merge["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        summary = self.stage(B, self.interview([merge]))
        front, body = library.read_note(path)
        self.assertEqual(front["source_aweme_ids"], [A, B])
        self.assertIn("合并后的新补充。", body)
        self.assertIn("我的亲自补充。", body)
        self.assertEqual(body.count("｜[原作品]"), 2)
        backup = [s for s in summary if s["action"] == "备份"][0]["path"]
        self.assertTrue(Path(backup).is_file())
        index, log = unified_store.load(self.vault)
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["cells"][3], "两阶段分工")
        self.assertEqual(sorted(r[2] for r in log), ["合并面试题", "新增面试题"])
        merge["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "已收录"):
            self.stage(B, self.interview([merge]))

    def test_body_and_interview_validation(self):
        good = question_body()
        bad_bodies = [good.replace("## 答题重点\n\n考察工程取舍。\n\n", ""),
                      good.replace("- 再路由\n- 最后兜底\n", ""),
                      good + "\n## 相关笔记\n\n不要写\n",
                      "# 标题\n\n" + good,
                      good.replace("## 模型补充\n\n比例只是示例。", "## 模型补充\n\n")]
        for body in bad_bodies:
            with self.subTest(body[:20]), self.assertRaises(ValueError):
                self.interview([self.new_question(body=body)])
        for category in (["面试题库", "RAG"], ["知识库", "RAG", "x"], ["面试题库", "抖音", "x"]):
            with self.subTest(category), self.assertRaises(ValueError):
                self.interview([self.new_question(category=category)])
        outside = self.new_question()
        outside["body_file"] = "../escape.md"
        with self.assertRaises(ValueError):
            self.interview([outside])
        with self.assertRaises(ValueError):
            self.interview([])
        self.assertEqual(self.interview([self.new_question(category=("面试题库", "全新方向", "新子方向"))])[0]["category"][1],
                         "全新方向")

    def test_question_document_with_aweme_id_is_rejected(self):
        path = self.vault / "知识库/面试题库/RAG/x/坏.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\ntitle: 坏\ntype: 面试题\naweme_id: '{A}'\n---\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "aweme_id"):
            qb.scan_questions(self.vault, self.config)

    def test_mentions_interview_is_literal(self):
        self.assertTrue(qb.mentions_interview("", None, "这是面试高频题"))
        self.assertFalse(qb.mentions_interview("面 试", "interview"))

    def test_catalog_lists_questions_for_merge_decisions(self):
        self.stage(A, self.interview([self.new_question()]))
        data = qb.catalog(self.vault, self.config)
        self.assertEqual(data["categories"], ["面试题库/RAG/检索与重排"])
        self.assertEqual(data["questions"][0]["category"], ["面试题库", "RAG", "检索与重排"])
        self.assertEqual(data["questions"][0]["source_aweme_ids"], [A])
        self.assertIn("意图识别", data["questions"][0]["forms"])
        self.assertIn("大模型基础", data["suggested_first_levels"])
        json.dumps(data, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
