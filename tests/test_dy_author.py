import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import helpers
from helpers import AUTHOR, analysis, question_body, skeleton
import author_docs
import author_works
import dy_author
import library
import pipeline
import question_bank
import unified_store

OLD = "7659111274168454446"
FIRST = "7659111274168454447"
SECOND = "7659111274168454448"
IMAGE = "7659111274168454450"
BAD = "7659111274168454449"
INTERVIEW = "7659111274168454451"


def listing(*works, complete=True, reason=None, author=AUTHOR, nickname="测试作者"):
    return author_works.Listing([{"id": i, "kind": k, "title": t} for i, k, t in works], complete, reason, 1,
                                author, nickname, "anonymous")


class QueueBase(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)

    def create_run(self, raw):
        created = dy_author.initialize(self.config)
        Path(created["input_file"]).write_text(raw, encoding="utf-8")
        return dy_author.load_run(created["run_dir"], self.config)

    def plan(self, works_listing, seed=FIRST, kind="video"):
        run, state, config = self.create_run(f"/dy-author 复制打开抖音 https://www.douyin.com/{kind}/{seed} 看看")
        with patch.object(author_works, "enumerate_work", new=AsyncMock(return_value=works_listing)):
            result = dy_author.plan(run, state, config)
        return run, state, config, result

    def fake_prepare(self, args, config):
        url, vid = pipeline.normalize_url(args.source)
        text = "面试官常问这一题。" if vid == INTERVIEW else "Synthetic knowledge evidence."
        bundle = helpers.video_bundle(Path(config["cache_dir"]), vid, author_id=AUTHOR, text=text)
        data = pipeline.load_json(bundle)
        data["source_url"] = url
        pipeline.write_json(bundle, data)
        print(json.dumps({"bundle": str(bundle)}))

    def fake_transcribe(self, args, config):
        if Path(args.bundle).parent.name == BAD:
            raise RuntimeError("Synthetic recognition failure")
        print(json.dumps({"status": "cached"}))

    def patched(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(pipeline, "prepare", side_effect=self.fake_prepare))
        stack.enter_context(patch.object(pipeline, "transcribe", side_effect=self.fake_transcribe))
        return stack

    def write_note(self, ready, title=None):
        title = title or f"知识 {ready['id'][-4:]}"
        Path(ready["knowledge_file"]).write_text(skeleton(), encoding="utf-8")
        pipeline.write_json(ready["analysis_file"], analysis(title=title))

    def write_question(self, ready, **question):
        item_dir = Path(ready["item_dir"])
        (item_dir / "q1.md").write_text(question_body(), encoding="utf-8")
        data = {"action": "new", "category": ["面试题库", "RAG", "检索与重排"], "title": "为什么需要重排",
                "tags": ["RAG", "重排"], "insight": "召回求全、重排求准", "body_file": "q1.md"}
        data.update(question)
        pipeline.write_json(item_dir / "interview.json", {"questions": [data]})


