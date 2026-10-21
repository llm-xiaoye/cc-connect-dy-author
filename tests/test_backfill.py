import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import helpers
from helpers import AUTHOR, OTHER_AUTHOR
import author_docs
import backfill_authors as bf
import library
import pipeline
import unified_store

A = "7659111274168454401"
B = "7659111274168454402"
C = "7659111274168454403"


class FakeLookup:
    def __init__(self, table, limit_at=None):
        self.table, self.limit_at, self.calls = table, limit_at, []

    def __call__(self, vid):
        self.calls.append(vid)
        if vid == self.limit_at:
            raise bf.RateLimited(vid)
        if vid in self.table:
            uid, name = self.table[vid]
            return {"id": vid, "sec_uid": uid, "nickname": name, "route": "fake"}
        return {"id": vid, "sec_uid": None, "reason": "f2_anonymous:http_403"}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.vault = helpers.make_vault(self.root)
        self.config = helpers.make_config(self.root, self.vault)
        self.out = self.root / "migration" / "backfill"
        self.notes = {vid: helpers.existing_note(self.vault, vid, f"知识库/AI/x/{vid}.md") for vid in (A, B, C)}
        self.catalogs = {p: p.read_bytes() for p in (self.vault / unified_store.INDEX_NAME,
                                                      self.vault / unified_store.LOG_NAME)}

    def test_preview_apply_verify_changes_only_front_matter(self):
        lookup = FakeLookup({A: (AUTHOR, "甲"), B: (AUTHOR, "甲")})
        preview = bf.preview(self.config, self.out, lookup=lookup)
        self.assertEqual((preview["resolved"], preview["unresolved"], preview["authors"]), (2, 1, 1))
        self.assertIn("未能核实", (self.out / "preview.md").read_text())
        self.assertEqual({p: p.read_bytes() for p in self.notes.values()},
                         {p: p.read_bytes() for p in self.notes.values()})
        bodies = {vid: library.read_note(p)[1] for vid, p in self.notes.items()}
        result = bf.apply(self.config, self.out)
        self.assertEqual((result["updated_notes"], result["author_docs"], result["unknown"]), (2, 1, 1))
        for vid, path in self.notes.items():
            self.assertEqual(library.read_note(path)[1], bodies[vid])
        self.assertEqual(library.read_note(self.notes[A])[0]["douyin_author_id"], AUTHOR)
        self.assertNotIn("douyin_author_id", library.read_note(self.notes[C])[0])
        self.assertTrue((self.out / "before" / f"知识库/AI/x/{A}.md").is_file())
        self.assertEqual(self.catalogs, {p: p.read_bytes() for p in self.catalogs})
        self.assertTrue((self.vault / "抖音作者" / "未识别作者.md").is_file())
        doc = author_docs.author_docs(self.vault, self.config)[AUTHOR]
        self.assertIn("已入库 2", doc.read_text())
        self.assertEqual(bf.verify(self.config, self.out)["status"], "ok")
        with self.assertRaisesRegex(ValueError, "已执行过"):
            bf.apply(self.config, self.out)

    def test_note_changed_after_preview_blocks_apply(self):
        bf.preview(self.config, self.out, lookup=FakeLookup({A: (AUTHOR, "甲")}))
        self.notes[A].write_text(self.notes[A].read_text() + "改动\n")
        with self.assertRaisesRegex(ValueError, "重新 preview"):
            bf.apply(self.config, self.out)
        self.assertNotIn("douyin_author_id", library.read_note(self.notes[B])[0])

    def test_rate_limit_stops_lookups_and_reviewed_notes_are_skipped(self):
        state = self.vault / ".dy-author-review-state.json"
        state.write_text(json.dumps({"version": 1, "items": {C: {"initial_status": "未复审", "review": {
            "outcome": "复审通过", "note_sha256": "0" * 64, "reviewed_at": "2026-09-01"}}}}))
        lookup = FakeLookup({A: (AUTHOR, "甲"), B: (OTHER_AUTHOR, "乙")}, limit_at=A)
        preview = bf.preview(self.config, self.out, lookup=lookup)
        self.assertEqual(preview["rate_limited_at"], A)
        self.assertEqual(lookup.calls, [A])
        self.assertEqual(preview["skipped_reviewed"], 1)
        self.assertEqual(preview["resolved"], 0)

    def test_transaction_failure_rolls_back(self):
        bf.preview(self.config, self.out, lookup=FakeLookup({A: (AUTHOR, "甲")}))
        before = {p: p.read_bytes() for p in self.notes.values()}
        original = library.atomic_write

        def broken(path, content, exclusive=False):
            if path.name == author_docs.STATE_NAME:
                raise OSError("disk failure")
            return original(path, content, exclusive)
        with patch.object(library, "atomic_write", side_effect=broken), self.assertRaises(OSError):
            bf.apply(self.config, self.out)
        self.assertEqual(before, {p: p.read_bytes() for p in self.notes.values()})
        self.assertFalse((self.vault / "抖音作者").exists() and list((self.vault / "抖音作者").glob("*.md")))


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.temp_root(self)
        self.config = helpers.make_config(self.root, self.root / "vault", browser=None)

    def test_listing_route_cache_reuse_and_f2(self):
        listing_dir = Path(self.config["cache_dir"]) / "listings" / "x"
        pipeline.write_json(listing_dir / "result.json", {"author_id": AUTHOR, "nickname": "甲",
                                                          "items": [{"id": A, "kind": "video"}]})
        pauses = []
        lookup = bf.Lookup(self.config, pause=pauses.append)
        self.assertEqual(lookup(A)["route"], "author_listing:x")
        self.assertTrue(lookup(A)["reused"])
        detail = {"aweme_id": B, "author": {"sec_uid": OTHER_AUTHOR, "nickname": "乙"}}
        with patch.object(bf.douyin_access, "api_detail", new=AsyncMock(side_effect=[detail, None])):
            self.assertEqual(lookup(B)["route"], "f2_anonymous_detail")
            missing = lookup(C)
        self.assertIsNone(missing["sec_uid"])
        self.assertIn("f2_anonymous", missing["reason"])
        self.assertEqual(pauses, [bf.PAUSE_SECONDS])

    def test_rate_limit_raises(self):
        class Limited(Exception):
            status_code = 429
        lookup = bf.Lookup(self.config, pause=lambda _: None)
        with patch.object(bf.douyin_access, "api_detail", new=AsyncMock(side_effect=Limited())):
            with self.assertRaises(bf.RateLimited):
                lookup(A)

    def test_mismatched_detail_is_not_accepted(self):
        lookup = bf.Lookup(self.config, pause=lambda _: None)
        wrong = {"aweme_id": B, "author": {"sec_uid": AUTHOR}}
        with patch.object(bf.douyin_access, "api_detail", new=AsyncMock(return_value=wrong)):
            self.assertIsNone(lookup(A)["sec_uid"])


if __name__ == "__main__":
    unittest.main()
