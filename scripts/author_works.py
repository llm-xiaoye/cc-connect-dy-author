#!/usr/bin/env python3
"""List one Douyin work's author and every publicly visible work (videos and image posts).

Ported from douyin-author-links/list_links.py into dy-author (no runtime link to it).
Differences: image posts are kept as /note/ items, image seeds are accepted, and the
pinned F2 dev branch adds the x-tt-argus/uifid gateway headers (issue #443).
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import browser_cache
import douyin_access
import pipeline

OFFICIAL_HOSTS = pipeline.OFFICIAL_HOSTS
URL_RE = re.compile(r"https?://[^\s<>\"'，。、()\[\]]+")
PAGE_SIZE = 20
MAX_PAGES = 1000
# F2 dl.py documents 0/4/51/53/55/61/66/109/201 as video-shaped and 68 as image posts.
VIDEO_TYPES = {0, 4, 51, 53, 55, 61, 66, 109, 201}
IMAGE_TYPES = {68}
# 163 is a Douyin 文章 (long text post). Its video.play_addr is only background music and
# the feed carries a truncated abstract, so it is listed but not imported.
ARTICLE_TYPES = {163}
SEC_UID_RE = re.compile(r"[A-Za-z0-9_-]{20,120}")


@dataclass
class Listing:
    items: list = field(default_factory=list)
    complete: bool = False
    reason: str | None = None
    pages: int = 0
    author_id: str | None = None
    nickname: str | None = None
    access: str = "anonymous"
    profile_items: int | None = None
    source_id: str | None = None
    source_kind: str | None = None

    @property
    def ids(self):
        return [item["id"] for item in self.items]


class PageError(ValueError):
    """A page cannot safely contribute to an all-works claim."""


class CacheLookupIncomplete(RuntimeError):
    """A bounded Chrome cache scan cannot establish absence of a matching detail."""


def work_url(item):
    path = "note" if item["kind"] == "note" else "video"
    return f"https://www.douyin.com/{path}/{item['id']}"


def select_official_url(text: str) -> str:
    urls = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;!?！）)]}")
        try:
            host = urlsplit(url).hostname
        except ValueError:
            continue
        if host and host.lower() in OFFICIAL_HOSTS and url not in urls:
            urls.append(url)
    if len(urls) != 1:
        raise ValueError("请提供恰好一条抖音官方作品链接。")
    return urls[0]


def item_kind(item: dict) -> str:
    media_type = item.get("aweme_type")
    if type(media_type) is str and media_type.isdecimal():
        media_type = int(media_type)
    if media_type in ARTICLE_TYPES:
        return "article"
    if item.get("images") or item.get("image_post_info") or media_type in IMAGE_TYPES:
        return "note"
    if type(media_type) is int and media_type in VIDEO_TYPES and isinstance(item.get("video"), dict) and item["video"]:
        return "video"
    raise PageError("unknown_media_type")


def parse_page(raw: object, author_id: str) -> tuple[list[dict], bool, int | None]:
    if not isinstance(raw, dict) or raw.get("status_code") != 0:
        raise PageError("api_rejected")
    items = raw.get("aweme_list")
    if not isinstance(items, list):
        raise PageError("missing_aweme_list")
    more = raw.get("has_more")
    if type(more) is bool:
        has_more = more
    elif type(more) is int and more in (0, 1):
        has_more = bool(more)
    else:
        raise PageError("invalid_has_more")
    cursor = None
    if has_more:
        cursor = raw.get("max_cursor")
        if type(cursor) is str and cursor.isdecimal():
            cursor = int(cursor)
        if type(cursor) is not int or cursor < 0:
            raise PageError("invalid_cursor")
    works = []
    for item in items:
        if not isinstance(item, dict):
            raise PageError("invalid_item")
        author = item.get("author")
        if not isinstance(author, dict) or author.get("sec_uid") != author_id:
            raise PageError("author_mismatch")
        try:
            aweme_id = pipeline.video_id(item.get("aweme_id"))
        except (TypeError, ValueError):
            raise PageError("invalid_aweme_id") from None
        works.append({"id": aweme_id, "kind": item_kind(item),
                      "title": str(item.get("desc") or "")[:200]})
    return works, has_more, cursor


async def scan_pages(fetch_page, author_id: str, *, max_pages: int = MAX_PAGES,
                     pause=asyncio.sleep) -> Listing:
    items, seen_ids, seen_cursors = [], set(), {0}
    cursor = 0
    for page_number in range(1, max_pages + 1):
        try:
            raw = await fetch_page(cursor)
        except PageError as exc:
            return Listing(items, False, str(exc), page_number - 1, author_id)
        except Exception as exc:
            return Listing(items, False, f"request_failed:{douyin_access.error_code(exc)}",
                           page_number - 1, author_id)
        try:
            page_items, has_more, next_cursor = parse_page(raw, author_id)
        except PageError as exc:
            return Listing(items, False, str(exc), page_number - 1, author_id)
        for item in page_items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                items.append(item)
        if not has_more:
            return Listing(items, True, None, page_number, author_id)
        if next_cursor in seen_cursors:
            return Listing(items, False, "cursor_not_advancing", page_number, author_id)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if page_number < max_pages:
            await pause(2)
    return Listing(items, False, "page_limit", max_pages, author_id)


async def fetch_author_posts(author_id: str, jar: CookieJar, *, max_pages: int) -> Listing:
    douyin_access.quiet_f2()
    from f2.apps.douyin.crawler import DouyinCrawler
    from f2.apps.douyin.model import UserPost

    class StrictPostCrawler(DouyinCrawler):
        """Keep F2's endpoint, signing and gateway headers; preserve the HTTP failure category."""

        async def _fetch_get_json(self, endpoint: str) -> dict:
            try:
                response = await self.aclient.get(
                    endpoint, headers=self.crawler_headers, follow_redirects=True)
            except Exception as exc:
                raise PageError(f"request_failed:{type(exc).__name__}") from None
            if response.status_code == 403 and b"Uifid Not Found" in response.content[:300]:
                raise PageError("http_403_argus_uifid_not_found")
            if response.status_code == 429:
                raise PageError("http_429_rate_limited")
            if response.status_code != 200:
                raise PageError(f"http_{response.status_code}")
            if not response.content:
                raise PageError("empty_response")
            try:
                return response.json()
            except (ValueError, UnicodeError):
                raise PageError("invalid_json_response") from None

    async with StrictPostCrawler(douyin_access.crawler_options(jar)) as crawler:
        async def fetch_page(cursor: int):
            return await crawler.fetch_user_post(
                UserPost(max_cursor=cursor, count=PAGE_SIZE, sec_user_id=author_id))

        return await scan_pages(fetch_page, author_id, max_pages=max_pages)