class PlanTests(QueueBase):
    def test_parse_input(self):
        self.assertEqual(dy_author.parse_input("/dy-author 补录"), ("supplement", None))
        mode, text = dy_author.parse_input("/dy-author 6.99 复制打开抖音 https://v.douyin.com/x/ K@W")
        self.assertEqual(mode, "author")
        for bad in ("", "/dy-author", "/dy-author 复制打开抖音 没有链接", "/dy-author 随便"):
            with self.subTest(bad), self.assertRaises(ValueError):
                dy_author.parse_input(bad)

    def test_plan_dedups_against_vault_and_skipped_and_writes_author_doc(self):
        old = helpers.existing_note(self.vault, OLD)
        before = old.read_bytes()
        state_path = author_docs.state_path(self.vault, self.config)
        state_path.parent.mkdir(parents=True)
        state_path.write_bytes(author_docs.encode_state({"version": 1, "authors": {AUTHOR: {
            "nickname": "测试作者", "doc_path": None, "listing": None,
            "skipped": {SECOND: {"reason": "纯配乐", "at": "x"}}, "failed": {}}}}))
        works = listing((OLD, "video", ""), (FIRST, "video", "t1"), (IMAGE, "note", "图文"), (SECOND, "video", ""))
        run, state, config, result = self.plan(works)
        self.assertEqual(result["counts"], {"pending": 2, "ready": 0, "saved": 0, "exists": 1, "skipped": 1, "failed": 0,
                                            "unsupported": 0})
        self.assertNotIn("暂不支持", Path(result["author_doc"]).read_text())
        self.assertTrue(result["source_complete"])
        self.assertEqual(dy_author.select_item(state, IMAGE)["url"], f"https://www.douyin.com/note/{IMAGE}")
        doc = Path(result["author_doc"])
        self.assertEqual(doc.relative_to(self.vault).as_posix(), "抖音作者/测试作者.md")
        text = doc.read_text()
        self.assertIn("| 已入库 |", text)
        self.assertIn("纯配乐", text)
        self.assertIn("清单完整", text)
        self.assertNotIn("aweme_id", text.split("---")[1])
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(len(library.scan(self.vault)), 1)

    def test_articles_are_listed_as_unsupported_and_never_prepared(self):
        ARTICLE = "7659111274168454452"
        works = listing((FIRST, "video", "t1"), (ARTICLE, "article", "长文"))
        run, state, config, result = self.plan(works)
        self.assertEqual(result["counts"]["pending"], 1)
        self.assertEqual(result["counts"]["unsupported"], 1)
        self.assertEqual(result["unsupported"][0]["url"], f"https://www.douyin.com/video/{ARTICLE}")
        text = Path(result["author_doc"]).read_text()
        self.assertIn(f"| [{ARTICLE}](https://www.douyin.com/video/{ARTICLE}) | 文章 | 暂不支持 | 抖音文章暂不支持，未导入 |", text)
        self.assertIn("失败 0，暂不支持 1", text)
        prepared = []
        with patch.object(pipeline, "prepare", side_effect=lambda a, c: prepared.append(a.source) or self.fake_prepare(a, c)), \
             patch.object(pipeline, "transcribe", side_effect=self.fake_transcribe):
            ready = dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(prepared, [f"https://www.douyin.com/video/{FIRST}"])
        self.write_note(ready["ready"][0])
        self.assertEqual(dy_author.commit(run, state, config, FIRST)["status"], "saved")
        final = dy_author.report(state)
        self.assertTrue(final["finished"])
        self.assertTrue(final["source_complete"])
        self.assertFalse(final["complete"])
        self.assertEqual(final["retry_command"], "")

    def test_article_detected_at_prepare_becomes_unsupported(self):
        works = listing((FIRST, "video", ""),)
        run, state, config, result = self.plan(works)
        article = pipeline.image_posts.StageError("detail", "文章", "UNSUPPORTED_ARTICLE")
        with patch.object(pipeline, "prepare", side_effect=article):
            dy_author.next_item(run, state, config, prepare_all=True)
        item = state["items"][0]
        self.assertEqual((item["status"], item["kind"]), ("unsupported", "article"))
        entry = author_docs.load_state(self.vault, self.config)["authors"][AUTHOR]
        self.assertIn(FIRST, entry["unsupported"])
        self.assertNotIn(FIRST, entry["failed"])
        self.assertIn("暂不支持", Path(result["author_doc"]).read_text())

    def test_partial_listing_keeps_seed_and_reports_retry(self):
        works = listing((SECOND, "video", ""), complete=False, reason="http_403_argus_uifid_not_found")
        run, state, config, result = self.plan(works)
        self.assertFalse(result["source_complete"])
        self.assertEqual([i["id"] for i in state["items"]], [FIRST, SECOND])
        self.assertIn("清单不完整（http_403_argus_uifid_not_found）", Path(result["author_doc"]).read_text())
        self.assertEqual(result["retry_command"], f"/dy-author https://www.douyin.com/video/{FIRST}")

    def test_unknown_author_processes_only_seed_without_doc(self):
        works = author_works.Listing([], False, "detail_unavailable")
        run, state, config, result = self.plan(works)
        self.assertIsNone(result["author"])
        self.assertEqual([i["id"] for i in state["items"]], [FIRST])
        self.assertFalse((self.vault / "抖音作者").exists())


