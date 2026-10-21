import unittest
from pathlib import Path
from unittest.mock import patch

import helpers
from helpers import AUTHOR, analysis, skeleton
import image_posts
import library
import topic_taxonomy
import unified_store

A = "7659111274168454401"
B = "7659111274168454402"
URL_A = f"https://www.douyin.com/video/{A}"


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)

    def save(self, vid, data=None, body=None, **kwargs):
        return library.save_note(self.config, vid, library.validate_analysis(data or analysis()),
                                 body or skeleton(), source_url=f"https://www.douyin.com/video/{vid}", **kwargs)

    def test_unified_catalogs_dedup_and_same_title_suffix(self):
        result = self.save(A)
        note = Path(result["note"])
        self.assertEqual(note.relative_to(self.vault).as_posix(), "知识库/AI/Agent/记忆管理/记忆的状态与历史.md")
        index, log = unified_store.load(self.vault)
        self.assertEqual((len(index), len(log)), (1, 1))
        self.assertEqual(log[0][1:3], ["DY", "新增"])
        self.assertIn(URL_A, log[0][5])
        snapshot = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}
        self.assertEqual(self.save(A)["status"], "exists")
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file()})
        self.assertTrue(self.save(B)["note"].endswith("记忆的状态与历史-2.md"))

    def test_failed_index_write_rolls_back(self):
        index = self.vault / unified_store.INDEX_NAME
        log = self.vault / unified_store.LOG_NAME
        before = (index.read_bytes(), log.read_bytes())
        original = library.atomic_write

        def fail_index(path, content, exclusive=False):
            if path == index:
                raise OSError("synthetic index failure")
            return original(path, content, exclusive)
        with patch.object(library, "atomic_write", side_effect=fail_index):
            with self.assertRaises(OSError):
                self.save(A)
        self.assertEqual((index.read_bytes(), log.read_bytes()), before)
        self.assertFalse(library.find_existing(self.vault, A))
        self.assertFalse((self.vault / ".dy-author-transaction.json").exists())

    def test_source_url_required_and_must_match(self):
        with self.assertRaisesRegex(ValueError, "原作品链接"):
            library.save_note(self.config, A, library.validate_analysis(analysis()), skeleton())
        with self.assertRaises(ValueError):
            library.save_note(self.config, A, library.validate_analysis(analysis()), skeleton(),
                              source_url=f"https://www.douyin.com/video/{B}")

    def test_invalid_and_question_bank_categories_rejected_for_notes(self):
        for category in [["AI"], ["AI", "../escape"], ["AI", ".private"], ["面试题库", "RAG"],
                         ["面试题库", "RAG", "检索"], ["AI", "面试"]]:
            with self.subTest(category), self.assertRaises(ValueError):
                self.save(A, analysis(category=category))
        self.assertFalse((self.vault / "知识库").exists())

    def test_non_unified_layout_is_refused(self):
        self.config["unified_only"] = False
        with self.assertRaisesRegex(ValueError, "unified_only"):
            self.save(A)

    def test_scan_ignores_author_and_question_documents_and_dot_dirs(self):
        self.save(A, author={"sec_uid": AUTHOR, "nickname": "甲"})
        (self.vault / "抖音作者").mkdir()
        (self.vault / "抖音作者" / "甲.md").write_text(f"---\ntype: 抖音作者\ndouyin_author_id: {AUTHOR}\n---\n")
        q = self.vault / "知识库" / "面试题库" / "RAG" / "重排" / "题.md"
        q.parent.mkdir(parents=True)
        q.write_text(f"---\ntitle: 题\ntype: 面试题\nsource_aweme_ids: ['{A}']\n---\n")
        hidden = self.vault / ".trash" / "x.md"
        hidden.parent.mkdir()
        hidden.write_text(f"---\naweme_id: '{A}'\n---\n")
        rows = library.existing_map(self.vault)
        self.assertEqual(list(rows), [A])
        self.assertEqual(rows[A]["douyin_author_id"], AUTHOR)

    def test_extra_catalog_rows_survive_a_plain_dy_save(self):
        relative = "知识库/面试题库/RAG/检索与重排/为什么要重排.md"

        def before(vault, changes, extra_index, extra_logs, note_path, front):
            path = vault / relative
            changes[path] = library.render_note({"title": "为什么要重排", "type": "面试题"}, "# 为什么要重排\n").encode()
            extra_index.append({"path": relative, "cells": [unified_store.note_link("为什么要重排", relative),
                                                             "面试题", "RAG", "召回求全", "2026-09-29 10:00:00"]})
            extra_logs.append(["2026-09-29 10:00:00", "DY", "新增面试题", unified_store.note_link("为什么要重排", relative),
                               "面试题库 / RAG / 检索与重排", f"[原作品]({URL_A})", "未复审"])
            return {"questions": [relative]}
        result = self.save(A, before_metadata=before)
        self.assertEqual(result["questions"], [relative])
        self.save(B, analysis(title="另一篇"))
        index, log = unified_store.load(self.vault)
        self.assertIn(relative, [row["path"] for row in index])
        self.assertIn("新增面试题", [row[2] for row in log])
        self.assertIn("## 面试题库 / RAG / 检索与重排", (self.vault / unified_store.INDEX_NAME).read_text())

    def test_write_extras_skips_unchanged_files(self):
        path = self.vault / "抖音作者" / "x.md"
        stage = lambda vault, changes, ei, el: changes.__setitem__(path, b"same\n")
        self.assertEqual(len(library.write_extras(self.config, stage)["changed_files"]), 1)
        self.assertEqual(library.write_extras(self.config, stage)["changed_files"], [])


    def test_previous_success_survives_later_write_failure_and_resume_deduplicates(self):
        first = self.save(A, author={"sec_uid": AUTHOR, "nickname": "已核实作者|甲"})
        note = Path(first["note"])
        index, log = self.vault / unified_store.INDEX_NAME, self.vault / unified_store.LOG_NAME
        before = {path: path.read_bytes() for path in (note, index, log)}
        original = library.atomic_write

        def fail_second_log(path, content, exclusive=False):
            if path == log:
                raise OSError("second work log failure")
            return original(path, content, exclusive)

        with patch.object(library, "atomic_write", side_effect=fail_second_log):
            with self.assertRaises(OSError):
                self.save(B)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(library.find_existing(self.vault, A), note)
        self.assertIsNone(library.find_existing(self.vault, B))
        self.assertEqual(self.save(A)["status"], "exists")
        self.assertEqual(self.save(B)["status"], "saved")
        _, rows = unified_store.load(self.vault)
        self.assertEqual(next(row[-1] for row in rows if URL_A in row[5]), "已核实作者\\|甲")
        self.assertEqual(next(row[-1] for row in rows if B in row[5]), "")