def detail_author(detail, aweme_id):
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != aweme_id:
        return None
    author = detail.get("author")
    if not isinstance(author, dict) or not isinstance(author.get("sec_uid"), str) or not author["sec_uid"]:
        return None
    nickname = author.get("nickname") if isinstance(author.get("nickname"), str) else None
    return author["sec_uid"], nickname


async def one_attempt(aweme_id: str, jar: CookieJar, access: str, *, max_pages: int,
                      detail_override: dict | None = None) -> Listing:
    if detail_override is None:
        try:
            detail = await douyin_access.api_detail(aweme_id, jar)
        except Exception as exc:
            return Listing([], False, f"detail_failed:{douyin_access.error_code(exc)}", access=access)
    else:
        detail = detail_override
    if not isinstance(detail, dict):
        return Listing([], False, "detail_unavailable", access=access)
    if str(detail.get("aweme_id")) != aweme_id:
        return Listing([], False, "detail_id_mismatch", access=access)
    found = detail_author(detail, aweme_id)
    if not found:
        return Listing([], False, "author_unavailable", access=access)
    author_id, nickname = found
    try:
        listing = await fetch_author_posts(author_id, jar, max_pages=max_pages)
    except Exception as exc:
        return Listing([], False, f"request_failed:{type(exc).__name__}", author_id=author_id,
                       nickname=nickname, access=access)
    listing.nickname = nickname
    listing.access = access
    return listing


def cached_detail(aweme_id: str, config: dict, *, open_if_missing: bool = False,
                  source_url: str | None = None) -> dict | None:
    if config.get("browser") != "chrome":
        return None
    root = config.get("browser_cache_dir")
    diagnostics = {}
    detail = browser_cache.find_detail(aweme_id, "chrome", root, timeout=12, diagnostics=diagnostics)
    if detail or not open_if_missing or not config.get("auto_browser_recovery"):
        if not detail and (diagnostics.get("scan_incomplete") or diagnostics.get("deadline_reached")):
            raise CacheLookupIncomplete("CACHE_LOOKUP_INCOMPLETE")
        return detail
    baseline = browser_cache.snapshot_entries("chrome", root)
    track = bool(config.get("close_opened_tabs"))
    before = douyin_access.douyin_tab_ids(aweme_id, "chrome") if track else None
    try:
        douyin_access.refresh_browser_session(
            aweme_id, "chrome", config.get("browser_recovery_wait_seconds", 8),
            source_url=source_url, profile=config.get("browser_profile"))
        detail = browser_cache.wait_for_detail(
            aweme_id, "chrome", root, timeout=config.get("browser_media_timeout_seconds", 40),
            baseline=baseline, diagnostics=diagnostics)
    finally:
        if track:
            # Detail (if any) is already read from Chrome's cache; the tab is no longer needed.
            douyin_access.close_new_tabs(aweme_id, before, "chrome")
    if not detail and (diagnostics.get("scan_incomplete") or diagnostics.get("deadline_reached")):
        raise CacheLookupIncomplete("CACHE_LOOKUP_INCOMPLETE")
    return detail