class PrepareCommitTests(QueueBase):
    def test_limit_batches_and_end_to_end_commit(self):
        works = listing((FIRST, "video", ""), (SECOND, "video", ""), (IMAGE, "video", ""))
        run, state, config, _ = self.plan(works)
        with self.patched() as _:
            result = dy_author.next_item(run, state, config, prepare_all=True, limit=2)
            self.assertEqual([r["id"] for r in result["ready"]], [FIRST, SECOND])
            self.assertEqual(result["remaining_pending"], 1)
            again = dy_author.next_item(run, state, config, prepare_all=True, limit=2)
            self.assertEqual(len(again["ready"]), 2)
            ready = dy_author.next_item(run, state, config)
            self.assertEqual(ready["id"], FIRST)
            self.assertFalse(ready["interview_required"])
            self.assertEqual(Path(ready["material_file"]).read_text(), "Synthetic knowledge evidence.")
            self.write_note(ready)
            self.assertEqual(dy_author.commit(run, state, config, FIRST)["status"], "saved")
            third = dy_author.next_item(run, state, config, prepare_all=True, limit=2)
            self.assertEqual([r["id"] for r in third["ready"]], [SECOND, IMAGE])
        note = library.find_existing(self.vault, FIRST)
        front, _ = library.read_note(note)
        self.assertEqual((front["douyin_author"], front["douyin_author_id"]), ("测试作者", AUTHOR))
        doc = Path(state["author_doc"]).read_text()
        self.assertIn("已入库 1，待导入 2", doc)
        index, log = unified_store.load(self.vault)
        self.assertEqual((len(index), len(log)), (1, 1))
        data = helpers.read_json(run / "usage.json")
        entry = data["items"][FIRST]
        self.assertIn("prepare", entry["timings"])
        self.assertIn("commit", entry["timings"])
        self.assertGreater(entry["written"]["knowledge"]["chars"], 0)
        summary = dy_author.report(state)["usage"]
        self.assertGreater(summary["estimated_output_tokens"], 0)
        self.assertIn("估算", summary["note"])

    def test_interview_item_requires_and_writes_question_doc_in_same_transaction(self):
        run, state, config, _ = self.plan(listing((INTERVIEW, "video", "面试官：怎么答"), (FIRST, "video", "")),
                                          seed=INTERVIEW)
        with self.patched():
            result = dy_author.next_item(run, state, config, prepare_all=True)
        ready = {r["id"]: r for r in result["ready"]}
        self.assertTrue(ready[INTERVIEW]["interview_required"])
        self.assertFalse(ready[FIRST]["interview_required"])
        self.write_note(ready[INTERVIEW], "意图识别的三层漏斗")
        with self.assertRaisesRegex(ValueError, "interview.json"):
            dy_author.commit(run, state, config, INTERVIEW)
        self.assertIsNone(library.find_existing(self.vault, INTERVIEW))
        self.write_question(ready[INTERVIEW])
        saved = dy_author.commit(run, state, config, INTERVIEW)
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(saved["questions"][0]["action"], "新增面试题")
        qdoc = self.vault / "知识库/面试题库/RAG/检索与重排/为什么需要重排.md"
        front, body = library.read_note(qdoc)
        self.assertNotIn("aweme_id", front)
        self.assertEqual(front["source_aweme_ids"], [INTERVIEW])
        self.assertIn("## 相关笔记", body)
        self.assertIn("[[知识库/AI/Agent/记忆管理/意图识别的三层漏斗|意图识别的三层漏斗]]", body)
        self.assertTrue(body.rstrip().endswith("## 我的补充"))
        index, log = unified_store.load(self.vault)
        self.assertEqual(sorted(r["cells"][1] for r in index), ["知识笔记", "面试题"])
        self.assertEqual(sorted(r[2] for r in log), ["新增", "新增面试题"])
        self.assertIn("## 面试题库 / RAG / 检索与重排", (self.vault / unified_store.INDEX_NAME).read_text())
        self.assertIn("为什么需要重排", Path(state["author_doc"]).read_text())
        self.assertEqual(len(library.scan(self.vault)), 1)
        # A non-interview work cannot carry question documents.
        self.write_note(ready[FIRST])
        self.write_question(ready[FIRST], title="另一题")
        with self.assertRaisesRegex(ValueError, "非面试类"):
            dy_author.commit(run, state, config, FIRST)

    def test_failed_transaction_writes_nothing(self):
        run, state, config, _ = self.plan(listing((INTERVIEW, "video", "面试"),), seed=INTERVIEW)
        with self.patched():
            ready = dy_author.next_item(run, state, config, prepare_all=True)["ready"][0]
        self.write_note(ready, "失败的提交")
        self.write_question(ready)
        def files():
            return {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file() and p.suffix != ".lock"}
        before = files()
        original = library.atomic_write

        def broken(path, content, exclusive=False):
            if path.name == unified_store.LOG_NAME:
                raise OSError("disk failure")
            return original(path, content, exclusive)
        with patch.object(library, "atomic_write", side_effect=broken), self.assertRaises(OSError):
            dy_author.commit(run, state, config, INTERVIEW)
        self.assertEqual(before, files())
        self.assertFalse((self.vault / ".dy-transaction.json").exists())

    def test_concurrent_note_makes_commit_exists(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", ""),))
        with self.patched():
            ready = dy_author.next_item(run, state, config, prepare_all=True)["ready"][0]
        self.write_note(ready)
        other = helpers.existing_note(self.vault, FIRST, "知识库/别处/并发.md")
        before = other.read_bytes()
        self.assertEqual(dy_author.commit(run, state, config, FIRST)["status"], "exists")
        self.assertEqual(other.read_bytes(), before)
        self.assertEqual(len(library.scan(self.vault)), 1)

    def test_skip_is_remembered_and_fail_is_retried(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", ""), (BAD, "video", ""), (SECOND, "video", "")))
        with self.patched():
            result = dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(result["counts"]["failed"], 1)
        doc = Path(state["author_doc"]).read_text()
        self.assertIn("| 失败 |", doc)
        reason = Path(dy_author.select_item(state, FIRST)["artifacts"]["reason_file"])
        reason.write_text("只有配乐，没有讲解", encoding="utf-8")
        dy_author.mark_item(run, state, config, FIRST, "skipped")
        self.assertIn("只有配乐", Path(state["author_doc"]).read_text())
        run2, state2, config2, result2 = self.plan(listing((FIRST, "video", ""), (BAD, "video", ""), (SECOND, "video", "")))
        statuses = {i["id"]: i["status"] for i in state2["items"]}
        self.assertEqual(statuses, {FIRST: "skipped", BAD: "pending", SECOND: "pending"})

    def test_changed_material_rejected_before_commit(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", ""),))
        with self.patched():
            ready = dy_author.next_item(run, state, config, prepare_all=True)["ready"][0]
        self.write_note(ready)
        Path(ready["material_file"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "模型材料"):
            dy_author.commit(run, state, config, FIRST)

    def test_parallel_workers_prepare_batch(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", ""), (SECOND, "video", "")))
        # Spawned workers cannot see test patches; exercise the worker function directly.
        with self.patched():
            item = dy_author.prepare_worker(str(run), state, state["items"][0], config)
        self.assertEqual(item["status"], "ready")
        self.assertEqual(Path(item["artifacts"]["knowledge_file"]).parent, run / "items" / FIRST)


class RecoveryTests(QueueBase):
    def run_one(self):
        run, state, config, _ = self.plan(listing((FIRST, "video", ""),))
        return run, state, dict(config, browser="chrome", browser_cache_dir=str(self.root / "no-chrome-cache"))

    def test_detail_failure_opens_browser_once_then_recovers(self):
        run, state, config = self.run_one()
        missing = pipeline.image_posts.StageError("detail", "No matching media", "DETAIL_UNAVAILABLE")
        calls = []

        def prepare(args, actual):
            calls.append(args.source)
            if len(calls) == 1:
                raise missing
            self.fake_prepare(args, actual)

        def refresh(*args, **kwargs):
            persisted = pipeline.load_json(run / "state.json")["items"][0]
            self.assertTrue(persisted["browser_recovery"]["attempted"])
            return {"page_state": "not_observed"}
        with patch.object(pipeline, "prepare", side_effect=prepare), \
             patch.object(pipeline.douyin_access, "refresh_browser_session", side_effect=refresh) as browser, \
             patch.object(pipeline, "transcribe", side_effect=self.fake_transcribe):
            result = dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(result["status"], "prepared")
        browser.assert_called_once_with(FIRST, "chrome", 8, source_url=f"https://www.douyin.com/video/{FIRST}",
                                        profile=None)

    def test_resume_after_attempt_and_rate_limit_do_not_open_again(self):
        run, state, config = self.run_one()
        state["items"][0]["browser_recovery"] = {"attempted": True, "status": "started"}
        missing = pipeline.image_posts.StageError("detail", "No matching media", "DETAIL_UNAVAILABLE")
        with patch.object(pipeline, "prepare", side_effect=missing), \
             patch.object(pipeline.douyin_access, "refresh_browser_session") as browser:
            dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(state["items"][0]["error_code"], "RECOVERY_EXHAUSTED")
        browser.assert_not_called()
        run, state, config = self.run_one()
        limited = pipeline.image_posts.StageError("detail", "HTTP 429", "RATE_LIMITED")
        with patch.object(pipeline, "prepare", side_effect=limited) as prepare, \
             patch.object(pipeline.douyin_access, "refresh_browser_session") as browser:
            dy_author.next_item(run, state, config, prepare_all=True)
        prepare.assert_called_once()
        browser.assert_not_called()

    def test_background_first_then_browser_closes_only_the_opened_tab(self):
        run, state, config = self.run_one()
        config = dict(config, background_detail_first=True, close_opened_tabs=True, prefer_browser_media=False)
        missing = pipeline.image_posts.StageError("detail", "No matching media", "DETAIL_UNAVAILABLE")
        seen = []

        def prepare(args, actual):
            seen.append({k: actual.get(k) for k in ("_background_detail_only", "_browser_media_only")})
            if len(seen) == 1:
                raise missing
            self.fake_prepare(args, actual)
        order = []
        with patch.object(dy_author.pipeline.sys, "platform", "darwin"), \
             patch.object(pipeline, "prepare", side_effect=prepare), \
             patch.object(pipeline.douyin_access, "douyin_tab_ids",
                          side_effect=lambda *a, **k: order.append("snapshot") or ["7"]), \
             patch.object(pipeline.douyin_access, "refresh_browser_session",
                          side_effect=lambda *a, **k: order.append("open") or {"page_state": "not_observed"}), \
             patch.object(pipeline.douyin_access, "close_new_tabs",
                          side_effect=lambda *a, **k: order.append("close") or 1) as closer, \
             patch.object(pipeline, "transcribe", side_effect=self.fake_transcribe):
            result = dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(seen[0], {"_background_detail_only": True, "_browser_media_only": None})
        self.assertIsNone(seen[1]["_background_detail_only"])
        self.assertEqual(order, ["snapshot", "open", "close"])
        closer.assert_called_once_with(FIRST, ["7"], "chrome")
        self.assertEqual(pipeline.load_json(run / "state.json")["items"][0]["browser_recovery"]["opened_tab_closed"], 1)

    def test_background_success_opens_no_tab_and_failed_recovery_still_closes(self):
        run, state, config = self.run_one()
        config = dict(config, background_detail_first=True, close_opened_tabs=True)
        with patch.object(dy_author.pipeline.sys, "platform", "darwin"), \
             patch.object(pipeline, "prepare", side_effect=self.fake_prepare), \
             patch.object(pipeline.douyin_access, "douyin_tab_ids") as snapshot, \
             patch.object(pipeline.douyin_access, "refresh_browser_session") as browser, \
             patch.object(pipeline, "transcribe", side_effect=self.fake_transcribe):
            self.assertEqual(dy_author.next_item(run, state, config, prepare_all=True)["status"], "prepared")
        snapshot.assert_not_called()
        browser.assert_not_called()
        run, state, config = self.run_one()
        config = dict(config, background_detail_first=True, close_opened_tabs=True)
        missing = pipeline.image_posts.StageError("detail", "No matching media", "DETAIL_UNAVAILABLE")
        with patch.object(dy_author.pipeline.sys, "platform", "darwin"), \
             patch.object(pipeline, "prepare", side_effect=missing), \
             patch.object(pipeline.douyin_access, "douyin_tab_ids", return_value=None), \
             patch.object(pipeline.douyin_access, "refresh_browser_session", return_value={}), \
             patch.object(pipeline.douyin_access, "close_new_tabs", return_value=None) as closer:
            dy_author.next_item(run, state, config, prepare_all=True)
        item = state["items"][0]
        self.assertEqual(item["error_code"], "RECOVERY_EXHAUSTED")
        closer.assert_called_once_with(FIRST, None, "chrome")
        self.assertEqual(item["browser_recovery"]["opened_tab_closed"], "unavailable")

    def test_incomplete_cache_search_is_not_unavailable_media(self):
        run, state, config = self.run_one()

        def missing(args, actual):
            if "_browser_cache_diagnostics" in actual:
                actual["_browser_cache_diagnostics"]["scan_incomplete"] = True
            raise pipeline.image_posts.StageError("detail", "cache miss", "DETAIL_UNAVAILABLE")
        with patch.object(pipeline, "prepare", side_effect=missing), \
             patch.object(pipeline.douyin_access, "refresh_browser_session", return_value={}):
            result = dy_author.next_item(run, state, config, prepare_all=True)
        self.assertEqual(state["items"][0]["error_code"], "CACHE_LOOKUP_INCOMPLETE")
        self.assertFalse(result["model_analysis_needed"])
        self.assertEqual(result["status"], "done")


if __name__ == "__main__":
    unittest.main()
