import asyncio
from http.cookiejar import CookieJar
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import helpers
import author_works as works

AUTHOR = "MS4wLjABAAAATestAuthor"
VIDEO_A = "7659111274168454446"
VIDEO_B = "7659111274168454447"
IMAGE = "7659111274168454448"


def video(aweme_id=VIDEO_A, *, author=AUTHOR, aweme_type=0):
    return {"aweme_id": aweme_id, "author": {"sec_uid": author}, "aweme_type": aweme_type, "desc": "标题",
            "video": {"play_addr": {"url_list": ["https://example.test/media"]}}}


def image(aweme_id=IMAGE):
    return {"aweme_id": aweme_id, "author": {"sec_uid": AUTHOR}, "aweme_type": 68, "images": [{"url": "x"}]}


async def no_pause(_):
    return None


def items(*pairs):
    return [{"id": i, "kind": k, "title": ""} for i, k in pairs]


class AuthorWorksTests(unittest.IsolatedAsyncioTestCase):
    def test_share_text_selects_only_one_official_link(self):
        text = "8.41 AGI:/ [z@T.YZ](mailto:z@T.YZ) https://example.com/other https://v.douyin.com/Rw73nGIP8xU/ 复制此链接"
        self.assertEqual(works.select_official_url(text), "https://v.douyin.com/Rw73nGIP8xU/")
        luma = ("6.99 复制打开抖音，看看【LumaAgent的作品】面试官：业务问为什么跌了，Agent怎么查数不翻车... "
                "https://v.douyin.com/jsmJLifDj0o/ K@W.mQ :3pm 02/28 BGV:/")
        self.assertEqual(works.select_official_url(luma), "https://v.douyin.com/jsmJLifDj0o/")
        with self.assertRaises(ValueError):
            works.select_official_url("https://v.douyin.com/one/ https://v.douyin.com/two/")

    async def test_pagination_dedups_and_keeps_image_posts(self):
        pages = {0: {"status_code": 0, "has_more": 1, "max_cursor": 100,
                     "aweme_list": [video(VIDEO_A), image(), video(VIDEO_A)]},
                 100: {"status_code": 0, "has_more": 0, "max_cursor": 90,
                       "aweme_list": [video(VIDEO_A), video(VIDEO_B, aweme_type=66)]}}
        cursors = []

        async def fetch(cursor):
            cursors.append(cursor)
            return pages[cursor]
        result = await works.scan_pages(fetch, AUTHOR, pause=no_pause)
        self.assertTrue(result.complete)
        self.assertEqual(cursors, [0, 100])
        self.assertEqual([(i["id"], i["kind"]) for i in result.items],
                         [(VIDEO_A, "video"), (IMAGE, "note"), (VIDEO_B, "video")])
        self.assertEqual(works.work_url(result.items[1]), f"https://www.douyin.com/note/{IMAGE}")

    async def test_partial_reasons(self):
        async def stall(_):
            return {"status_code": 0, "has_more": True, "max_cursor": 0, "aweme_list": [video()]}
        self.assertEqual((await works.scan_pages(stall, AUTHOR)).reason, "cursor_not_advancing")

        async def forbidden(_):
            raise works.PageError("http_403_argus_uifid_not_found")
        result = await works.scan_pages(forbidden, AUTHOR)
        self.assertEqual((result.complete, result.reason, result.pages), (False, "http_403_argus_uifid_not_found", 0))

        class RateLimit(Exception):
            status_code = 429

        async def limited(_):
            raise RateLimit()
        self.assertEqual((await works.scan_pages(limited, AUTHOR)).reason, "request_failed:http_429")

        async def mismatch(cursor):
            return {"status_code": 0, "has_more": cursor == 0, "max_cursor": 100,
                    "aweme_list": [video(VIDEO_A) if cursor == 0 else video(VIDEO_B, author="other")]}
        result = await works.scan_pages(mismatch, AUTHOR, pause=no_pause)
        self.assertEqual((result.reason, result.ids), ("author_mismatch", [VIDEO_A]))

        async def endless(cursor):
            return {"status_code": 0, "has_more": True, "max_cursor": cursor + 1, "aweme_list": [video()]}
        self.assertEqual((await works.scan_pages(endless, AUTHOR, max_pages=1)).reason, "page_limit")

    def test_unknown_media_type_is_not_silently_listed(self):
        for item in (video(aweme_type=101), dict(video(), video={})):
            with self.subTest(item["aweme_type"]), self.assertRaisesRegex(works.PageError, "unknown_media_type"):
                works.parse_page({"status_code": 0, "has_more": False, "aweme_list": [item]}, AUTHOR)

    def test_article_type_is_listed_as_article_not_video(self):
        # A 文章 carries a video dict whose play_addr is only background music.
        article = dict(video(VIDEO_B), aweme_type=163,
                       video={"play_addr": {"url_list": ["https://music.example/bgm.mp3"]}, "duration": 0})
        found, more, _ = works.parse_page({"status_code": 0, "has_more": False,
                                           "aweme_list": [video(VIDEO_A), article]}, AUTHOR)
        self.assertEqual([(w["id"], w["kind"]) for w in found], [(VIDEO_A, "video"), (VIDEO_B, "article")])
        self.assertEqual(works.work_url({"id": VIDEO_B, "kind": "article"}), f"https://www.douyin.com/video/{VIDEO_B}")
        self.assertEqual(works.item_kind(dict(article, aweme_type="163")), "article")

    async def test_image_seed_is_valid_and_missing_seed_is_partial(self):
        listing = works.Listing(items((VIDEO_A, "video"), (IMAGE, "note")), True, None, 1, AUTHOR)
        with patch.object(works, "one_attempt", new=AsyncMock(return_value=listing)):
            result = await works.enumerate_work(IMAGE, {"browser": None}, max_pages=10)
        self.assertTrue(result.complete)
        empty = works.Listing([], True, None, 1, AUTHOR)
        with patch.object(works, "one_attempt", new=AsyncMock(return_value=empty)):
            result = await works.enumerate_work(VIDEO_A, {"browser": None}, max_pages=10)
        self.assertFalse(result.complete)
        self.assertIn("seed_not_in_profile_feed", result.reason)

    async def test_anonymous_failure_falls_back_to_chrome(self):
        anonymous = works.Listing(items((VIDEO_A, "video")), False, "api_rejected", 1, AUTHOR)
        chrome = works.Listing(items((VIDEO_A, "video"), (VIDEO_B, "video")), True, None, 2, AUTHOR)
        with patch.object(works, "one_attempt", new=AsyncMock(side_effect=[anonymous, chrome])) as attempt:
            with patch.object(works.douyin_access, "browser_cookies", return_value=CookieJar()) as cookies:
                result = await works.enumerate_work(VIDEO_A, {"browser": "chrome", "browser_profile": "/tmp/p"},
                                                    max_pages=10)
        self.assertTrue(result.complete)
        self.assertEqual(attempt.await_count, 2)
        cookies.assert_called_once_with("chrome", "/tmp/p")

    async def test_author_change_between_attempts_is_rejected(self):
        anonymous = works.Listing(items((VIDEO_A, "video")), False, "api_rejected", 1, AUTHOR)
        chrome = works.Listing(items((VIDEO_A, "video")), True, None, 1, "MS4wLjABAAAAOtherAuthor1")
        with patch.object(works, "one_attempt", new=AsyncMock(side_effect=[anonymous, chrome])):
            with patch.object(works.douyin_access, "browser_cookies", return_value=CookieJar()):
                result = await works.enumerate_work(VIDEO_A, {"browser": "chrome"}, max_pages=10)
        self.assertEqual((result.complete, result.reason), (False, "author_changed_between_attempts"))

    async def test_matching_browser_cache_recovers_empty_detail(self):
        missing = works.Listing([], False, "detail_unavailable")
        recovered = works.Listing(items((VIDEO_A, "video")), True, None, 1, AUTHOR)
        detail = {"aweme_id": VIDEO_A, "author": {"sec_uid": AUTHOR}}
        with patch.object(works, "one_attempt", new=AsyncMock(side_effect=[missing, recovered])) as attempt:
            with patch.object(works, "cached_detail", return_value=detail):
                result = await works.enumerate_work(VIDEO_A, {"browser": "chrome"}, max_pages=10)
        self.assertTrue(result.complete)
        self.assertEqual(attempt.await_args_list[1].kwargs["detail_override"], detail)

    def test_incomplete_cache_lookup_does_not_become_detail_absence(self):
        def incomplete(*_args, **kwargs):
            kwargs["diagnostics"]["scan_incomplete"] = True
            return None
        with patch.object(works.browser_cache, "find_detail", side_effect=incomplete):
            with self.assertRaises(works.CacheLookupIncomplete):
                works.cached_detail(VIDEO_A, {"browser": "chrome", "browser_cache_dir": "/tmp/c"})

    def test_browser_observation_keeps_notes_and_checks_counts(self):
        root = helpers.temp_root(self)
        observed = root / "observed.txt"
        observed.write_text(f"https://www.douyin.com/video/{VIDEO_A}\n"
                            f"https://www.douyin.com/note/{IMAGE}?from_tab_name=main\n"
                            f"https://www.douyin.com/video/{VIDEO_B}?count=10\n", encoding="utf-8")
        listing = works.browser_observation_listing(VIDEO_A, AUTHOR, observed, 3, True, "T")
        self.assertTrue(listing.complete)
        self.assertEqual([i["kind"] for i in listing.items], ["video", "note", "video"])
        self.assertEqual(works.browser_observation_listing(VIDEO_A, AUTHOR, observed, 4, True).reason,
                         "browser_count_mismatch")
        self.assertEqual(works.browser_observation_listing(VIDEO_A, AUTHOR, observed, 3, False).reason,
                         "browser_end_not_observed")
        observed.write_text(f"https://example.com/video/{VIDEO_A}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "非官方"):
            works.browser_observation_listing(VIDEO_A, AUTHOR, observed, 1, True)
        observed.write_text(f"https://www.douyin.com/video/{VIDEO_A}\nhttps://www.douyin.com/video/{VIDEO_A}?x=1\n",
                            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "重复作品"):
            works.browser_observation_listing(VIDEO_A, AUTHOR, observed, 2, True)

    def test_saved_listing_binds_prior_result(self):
        root = helpers.temp_root(self)
        listing = works.Listing(items((VIDEO_A, "video"), (IMAGE, "note")), True, None, 1, AUTHOR, "T",
                                source_id=VIDEO_A, source_kind="video")
        result = works.save_listing(listing, {"cache_dir": str(root)}, output_dir=root / "out")
        self.assertEqual((result["videos"], result["notes"]), (1, 1))
        self.assertEqual(Path(result["links_file"]).read_text().splitlines(),
                         [f"https://www.douyin.com/video/{VIDEO_A}", f"https://www.douyin.com/note/{IMAGE}"])
        prior = works.verified_prior_result(Path(result["result_file"]), VIDEO_A, AUTHOR)
        self.assertEqual(prior["nickname"], "T")
        with self.assertRaisesRegex(ValueError, "不一致"):
            works.verified_prior_result(Path(result["result_file"]), VIDEO_B, AUTHOR)

    def test_pinned_f2_sends_gateway_headers(self):
        from f2.apps.douyin.crawler import DouyinCrawler
        from f2.apps.douyin.utils import GatewayHeaderManager
        self.assertEqual(GatewayHeaderManager.gen_gateway_headers(""), {"x-tt-argus": "1"})
        crawler = DouyinCrawler({"headers": {"User-Agent": "UA"}, "cookie": "UIFID=fake-uifid-value; a=b"})
        headers = {k.lower(): v for k, v in crawler.headers.items()}
        self.assertEqual(headers.get("x-tt-argus"), "1")
        self.assertEqual(headers.get("uifid"), "fake-uifid-value")


class IndependenceTests(unittest.TestCase):
    def test_scripts_import_only_bundled_or_declared_modules(self):
        import ast
        import sys
        scripts = Path(works.__file__).resolve().parent
        own = {p.stem for p in scripts.glob("*.py")}
        stdlib_or_deps = {"argparse", "asyncio", "base64", "collections", "concurrent", "contextlib",
                          "dataclasses", "datetime", "fcntl", "functools", "hashlib", "html", "http", "io",
                          "json", "logging", "math", "multiprocessing", "os", "pathlib", "re", "shutil",
                          "struct", "subprocess", "sys", "tempfile", "time", "unicodedata", "urllib",
                          "uuid", "wave", "zlib", "__future__", "yaml", "f2", "yt_dlp", "mlx_whisper",
                          "huggingface_hub", "numpy", "PIL", "imageio_ffmpeg", "brotli"} | sys.stdlib_module_names
        for path in scripts.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertIn(name, own | stdlib_or_deps)
                if isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name) \
                        and node.value.id == "sys":
                    self.fail(f"{path.name} manipulates sys.path")

    def test_public_config_template_is_independent(self):
        config = json.loads((Path(works.__file__).resolve().parents[1] / "config.example.json").read_text())
        self.assertTrue(config["python"].endswith("/dy-author/.venv/bin/python"))
        self.assertTrue(config["cache_dir"].endswith("/dy-author/.cache"))
        self.assertEqual(config["material_cache_dirs"], [])
        self.assertTrue(config["unified_only"])


if __name__ == "__main__":
    unittest.main()