def require_seed_in_complete_listing(listing: Listing, aweme_id: str) -> Listing:
    if listing.complete and aweme_id not in listing.ids:
        listing.complete = False
        listing.reason = "seed_not_in_profile_feed"
    return listing


async def enumerate_work(aweme_id: str, config: dict, *, max_pages: int,
                         source_url: str | None = None) -> Listing:
    anonymous = require_seed_in_complete_listing(
        await one_attempt(aweme_id, CookieJar(), "anonymous", max_pages=max_pages), aweme_id)
    if anonymous.complete:
        return anonymous
    detail = None
    cache_issue = None
    if not anonymous.author_id:
        try:
            detail = cached_detail(aweme_id, config)
        except CacheLookupIncomplete:
            cache_issue = "CACHE_LOOKUP_INCOMPLETE"
        except Exception:
            detail = None
        if detail:
            anonymous = require_seed_in_complete_listing(
                await one_attempt(aweme_id, CookieJar(), "anonymous", max_pages=max_pages,
                                  detail_override=detail), aweme_id)
            if anonymous.complete:
                return anonymous
    browser = config.get("browser")
    if browser != "chrome":
        anonymous.reason = f"anonymous:{anonymous.reason}; chrome:not_configured"
        if cache_issue:
            anonymous.reason += f"; cache:{cache_issue}"
        return anonymous
    try:
        jar = douyin_access.browser_cookies(browser, config.get("browser_profile"))
    except Exception as exc:
        anonymous.reason = f"anonymous:{anonymous.reason}; chrome_cookies:{type(exc).__name__}"
        if cache_issue:
            anonymous.reason += f"; cache:{cache_issue}"
        return anonymous
    authenticated = require_seed_in_complete_listing(
        await one_attempt(aweme_id, jar, "chrome", max_pages=max_pages, detail_override=detail), aweme_id)
    if not authenticated.author_id:
        try:
            detail = cached_detail(aweme_id, config, open_if_missing=True, source_url=source_url)
        except CacheLookupIncomplete:
            cache_issue = "CACHE_LOOKUP_INCOMPLETE"
        except Exception:
            detail = None
        if detail:
            authenticated = require_seed_in_complete_listing(
                await one_attempt(aweme_id, jar, "chrome", max_pages=max_pages,
                                  detail_override=detail), aweme_id)
    if authenticated.complete:
        if anonymous.author_id and authenticated.author_id != anonymous.author_id:
            anonymous.reason = "author_changed_between_attempts"
            return anonymous
        return authenticated
    if authenticated.author_id and anonymous.author_id and authenticated.author_id != anonymous.author_id:
        anonymous.reason = "author_changed_between_attempts"
        return anonymous
    chosen = authenticated if len(authenticated.items) > len(anonymous.items) or (
        authenticated.author_id and not anonymous.author_id) else anonymous
    chosen.reason = f"anonymous:{anonymous.reason}; chrome:{authenticated.reason}"
    if cache_issue:
        chosen.reason += f"; cache:{cache_issue}"
    return chosen


def browser_observation_listing(source_id: str, author_id: str, observed_links_file: Path,
                                profile_count: int, reached_end: bool,
                                nickname: str | None = None) -> Listing:
    """Validate works observed on the exact author's Chrome profile page (videos and notes)."""
    seed_id = pipeline.video_id(source_id)
    if not SEC_UID_RE.fullmatch(author_id):
        raise ValueError("博主 sec_uid 格式错误。")
    if not 0 <= profile_count <= 100000:
        raise ValueError("主页作品数量超出允许范围。")
    path = Path(observed_links_file).expanduser()
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("浏览器观察文件过大。")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) > 10000:
        raise ValueError("浏览器观察条目过多。")
    items, seen = [], set()
    for line in lines:
        parsed = urlsplit(line.strip())
        match = re.fullmatch(r"/(video|note)/(\d{16,22})", parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "www.douyin.com"
                or not match or parsed.fragment):
            raise ValueError("浏览器观察文件包含非官方作品链接。")
        kind, work_id = match.groups()
        if work_id in seen:
            raise ValueError("浏览器观察文件包含重复作品 ID。")
        seen.add(work_id)
        items.append({"id": work_id, "kind": kind, "title": ""})
    reason = None
    if not reached_end:
        reason = "browser_end_not_observed"
    elif len(seen) != profile_count:
        reason = "browser_count_mismatch"
    elif seed_id not in seen:
        reason = "seed_not_in_profile_feed"
    return Listing(items, reason is None, reason, 0, author_id, nickname,
                   "chrome_profile_ui", profile_count)


