from pathlib import Path
import unittest

import helpers
from helpers import AUTHOR, OTHER_AUTHOR
import author_docs as docs
import library

A = "7659111274168454401"
B = "7659111274168454402"
C = "7659111274168454403"


class AuthorDocTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)

    def stage(self, sec_uid=AUTHOR, **kwargs):
        return library.write_extras(self.config, lambda v, changes, ei, el: docs.stage_author(
            v, self.config, changes, sec_uid, **kwargs))

    def listing(self, *ids):
        return {"checked_at": "t", "complete": True, "reason": None, "access": "anonymous",
                "items": [{"id": i, "kind": "note" if i == C else "video"} for i in ids]}

    def test_rows_statuses_and_library_extras(self):
        helpers.existing_note(self.vault, A, extra_front=f"douyin_author: 甲\ndouyin_author_id: {AUTHOR}\n")
        helpers.existing_note(self.vault, "7659111274168454499", "知识库/AI/x/库内独有.md",
                              extra_front=f"douyin_author_id: {AUTHOR}\n")
        result = self.stage(nickname="甲", listing=self.listing(A, B, C), skipped={B: "纯配乐"})
        doc = Path(result["author_doc"])
        text = doc.read_text()
        self.assertEqual(doc.name, "甲.md")
        self.assertIn("共 4：已入库 2，待导入 1，跳过 1，失败 0", text)
        self.assertIn(f"[{C}](https://www.douyin.com/note/{C}) | 图文 | 待导入", text)
        self.assertIn("[[知识库/AI/旧主题/用户改名\\|用户标题]]", text)
        front, _ = library.read_note(doc)
        self.assertEqual((front["douyin_author_id"], front["listing_status"]), (AUTHOR, "完整"))
        self.assertNotIn("aweme_id", front)
        self.assertEqual(sorted(r["aweme_id"] for r in library.scan(self.vault)), sorted([A, "7659111274168454499"]))

    def test_outside_content_preserved_and_broken_markers_refused(self):
        doc = Path(self.stage(nickname="甲", listing=self.listing(A))["author_doc"])
        doc.write_text(doc.read_text().replace(docs.START, "我的备注。\n\n" + docs.START) + "\n末尾备注。\n")
        self.stage(nickname="甲改名", failed={A: "下载失败"})
        text = doc.read_text()
        self.assertIn("我的备注。", text)
        self.assertTrue(text.endswith("末尾备注。\n"))
        self.assertIn("| 失败 | 下载失败 |", text)
        self.assertEqual(library.read_note(doc)[0]["douyin_author"], "甲改名")
        self.assertEqual(doc.name, "甲.md")
        doc.write_text(doc.read_text().replace(docs.END, ""))
        with self.assertRaisesRegex(ValueError, "管理区域"):
            self.stage(nickname="甲")

    def test_same_nickname_different_author_gets_suffix(self):
        first = Path(self.stage(nickname="同名")["author_doc"])
        second = Path(self.stage(OTHER_AUTHOR, nickname="同名")["author_doc"])
        self.assertEqual(first.name, "同名.md")
        self.assertEqual(second.name, f"同名-{docs.suffix_for(OTHER_AUTHOR)}.md")
        self.assertNotEqual(docs.suffix_for(AUTHOR), docs.suffix_for(OTHER_AUTHOR))
        third_id = "MS4wLjABAAAATestAuthorSecUid0003"
        third = Path(self.stage(third_id, nickname="同名")["author_doc"])
        self.assertEqual(third.name, f"同名-{docs.suffix_for(third_id)}.md")
        self.assertEqual(set(docs.author_docs(self.vault, self.config)), {AUTHOR, OTHER_AUTHOR, third_id})
        state = docs.load_state(self.vault, self.config)
        self.assertEqual(state["authors"][OTHER_AUTHOR]["doc_path"], "抖音作者/" + second.name)

    def test_unknown_listing_is_labelled_unchecked(self):
        doc = Path(self.stage(nickname="甲")["author_doc"])
        self.assertIn("清单未核对", doc.read_text())
        self.assertEqual(library.read_note(doc)[0]["listing_status"], "未核对")

    def test_unknown_author_page(self):
        changes = {}
        path = docs.render_unknown(self.vault, self.config, [{"id": A, "path": "知识库/x.md", "title": "t",
                                                              "url": f"https://www.douyin.com/video/{A}",
                                                              "reason": "f2_anonymous:http_403"}], changes)
        self.assertIn("http_403", changes[path].decode())

    def test_insert_author_keys_keeps_body_bytes(self):
        note = helpers.existing_note(self.vault, A)
        raw = note.read_bytes()
        new = docs.insert_author_keys(raw, '作者: "引号" #1', AUTHOR)
        front, body = library.read_note_text(new.decode())
        old_front, old_body = library.read_note_text(raw.decode())
        self.assertEqual(body, old_body)
        self.assertEqual(front, dict(old_front, douyin_author='作者: "引号" #1', douyin_author_id=AUTHOR))
        self.assertTrue(new.endswith(raw[raw.index(b"\n---\n", 3):]))
        self.assertEqual(list(front)[:5], ["source_url", "title", "aweme_id", "douyin_author", "douyin_author_id"])
        with self.assertRaises(ValueError):
            docs.insert_author_keys(new, "x", AUTHOR)
        with self.assertRaises(ValueError):
            docs.insert_author_keys(raw, "x", "bad id")


if __name__ == "__main__":
    unittest.main()
