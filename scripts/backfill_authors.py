#!/usr/bin/env python3
"""Backfill douyin_author / douyin_author_id on existing DY notes: preview -> apply -> verify.

Author identity comes only from verified sources, in this order: an earlier
dy-author lookup, a dy-author author listing (every item's author.sec_uid was
checked), Chrome's cached official detail (read-only), F2 anonymous detail, and
F2 detail with the configured Chrome cookies (in memory). No browser is opened.
Only front matter changes; note bodies, catalogs and review state are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime
from http.cookiejar import CookieJar
import hashlib
import json
from pathlib import Path
import sys
import time

import author_docs
import author_works
import browser_cache
import douyin_access
import library
import pipeline
import review_log
import unified_store

PAUSE_SECONDS = 2


class RateLimited(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def lookup_dir(config):
    return Path(config["cache_dir"]).expanduser().resolve() / "author-lookup"


def listing_authors(config):
    """{aweme_id: (sec_uid, nickname, file)} from dy-author listings (author-verified per item)."""
    found = {}
    root = Path(config["cache_dir"]).expanduser().resolve() / "listings"
    for path in sorted(root.glob("*/result.json")) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sec_uid = data.get("author_id")
        if not isinstance(sec_uid, str) or not author_docs.SEC_UID_RE.fullmatch(sec_uid):
            continue
        for item in data.get("items") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found.setdefault(item["id"], (sec_uid, data.get("nickname") or "", path.parent.name))
    return found


def cached_lookup(config, vid):
    path = lookup_dir(config) / f"{vid}.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("id") == vid and author_docs.SEC_UID_RE.fullmatch(str(data.get("sec_uid", ""))):
            return data
    return None


def save_lookup(config, vid, sec_uid, nickname, route):
    record = {"id": vid, "sec_uid": sec_uid, "nickname": nickname or "", "route": route,
              "checked_at": library.now()}
    pipeline.write_json(lookup_dir(config) / f"{vid}.json", record)
    return record


class Lookup:
    """Sequential author lookups with a pause between network requests; 429 stops the run."""

    def __init__(self, config, *, pause=time.sleep, allow_cookies=True, cache_timeout=3):
        self.config = config
        self.pause = pause
        self.allow_cookies = allow_cookies
        # Chrome keeps matching detail responses only ~12 h (browser_cache.MAX_AGE); keep this bounded.
        self.cache_timeout = cache_timeout
        self.listings = listing_authors(config)
        self._jar = None
        self._network_used = False

    def _wait(self):
        if self._network_used:
            self.pause(PAUSE_SECONDS)
        self._network_used = True

    def jar(self):
        if self._jar is None:
            self._jar = douyin_access.browser_cookies(self.config.get("browser"), self.config.get("browser_profile"))
        return self._jar

    def detail(self, vid, jar):
        self._wait()
        try:
            return asyncio.run(douyin_access.api_detail(vid, jar))
        except Exception as exc:
            code = douyin_access.error_code(exc)
            if code == "http_429":
                raise RateLimited(vid) from None
            return {"_error": code}

    def __call__(self, vid):
        cached = cached_lookup(self.config, vid)
        if cached:
            return dict(cached, reused=True)
        if vid in self.listings:
            sec_uid, nickname, name = self.listings[vid]
            return save_lookup(self.config, vid, sec_uid, nickname, f"author_listing:{name}")
        reasons = []
        if self.config.get("browser") == "chrome":
            diagnostics = {}
            try:
                detail = browser_cache.find_detail(vid, "chrome", self.config.get("browser_cache_dir"),
                                                   timeout=self.cache_timeout, diagnostics=diagnostics)
            except Exception as exc:
                detail, reasons = None, reasons + [f"chrome_cache:{type(exc).__name__}"]
            found = author_works.detail_author(detail, vid) if detail else None
            if found:
                return save_lookup(self.config, vid, found[0], found[1], "chrome_cache_detail")
            reasons.append("chrome_cache:incomplete" if diagnostics.get("scan_incomplete") or
                           diagnostics.get("deadline_reached") else "chrome_cache:no_match")
        detail = self.detail(vid, CookieJar())
        found = author_works.detail_author(detail, vid)
        if found:
            return save_lookup(self.config, vid, found[0], found[1], "f2_anonymous_detail")
        reasons.append("f2_anonymous:" + (detail or {}).get("_error", "empty_or_mismatched") if isinstance(detail, dict)
                       else "f2_anonymous:empty_or_mismatched")
        if self.allow_cookies and self.config.get("browser"):
            try:
                jar = self.jar()
            except Exception as exc:
                reasons.append(f"chrome_cookies:{type(exc).__name__}")
            else:
                detail = self.detail(vid, jar)
                found = author_works.detail_author(detail, vid)
                if found:
                    return save_lookup(self.config, vid, found[0], found[1], "f2_chrome_cookie_detail")
                reasons.append("f2_chrome:" + (detail or {}).get("_error", "empty_or_mismatched") if isinstance(detail, dict)
                               else "f2_chrome:empty_or_mismatched")
        return {"id": vid, "sec_uid": None, "nickname": None, "route": None, "reason": "; ".join(reasons)}


def out_dir_for(args_out, config):
    if args_out:
        return Path(args_out).expanduser().resolve()
    project = Path(config["cache_dir"]).expanduser().resolve().parents[1]
    return project / "migration" / f"dy-author-backfill-{datetime.now().strftime('%Y%m%d')}"


def file_sha(path):
    return sha(path.read_bytes()) if path.is_file() else None


def reviewed_ids(vault, config):
    path = vault / config.get("review_state_file", review_log.STATE_FILE)
    state = review_log.load_state(path, {})
    return {vid for vid, item in state["items"].items() if item.get("review")}


def preview(config, out, *, lookup=None):
    vault = Path(config["vault"]).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = library.scan(vault)
    reviewed = reviewed_ids(vault, config)
    lookup = lookup or Lookup(config)
    notes, skipped_reviewed, stopped = [], [], None
    for row in rows:
        if row.get("douyin_author_id"):
            continue
        entry = {"id": row["aweme_id"], "path": row["path"], "title": row.get("title", ""),
                 "source_url": row.get("source_url") or "",
                 "sha256": sha((vault / row["path"]).read_bytes())}
        if row["aweme_id"] in reviewed:
            skipped_reviewed.append(dict(entry, reason="已登记绑定笔记哈希的复审，补属性会使复审失效"))
            continue
        if stopped:
            notes.append(dict(entry, author=None, reason="lookup_not_run:rate_limited"))
            continue
        try:
            found = lookup(row["aweme_id"])
        except RateLimited:
            stopped = row["aweme_id"]
            notes.append(dict(entry, author=None, reason="rate_limited"))
            continue
        if found.get("sec_uid"):
            notes.append(dict(entry, author={"sec_uid": found["sec_uid"], "nickname": found.get("nickname") or "",
                                             "route": found.get("route")}))
        else:
            notes.append(dict(entry, author=None, reason=found.get("reason", "unknown")))
    names = defaultdict(Counter)
    for note in notes:
        if note["author"]:
            names[note["author"]["sec_uid"]][note["author"]["nickname"]] += 1
    authors = {uid: {"nickname": counter.most_common(1)[0][0] or uid[:12], "count": sum(counter.values()),
                     "nicknames": dict(counter)} for uid, counter in names.items()}
    plan = {"schema_version": 1, "created_at": library.now(), "vault": str(vault),
            "total_dy_notes": len(rows), "targets": len(notes) + len(skipped_reviewed),
            "catalog_sha256": {name: file_sha(vault / name) for name in
                               (unified_store.INDEX_NAME, unified_store.LOG_NAME,
                                config.get("review_state_file", review_log.STATE_FILE))},
            "rate_limited_at": stopped, "authors": authors, "notes": notes,
            "skipped_reviewed": skipped_reviewed}
    pipeline.write_json(out / "plan.json", plan)
    (out / "preview.md").write_text(render_preview(plan), encoding="utf-8")
    resolved = sum(bool(n["author"]) for n in notes)
    return {"status": "previewed", "out": str(out), "total_dy_notes": len(rows), "targets": plan["targets"],
            "resolved": resolved, "unresolved": len(notes) - resolved, "authors": len(authors),
            "skipped_reviewed": len(skipped_reviewed), "rate_limited_at": stopped,
            "preview": str(out / "preview.md"), "plan": str(out / "plan.json")}


def render_preview(plan):
    lines = ["# 作者属性回填预览", "",
             f"生成时间：{plan['created_at']}；DY 笔记 {plan['total_dy_notes']} 篇，待补作者 {plan['targets']} 篇。",
             "只插入 `douyin_author`、`douyin_author_id` 两个属性；正文、总索引、总日志、复审状态不改。", ""]
    resolved = [n for n in plan["notes"] if n["author"]]
    unresolved = [n for n in plan["notes"] if not n["author"]]
    lines += [f"- 已核实作者：{len(resolved)} 篇，{len(plan['authors'])} 位作者",
              f"- 未能核实：{len(unresolved)} 篇（写入 `抖音作者/未识别作者.md`，不猜测）",
              f"- 因复审哈希跳过：{len(plan['skipped_reviewed'])} 篇"]
    if plan.get("rate_limited_at"):
        lines.append(f"- 遇到 HTTP 429，查询在 {plan['rate_limited_at']} 停止；可稍后重新 preview（已查到的会复用）")
    routes = Counter(n["author"]["route"].split(":")[0] for n in resolved)
    lines += ["", "查询途径：" + "，".join(f"{k} {v}" for k, v in routes.most_common()), "",
              "## 按作者", "", "| 作者 | sec_uid | 笔记数 | 其他昵称 |", "| --- | --- | --- | --- |"]
    for uid, info in sorted(plan["authors"].items(), key=lambda kv: (-kv[1]["count"], kv[1]["nickname"])):
        others = "、".join(k for k in info["nicknames"] if k != info["nickname"])
        lines.append(f"| {info['nickname']} | `{uid[:16]}…` | {info['count']} | {others} |")
    if unresolved:
        lines += ["", "## 未能核实作者", "", "| 作品 | 笔记 | 原因 |", "| --- | --- | --- |"]
        for n in unresolved:
            lines.append(f"| {n['id']} | {n['title']} | {n['reason']} |")
    if plan["skipped_reviewed"]:
        lines += ["", "## 跳过（已复审）", ""] + [f"- {n['id']} {n['title']}" for n in plan["skipped_reviewed"]]
    return "\n".join(lines) + "\n"


def apply(config, out):
    vault = Path(config["vault"]).expanduser().resolve()
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    if plan.get("vault") != str(vault):
        raise ValueError("预览的 Vault 与当前配置不一致。")
    if (out / "result.json").exists():
        raise ValueError("该预览已执行过 apply；如需再次回填请重新 preview 到新目录。")
    backup = out / "before"
    with library.locked(vault):
        for note in plan["notes"] + plan["skipped_reviewed"]:
            path = library.inside(vault, note["path"])
            if not path.is_file() or sha(path.read_bytes()) != note["sha256"]:
                raise ValueError(f"笔记在预览后发生变化，请重新 preview：{note['path']}")
        changes = {}
        for note in plan["notes"]:
            if note["author"]:
                info = plan["authors"][note["author"]["sec_uid"]]
                path = library.inside(vault, note["path"])
                changes[path] = author_docs.insert_author_keys(path.read_bytes(), info["nickname"],
                                                                note["author"]["sec_uid"])
        for uid, info in plan["authors"].items():
            author_docs.stage_author(vault, config, changes, uid, nickname=info["nickname"])
        unknown = [{"id": n["id"], "path": n["path"], "title": n["title"], "url": n["source_url"],
                    "reason": n.get("reason", "")} for n in plan["notes"] if not n["author"]]
        unknown_path = author_docs.render_unknown(vault, config, unknown, changes) if unknown else None
        for path in changes:
            if path.exists():
                target = backup / path.relative_to(vault)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        library.transact(vault, changes)
    result = {"status": "applied", "applied_at": library.now(),
              "updated_notes": sum(bool(n["author"]) for n in plan["notes"]),
              "author_docs": len(plan["authors"]), "unknown": len(unknown),
              "unknown_doc": str(unknown_path) if unknown_path else None, "backup": str(backup),
              "changed_files": [p.relative_to(vault).as_posix() for p in changes]}
    pipeline.write_json(out / "result.json", result)
    return result


def verify(config, out):
    vault = Path(config["vault"]).expanduser().resolve()
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    problems = []
    rows = library.scan(vault)
    if len(rows) < plan["total_dy_notes"]:
        problems.append(f"DY 笔记数减少：{len(rows)} < {plan['total_dy_notes']}")
    by_path = {r["path"]: r for r in rows}
    for note in plan["notes"]:
        if not note["author"]:
            continue
        before_path = out / "before" / note["path"]
        current = vault / note["path"]
        if not before_path.is_file() or not current.is_file():
            problems.append(f"缺少备份或笔记：{note['path']}")
            continue
        old_front, old_body = library.read_note_text(before_path.read_text(encoding="utf-8"))
        new_front, new_body = library.read_note_text(current.read_text(encoding="utf-8"))
        uid = note["author"]["sec_uid"]
        if new_body != old_body:
            problems.append(f"正文变化：{note['path']}")
        if new_front != dict(old_front, douyin_author=plan["authors"][uid]["nickname"], douyin_author_id=uid):
            problems.append(f"属性不是原属性加两个作者键：{note['path']}")
        if by_path.get(note["path"], {}).get("douyin_author_id") != uid:
            problems.append(f"扫描未读到作者：{note['path']}")
    for name, digest in plan["catalog_sha256"].items():
        if file_sha(vault / name) != digest:
            problems.append(f"{name} 在回填后发生变化（可能是之后的其他导入）")
    docs = author_docs.author_docs(vault, config)
    missing = [uid for uid in plan["authors"] if uid not in docs]
    if missing:
        problems.append(f"缺少作者文档：{len(missing)} 位")
    result = {"status": "ok" if not problems else "problems", "dy_notes": len(rows),
              "author_docs": len(docs), "expected_author_docs": len(plan["authors"]), "problems": problems[:50]}
    pipeline.write_json(out / "verify.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "apply", "verify"))
    parser.add_argument("--config", default=str(pipeline.DEFAULT_CONFIG))
    parser.add_argument("--out", help="回填记录目录；默认 migration/dy-author-backfill-<日期>")
    args = parser.parse_args()
    try:
        config = pipeline.load_json(args.config)
        out = out_dir_for(args.out, config)
        result = {"preview": preview, "apply": apply, "verify": verify}[args.command](config, out)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") != "problems" else 2


if __name__ == "__main__":
    raise SystemExit(main())
