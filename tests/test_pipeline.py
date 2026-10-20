import unittest
from pathlib import Path

import helpers
from helpers import AUTHOR, OTHER_AUTHOR, analysis, skeleton
import library
import pipeline

VID = "7659111274168454446"


class ReviewAndUrlTests(unittest.TestCase):
    def test_review_sections_normalize_quotes_but_reject_ambiguous_or_missing_headings(self):
        def data(section):
            return {"learning_review": {"learning_goal": "解释原理",
                    "key_claims": [{"claim": "结论", "evidence": "完整转写", "section": section}],
                    "application": "检验", "uncertainties": []}}
        d = data("为什么“不能”")
        pipeline.validate_learning_review(d, '## 为什么"不能"\n内容')
        self.assertEqual(d["learning_review"]["key_claims"][0]["section"], '为什么"不能"')
        for body in ("## 完全不同的主题\n内容", "## 最容易踩的坑：甲\n## 最容易踩的坑：乙\n"):
            with self.assertRaises(ValueError):
                pipeline.validate_learning_review(data("最容易踩的坑"), body)

    def test_url_identity_and_foreign_domain_rejection(self):
        for value in [VID, f"https://jingxuan.douyin.com/m/video/{VID}", f"https://www.douyin.com/note/{VID}",
                      f"复制打开抖音 https://www.douyin.com/?modal_id={VID} 看视频"]:
            self.assertEqual(pipeline.normalize_url(value)[1], VID)
        self.assertTrue(pipeline.normalize_url(f"https://www.douyin.com/note/{VID}")[0].endswith(f"/note/{VID}"))
        with self.assertRaises(ValueError):
            pipeline.normalize_url(f"https://douyin.com.example.com/video/{VID}")

    def test_invalid_transcript_not_success(self):
        for raw in [{"text": ""}, {"text": "t", "segments": [{"start": 9, "end": 2, "text": "t"}]}]:
            with self.assertRaises(ValueError):
                pipeline.validate_transcript(raw)

    def test_detail_metadata_keeps_verified_author_id(self):
        meta = pipeline.detail_metadata({"desc": "d", "author": {"nickname": "N", "sec_uid": AUTHOR}})
        self.assertEqual((meta["author"], meta["author_id"]), ("N", AUTHOR))
        self.assertIsNone(pipeline.detail_metadata({"author": {"nickname": "N"}})["author_id"])


class SkeletonTests(unittest.TestCase):
    TITLE = "记忆的状态与历史"

    def check(self, body, data=None):
        pipeline.validate_note_skeleton(body, self.TITLE, data or analysis())

    def test_valid_skeleton_passes_with_or_without_h1(self):
        self.check(skeleton())
        self.check(f"# {self.TITLE}\n\n" + skeleton())

    def test_each_defect_is_rejected(self):
        good = skeleton()
        cases = {
            "title heading": good.replace("## 机制", f"## {self.TITLE}"),
            "no conclusion": good.replace("> 一句话结论：状态与历史需要分开读取。\n\n", ""),
            "two points": good.replace("- 要点三\n", ""),
            "six points": good.replace("- 要点三\n", "- 三\n- 四\n- 五\n- 六\n"),
            "missing judgement": good.replace("### 模型判断\n\n判断与依据。\n\n", ""),
            "empty boundary": good.replace("边界说明。", ""),
            "no self test": good.split("## 自测")[0],
            "self test not last": good + "\n## 附录\n\n多余。\n",
            "no body section": good.replace("## 机制\n\n具体机制说明。\n\n", ""),
            "personal section": good + "\n## 我的补充\n\n不要写。\n",
            "legacy h1 inside": good.replace("## 机制", "# 机制"),
            "wrong order": good.replace("## 核心要点", "## 临时").replace("## 自测", "## 核心要点\n\n- a\n- b\n- c\n\n## 自测"),
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.check(body)

    def test_judgement_claim_is_required(self):
        data = analysis()
        data["learning_review"]["key_claims"] = data["learning_review"]["key_claims"][:1]
        with self.assertRaisesRegex(ValueError, "模型判断"):
            self.check(skeleton(), data)

    def test_headings_inside_code_fences_are_ignored(self):
        self.check(skeleton(extra="\n\n```markdown\n## 我的补充\n# 标题\n```"))


class SaveBundleTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)
        self.cache = Path(self.config["cache_dir"])
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "knowledge.md").write_text(skeleton(), encoding="utf-8")
        pipeline.write_json(self.work / "analysis.json", analysis())

    def save(self, author=None, bundle_author_id=None):
        bundle = helpers.video_bundle(self.cache, VID, author_id=bundle_author_id)
        return pipeline.save_bundle(bundle, self.work / "analysis.json", self.work / "knowledge.md",
                                    self.config, author=author)

    def test_author_front_order_and_duplicate_and_moved_note(self):
        result = self.save(author={"sec_uid": AUTHOR, "nickname": "作者甲"})
        note = Path(result["note"])
        front, body = library.read_note(note)
        self.assertEqual(list(front)[:5], ["title", "source_url", "aweme_id", "douyin_author", "douyin_author_id"])
        self.assertEqual((front["douyin_author"], front["douyin_author_id"]), ("作者甲", AUTHOR))
        self.assertTrue(body.startswith("# 记忆的状态与历史\n\n> 一句话结论"))
        self.assertTrue(body.rstrip().endswith("## 我的补充"))
        edited = note.read_text(encoding="utf-8") + "\n个人修改。\n"
        moved = self.vault / "知识库" / "用户移动.md"
        note.rename(moved)
        moved.write_text(edited, encoding="utf-8")
        self.assertEqual(self.save()["status"], "exists")
        self.assertEqual(moved.read_text(encoding="utf-8"), edited)

    def test_unknown_author_is_omitted_and_conflict_rejected(self):
        with self.assertRaisesRegex(ValueError, "作者"):
            self.save(author={"sec_uid": AUTHOR, "nickname": "甲"}, bundle_author_id=OTHER_AUTHOR)
        result = self.save()
        front, _ = library.read_note(result["note"])
        self.assertNotIn("douyin_author", front)
        self.assertNotIn("douyin_author_id", front)

    def test_bundle_author_used_when_listing_author_absent(self):
        result = self.save(bundle_author_id=AUTHOR)
        self.assertEqual(library.read_note(result["note"])[0]["douyin_author_id"], AUTHOR)

    def test_invalid_skeleton_writes_nothing(self):
        (self.work / "knowledge.md").write_text("## 原理\n\n旧格式正文。\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.save()
        self.assertFalse(list((self.vault / "知识库").rglob("*.md")) if (self.vault / "知识库").exists() else [])


if __name__ == "__main__":
    unittest.main()


class LocalAsrModelTests(unittest.TestCase):
    def test_existing_directory_is_used_directly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pipeline.local_asr_model(tmp), tmp)

    def test_cached_snapshot_is_preferred_without_network(self):
        from unittest import mock
        with mock.patch("huggingface_hub.snapshot_download", return_value="/cache/snap") as fake:
            self.assertEqual(pipeline.local_asr_model("org/model"), "/cache/snap")
        fake.assert_called_once_with(repo_id="org/model", local_files_only=True)

    def test_missing_snapshot_falls_back_to_repo_id(self):
        from unittest import mock
        with mock.patch("huggingface_hub.snapshot_download", side_effect=OSError("not cached")):
            self.assertEqual(pipeline.local_asr_model("org/model"), "org/model")
