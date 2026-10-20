import re
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from PIL import Image

import helpers
from helpers import analysis
import library
import note_assets
import pipeline
import unified_store

VID = "7652616197779915496"
BODY = ("> 一句话结论：关系和有效时间要一起解释。\n\n## 核心要点\n\n- 一\n- 二\n- 三\n\n"
        "## 机制\n\n解释第一张图。\n\n{{DY_IMAGE:1}}\n\n## 应用\n\n解释第二张图。\n\n{{DY_IMAGE:2}}\n\n"
        "## 模型补充\n\n### 补充知识\n\n补充。\n\n### 纠错与边界\n\n边界。\n\n### 模型判断\n\n判断。\n\n"
        "## 自测\n\n题与答。\n")


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.cache = self.root / "work"
        self.cache.mkdir()
        self.config = helpers.make_config(self.root, self.vault)
        entries = []
        for page in [1, 2]:
            original = self.cache / f"{page}.webp"
            Image.new("RGB", (8, 8), (page * 50, 20, 30)).save(original, lossless=True)
            display = original.with_suffix(".png")
            with Image.open(original) as im:
                im.convert("RGB").save(display)
            entries.append({"page": page, "file": display.name, "sha256": note_assets.digest(display.read_bytes())})
        self.bundle = {"aweme_id": VID, "source_url": f"https://www.douyin.com/note/{VID}", "media_kind": "image",
                       "image_count": 2, "images": entries}
        raw = {"aweme_id": VID, "source_kind": "image", "text": "图像知识", "pages": [dict(e, text="测试知识") for e in entries]}
        pipeline.write_json(self.cache / "bundle.json", self.bundle)
        pipeline.write_json(self.cache / "transcript.json", raw)
        self.analysis = analysis(title="图记忆的机制", reviewed_pages=[1, 2])
        pipeline.write_json(self.cache / "analysis.json", self.analysis)
        (self.cache / "knowledge.md").write_text(BODY, encoding="utf-8")

    def save(self):
        return pipeline.save_bundle(self.cache / "bundle.json", self.cache / "analysis.json",
                                    self.cache / "knowledge.md", self.config)

    def files(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*")
                if p.is_file() and not p.name.startswith(".")}

    def test_original_images_inline_and_duplicate_is_noop(self):
        note = Path(self.save()["note"])
        targets = [(note.parent / unquote(u)).resolve() for u in re.findall(r"!\[原图 \d+\]\(([^)]+)\)", note.read_text())]
        self.assertEqual(len(targets), 2)
        for page, path in enumerate(targets, 1):
            self.assertTrue(path.is_relative_to(self.vault / "附件"))
            self.assertEqual(path.read_bytes(), (self.cache / f"{page}.webp").read_bytes())
        snapshot = self.files()
        self.assertEqual(self.save()["status"], "exists")
        self.assertEqual(snapshot, self.files())

    def test_missing_duplicate_fenced_or_unknown_markers_do_not_write(self):
        baseline = self.files()
        for body in [BODY.replace("{{DY_IMAGE:2}}", ""), BODY.replace("## 自测", "{{DY_IMAGE:1}}\n\n## 自测"),
                     BODY.replace("{{DY_IMAGE:1}}", "```\n{{DY_IMAGE:1}}\n```"),
                     BODY.replace("{{DY_IMAGE:2}}", "{{DY_IMAGE:3}}")]:
            (self.cache / "knowledge.md").write_text(body, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.save()
            self.assertEqual(self.files(), baseline)

    def test_metadata_failure_rolls_back_images_and_note(self):
        baseline = self.files()
        original = library.atomic_write

        def broken(path, content, exclusive=False):
            if path.name == unified_store.INDEX_NAME:
                raise OSError("disk failure")
            return original(path, content, exclusive)
        with patch.object(library, "atomic_write", side_effect=broken):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.files(), baseline)
        self.assertFalse((self.vault / ".dy-transaction.json").exists())

    def test_reviewed_pages_required(self):
        pipeline.write_json(self.cache / "analysis.json", dict(self.analysis, reviewed_pages=[1]))
        with self.assertRaisesRegex(ValueError, "reviewed_pages"):
            self.save()


if __name__ == "__main__":
    unittest.main()