def verified_prior_result(path: Path, source_id: str, author_id: str) -> dict:
    prior_path = Path(path).expanduser().resolve()
    if prior_path.stat().st_size > 1024 * 1024:
        raise ValueError("首次运行结果文件过大。")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    if (not isinstance(prior, dict) or prior.get("result_file") != str(prior_path)
            or prior.get("source_id") != source_id or prior.get("author_id") != author_id):
        raise ValueError("浏览器主页与输入作品的首次作者识别结果不一致。")
    return prior


def save_listing(listing: Listing, config: dict, *, output_dir: Path | None = None) -> dict:
    if output_dir is None:
        root = Path(config["cache_dir"]).expanduser().resolve() / "listings"
        root.mkdir(parents=True, exist_ok=True)
        output_dir = root / uuid4().hex
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    links_file = output_dir / "links.txt"
    result_file = output_dir / "result.json"
    links = [work_url(item) for item in listing.items]
    links_file.write_text("".join(link + "\n" for link in links), encoding="utf-8")
    result = {
        "complete": listing.complete, "reason": listing.reason, "pages": listing.pages,
        "author_id": listing.author_id, "nickname": listing.nickname, "access": listing.access,
        "source_id": listing.source_id, "source_kind": listing.source_kind,
        "count": len(listing.items),
        "videos": sum(item["kind"] == "video" for item in listing.items),
        "notes": sum(item["kind"] == "note" for item in listing.items),
        "articles": sum(item["kind"] == "article" for item in listing.items),
        "items": listing.items, "profile_items": listing.profile_items,
        "links_file": str(links_file.resolve()), "result_file": str(result_file.resolve()),
    }
    pipeline.write_json(result_file, result)
    return result


async def run(source: str, config: dict, *, max_pages: int = MAX_PAGES,
              output_dir: Path | None = None) -> dict:
    selected = select_official_url(source)
    try:
        canonical, aweme_id = pipeline.normalize_url(selected)
    except Exception as exc:
        listing = Listing([], False, f"source_unavailable:{type(exc).__name__}")
    else:
        listing = await enumerate_work(aweme_id, config, max_pages=max_pages, source_url=canonical)
        listing.source_id = aweme_id
        listing.source_kind = "note" if "/note/" in canonical else "video"
        seed = next((item for item in listing.items if item["id"] == aweme_id), None)
        if seed:
            listing.source_kind = seed["kind"]
    return save_listing(listing, config, output_dir=output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="一条抖音作品链接或完整分享文本")
    parser.add_argument("--config", default=str(pipeline.DEFAULT_CONFIG))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--browser-observed-links", type=Path)
    parser.add_argument("--prior-result", type=Path)
    parser.add_argument("--profile-author-id")
    parser.add_argument("--profile-count", type=int)
    parser.add_argument("--profile-end", action="store_true")
    parser.add_argument("--profile-nickname")
    args = parser.parse_args()
    if not 1 <= args.max_pages <= MAX_PAGES:
        parser.error(f"--max-pages 必须在 1–{MAX_PAGES} 之间。")
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.browser_observed_links is not None:
            if args.prior_result is None or args.profile_author_id is None or args.profile_count is None:
                parser.error("浏览器观察模式需要 --prior-result、--profile-author-id 和 --profile-count。")
            canonical, aweme_id = pipeline.normalize_url(select_official_url(args.source))
            prior = verified_prior_result(args.prior_result, aweme_id, args.profile_author_id)
            listing = browser_observation_listing(
                aweme_id, args.profile_author_id, args.browser_observed_links, args.profile_count,
                args.profile_end, args.profile_nickname or prior.get("nickname"))
            listing.source_id = aweme_id
            listing.source_kind = "note" if "/note/" in canonical else "video"
            result = save_listing(listing, config, output_dir=args.output_dir)
        else:
            result = asyncio.run(run(args.source, config, max_pages=args.max_pages,
                                     output_dir=args.output_dir))
    except (ValueError, OSError, KeyError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    summary = {k: result[k] for k in ("complete", "reason", "author_id", "nickname", "access",
                                      "source_id", "count", "videos", "notes", "articles", "result_file")}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