class TaxonomyTests(unittest.TestCase):
    def test_question_categories(self):
        self.assertEqual(topic_taxonomy.validate_question_category(["面试题库", "RAG", "检索与重排"])[1], "RAG")
        self.assertEqual(topic_taxonomy.validate_question_category(["面试题库", "全新一级", "新子方向"])[1], "全新一级")
        for bad in [["面试题库", "RAG"], ["知识库", "RAG", "x"], ["面试题库", "RAG", "a/b"],
                    ["面试题库", "抖音", "x"], ["面试题库", ".hidden", "x"], ["面试题库", "RAG", "x" * 25]]:
            with self.subTest(bad), self.assertRaises(ValueError):
                topic_taxonomy.validate_question_category(bad)

    def test_fixed_agent_and_rag_trees_kept(self):
        topic_taxonomy.validate_category(["AI", "Agent", "记忆管理", "体系与分层"])
        topic_taxonomy.validate_category(["AI", "RAG", "检索与重排"])
        for bad in [["AI", "Agent", "未知"], ["AI", "RAG", "未知"], ["抖音", "x"], ["面试题库", "RAG"]]:
            with self.subTest(bad), self.assertRaises(ValueError):
                topic_taxonomy.validate_category(bad)


class ImageEvidenceTests(unittest.TestCase):
    def test_partial_or_changed_pages_are_rejected(self):
        import hashlib
        cache = helpers.temp_root(self)
        f = cache / "1.png"
        f.write_bytes(b"fixture image")
        entry = {"page": 1, "file": "1.png", "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
        bundle = {"aweme_id": A, "image_count": 1, "images": [entry]}
        valid = {"aweme_id": A, "pages": [dict(entry, text="知识证据")]}
        image_posts.validate_evidence(valid, bundle, cache)
        with self.assertRaises(image_posts.StageError):
            image_posts.validate_evidence(dict(valid, pages=[]), bundle, cache)
        f.write_bytes(b"changed")
        with self.assertRaises(image_posts.StageError):
            image_posts.validate_evidence(valid, bundle, cache)


if __name__ == "__main__":
    unittest.main()
