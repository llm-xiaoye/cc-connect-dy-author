import json
import unittest
from unittest.mock import patch

import catalog_context
import dy_author


class CatalogContextTests(unittest.TestCase):
    def test_small_views_keep_the_whole_category_tree(self):
        notes = {"notes_root": "知识库", "categories": ["知识库/AI/Agent", "知识库/AI/RAG"],
                 "notes": [{"path": f"知识库/AI/RAG/{n}.md", "title": str(n)} for n in range(1000)]}
        questions = {"question_root": "知识库/面试题库", "categories": ["面试题库/RAG/重排"],
                     "suggested_first_levels": ["RAG"], "questions": [{"title": "题"}]}
        with patch.object(catalog_context.library, "catalog", return_value=notes), \
             patch.object(catalog_context.question_bank, "catalog", return_value=questions):
            small_notes, small_questions = catalog_context.compact_catalogs({"vault": "/unused"})
        self.assertEqual(small_notes["categories"], notes["categories"])
        self.assertEqual(small_questions["categories"], questions["categories"])
        self.assertEqual(small_notes["note_count"], 1000)
        self.assertNotIn("notes", small_notes)
        self.assertNotIn("questions", small_questions)
        self.assertLess(len(json.dumps(small_notes)), 1000)

    def test_retrieval_searches_past_the_response_limit_and_preserves_merge_hash(self):
        rows = [{"path": f"知识库/面试题库/Agent/路由/{n}.md", "title": f"其他问题{n}",
                 "forms": "", "tags": [], "insight": ""} for n in range(100)]
        rows.append({"path": "知识库/面试题库/Agent/记忆/目标.md", "title": "记忆与状态",
                     "forms": "如何避免历史覆盖状态", "tags": ["memory"], "insight": "分层",
                     "sha256": "a" * 64, "source_aweme_ids": ["123"]})
        result = catalog_context.matches(rows, ["memory", "历史覆盖状态"], 1)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["candidates"][0]["sha256"], "a" * 64)
        self.assertEqual(result["candidates"][0]["path"], rows[-1]["path"])

    def test_normal_work_does_not_read_question_details_and_reports_more_matches(self):
        rows = [{"path": f"知识库/AI/Agent/{n}.md", "title": "记忆", "tags": [], "insight": "分层"}
                for n in range(20)]
        with patch.object(catalog_context.library, "catalog", return_value={"notes": rows}), \
             patch.object(catalog_context.question_bank, "catalog", side_effect=AssertionError("not needed")):
            result = catalog_context.search({"vault": "/unused"}, ["记忆"], interview=False)
        self.assertEqual(len(result["knowledge"]["candidates"]), 8)
        self.assertEqual(result["knowledge"]["total_matches"], 20)
        self.assertTrue(result["knowledge"]["has_more"])
        self.assertTrue(result["questions"]["not_required"])

    def test_response_size_is_bounded_without_claiming_all_matches_returned(self):
        rows = [{"path": f"知识库/AI/Agent/{n}.md", "title": "记忆" + "长" * 200,
                 "tags": ["关键词" * 50] * 12, "insight": "分层" * 300} for n in range(20)]
        with patch.object(catalog_context.library, "catalog", return_value={"notes": rows}):
            result = catalog_context.search({"vault": "/unused"}, ["记忆"], interview=False, limit=20)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 12000)
        self.assertTrue(result["knowledge"]["has_more"])
        self.assertEqual(result["knowledge"]["total_matches"], 20)


class ContextResumeTests(unittest.TestCase):
    def test_search_refreshes_old_large_views_without_touching_material_drafts_or_saved_notes(self):
        from pathlib import Path
        import helpers
        import library
        root = helpers.temp_root(self)
        vault = helpers.make_vault(root)
        config = helpers.make_config(root, vault)
        saved = library.save_note(config, "7659111274168454401", helpers.analysis(), helpers.skeleton(),
                                  source_url="https://www.douyin.com/video/7659111274168454401")
        note = Path(saved["note"])
        original_note = note.read_bytes()
        initialized = dy_author.initialize(config)
        run, state, frozen = dy_author.load_run(initialized["run_dir"], config)
        vid = "7659111274168454402"
        target = run / "items" / vid
        target.mkdir(parents=True)
        material, draft, analysis = target / "material.txt", target / "knowledge.md", target / "analysis.json"
        material.write_text("完整原始材料")
        draft.write_text("未提交草稿")
        analysis.write_text("{}")
        (target / "catalog.json").write_text(json.dumps({"notes": ["历史大目录"] * 1000}))
        (target / "catalog-terms.txt").write_text("记忆\n")
        state["items"] = [{"id": vid, "url": f"https://www.douyin.com/video/{vid}", "status": "ready",
                           "interview": False, "artifacts": {"item_dir": str(target), "material_file": str(material),
                           "knowledge_file": str(draft), "analysis_file": str(analysis)}}]
        result = dy_author.catalog_search(run, state, frozen, vid)
        self.assertEqual(result["knowledge"]["total_matches"], 1)
        self.assertNotIn("notes", json.loads((target / "catalog.json").read_text()))
        self.assertEqual((material.read_text(), draft.read_text(), analysis.read_text()),
                         ("完整原始材料", "未提交草稿", "{}"))
        self.assertEqual(note.read_bytes(), original_note)
        self.assertEqual(state["items"][0]["status"], "ready")
        self.assertTrue(dy_author.ready_result(state["items"][0])["draft_files_present"])
        self.assertEqual(dy_author.progress_report(state)["saved"], 0)


class BatchProgressTests(unittest.TestCase):
    def test_batch_is_not_finished_until_each_work_is_committed_or_explicitly_ended(self):
        items = [{"id": str(n), "status": "ready"} for n in range(10)]
        items += [{"id": "10", "status": "pending"}, {"id": "11", "status": "exists"}]
        state = {"items": items, "last_batch_ids": [str(n) for n in range(10)], "source_complete": True}
        self.assertFalse(dy_author.progress_report(state)["batch_finished"])
        for item in items[:8]:
            item["status"] = "saved"
        items[8]["status"] = "failed"
        self.assertFalse(dy_author.progress_report(state)["batch_finished"])
        items[9]["status"] = "skipped"
        report = dy_author.progress_report(state)
        self.assertTrue(report["batch_finished"])
        self.assertEqual((report["saved"], report["exists"], report["failed"], report["remaining"]), (8, 1, 1, 1))
        self.assertEqual(report["processed"], 11)
        self.assertIn("失败待重试 1 条", report["message"])

    def test_prepare_selection_never_exceeds_ten_and_reserves_ready_slots(self):
        state = {"items": [{"id": str(n), "status": "pending"} for n in range(40)]}
        self.assertEqual(len(dy_author.select_batch(state, 50)), 10)
        state["items"][0]["status"] = "ready"
        self.assertEqual(len(dy_author.select_batch(state, 10)), 9)


if __name__ == "__main__":
    unittest.main()
