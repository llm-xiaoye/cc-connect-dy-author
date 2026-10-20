#!/usr/bin/env python3
"""/dy-author queue: one work -> its author's full public works -> dedup -> import.

Also `/dy-author 补录`: add interview-type knowledge notes that are already in the
vault (for example imported by /dy) to 知识库/面试题库.

Adapted from douyin-to-obsidian/dy.py; runs independently of that skill. The
current agent writes each knowledge note / question document; no timers or model
subprocesses are created. Vault notes (aweme_id) remain the dedup source of truth.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

import author_docs
import author_works
import library
import pipeline
import question_bank
import usage

TERMINAL = {"saved", "exists", "skipped", "failed", "unsupported"}
UNSUPPORTED_ARTICLE = "抖音文章暂不支持，未导入"
TRIGGER = "复制打开抖音"
COMMAND = "/dy-author"
SUPPLEMENT = "补录"


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def runs_root(config):
    return Path(config["cache_dir"]).expanduser().resolve() / "runs"


def initialize(config):
    vault = Path(config["vault"]).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"知识库目录不存在：{vault}")
    if (vault / ".knowledge-migration-in-progress.json").exists():
        raise ValueError("知识库正在迁移，请迁移完成后重新 init。")
    base = runs_root(config)
    run = base / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, mode=0o700)
    snapshot = dict(config, vault=str(vault), cache_dir=str(base.parent))
    pipeline.write_json(run / "config.json", snapshot)
    pipeline.write_json(run / "state.json", {"schema_version": 1, "run_dir": str(run), "planned": False,
                                             "items": [], "warnings": []})
    return {"run_dir": str(run), "input_file": str(run / "input.txt"), "state_file": str(run / "state.json")}


def load_run(value, config):
    run = Path(value).expanduser().resolve()
    if run.parent != runs_root(config) or not (run / "state.json").is_file():
        raise ValueError("请使用本次 init 返回的运行目录。")
    state = pipeline.load_json(run / "state.json")
    if state.get("run_dir") != str(run):
        raise ValueError("运行目录与状态不匹配。")
    return run, state, pipeline.load_json(run / "config.json")


def write_state(run, state):
    pipeline.write_json(run / "state.json", state)


def existing(vid, config):
    return library.find_existing(config["vault"], pipeline.video_id(vid))


def parse_input(raw):
    text = raw.strip()
    command = re.match(r"^[/$]dy-author(?:\s+|$)", text)
    if command is None:
        raise ValueError("AUTHOR_SCOPE_REQUIRES_EXPLICIT_COMMAND：作者全量导入必须显式使用 /dy-author；裸抖音作品或分享文本只交给 /dy 处理本次链接。")
    text = text[command.end():].strip()
    if not text:
        raise ValueError("用法：/dy-author 抖音作品链接或完整分享文本；/dy-author 补录。")
    if text == SUPPLEMENT:
        return "supplement", None
    urls = re.findall(r"https?://[^\s<>\"，。、]+", text)
    if not urls:
        if TRIGGER in text:
            raise ValueError("识别到抖音分享口令，但没有找到完整链接。请重新复制完整分享内容。")
        raise ValueError("请提供一条抖音作品链接或完整分享文本；补录请只输入“补录”。")
    return "author", text


def checked_input(run, state):
    source_run = Path(state["run_dir"]).resolve()
    actual_run = Path(run).resolve()
    if actual_run != source_run:
        items = state["items"]
        if len(items) != 1 or actual_run != source_run / ".workers" / items[0]["id"]:
            raise ValueError("AUTHOR_RUN_SCOPE_MISMATCH：工作检查点必须绑定同一父批次与作品。")
    raw = (source_run / "input.txt").read_text(encoding="utf-8")
    mode, text = parse_input(raw)
    expected = state.get("input_sha256")
    if expected and hashlib.sha256(raw.encode()).hexdigest() != expected:
        raise ValueError("AUTHOR_INPUT_CHANGED：输入与已规划批次不一致，请用当前显式 /dy-author 命令建立新批次。")
    if state.get("planned") and mode != state.get("mode"):
        raise ValueError("AUTHOR_INPUT_CHANGED：当前命令模式与已规划批次不一致。")
    return raw, mode, text


def report(state):
    counts = Counter(item["status"] for item in state["items"])
    counts = {key: counts[key] for key in ("pending", "ready", "saved", "exists", "skipped", "failed",
                                           "unsupported")}
    finished = state.get("planned", False) and not counts["pending"] and not counts["ready"]
    failures = [{"id": i["id"], "title": i.get("title", ""), "url": i["url"],
                 "stage": i.get("stage"), "reason": i.get("reason", "")}
                for i in state["items"] if i["status"] == "failed"]
    unsupported = [{"id": i["id"], "title": i.get("title", ""), "url": i["url"], "reason": i.get("reason", "")}
                   for i in state["items"] if i["status"] == "unsupported"]
    seed = state.get("seed") or {}
    retry = ""
    if state.get("mode") == "author" and (failures or not state.get("source_complete", False)) and seed.get("url"):
        retry = f"{COMMAND} {seed['url']}"
    elif state.get("mode") == "supplement" and failures:
        retry = f"{COMMAND} {SUPPLEMENT}"
    return {"run_dir": state["run_dir"], "state_file": str(Path(state["run_dir"]) / "state.json"),
            "mode": state.get("mode"), "finished": finished,
            "source_complete": state.get("source_complete", False),
            "complete": (finished and state.get("source_complete", False) and not counts["failed"]
                         and not counts["unsupported"]),
            "author": state.get("author"), "author_doc": state.get("author_doc"),
            "listing": state.get("listing"), "counts": counts, "warnings": state["warnings"],
            "saved_notes": [{"title": i.get("knowledge_title", i.get("title")), "note": i["note"]}
                            for i in state["items"] if i["status"] == "saved" and i.get("note")],
            "questions": [q for i in state["items"] for q in i.get("questions", [])
                          if q.get("action") in ("新增面试题", "合并面试题")],
            "failures": failures, "unsupported": unsupported, "retry_command": retry,
            "usage": usage.summary(usage.load(Path(state["run_dir"])))}


# ---------------------------------------------------------------- plan

def progress_report(state):
    counts = Counter(item["status"] for item in state["items"])
    total = len(state["items"])
    remaining = counts["pending"] + counts["ready"]
    ids = set(state.get("last_batch_ids") or [])
    batch = [item for item in state["items"] if item["id"] in ids]
    batch_counts = Counter(item["status"] for item in batch)
    finished = bool(batch) and all(item["status"] in TERMINAL for item in batch)
    detail = f"新增 {batch_counts['saved']} 条、已有 {batch_counts['exists']} 条"
    for status, label in (("skipped", "跳过"), ("failed", "失败待重试"), ("unsupported", "暂不支持")):
        if batch_counts[status]:
            detail += f"、{label} {batch_counts[status]} 条"
    message = (f"本批处理 {len(batch)} 条（{detail}）；累计已处理 {total - remaining}/{total} 条，"
               f"剩余 {remaining} 条。") if finished else "本批尚未全部提交，不汇报批次完成。"
    if finished and not state.get("source_complete", False):
        message += " 当前作品清单不完整。"
    return {"batch_finished": finished, "batch_size": len(batch), "batch_counts": dict(batch_counts),
            "total": total, "processed": total - remaining, "saved": counts["saved"],
            "exists": counts["exists"], "failed": counts["failed"], "remaining": remaining,
            "message": message}


def plan(run, state, config):
    raw, mode, text = checked_input(run, state)
    if state["planned"]:
        return report(state)
    state.update(mode=mode, input_sha256=hashlib.sha256(raw.encode()).hexdigest())
    if mode == "supplement":
        return plan_supplement(run, state, config)
    selected = author_works.select_official_url(text)
    canonical, seed_id = pipeline.normalize_url(selected)
    listing = asyncio.run(author_works.enumerate_work(seed_id, config, max_pages=author_works.MAX_PAGES,
                                                     source_url=canonical))
    listing.source_id = seed_id
    seed_kind = next((i["kind"] for i in listing.items if i["id"] == seed_id),
                     "note" if "/note/" in canonical else "video")
    listing.source_kind = seed_kind
    saved = author_works.save_listing(listing, config)
    seed_url = f"https://www.douyin.com/{seed_kind}/{seed_id}"
    state.update(seed={"id": seed_id, "kind": seed_kind, "url": seed_url, "input_url": selected},
                 source_complete=listing.complete,
                 listing={"complete": listing.complete, "reason": listing.reason, "access": listing.access,
                          "count": saved["count"], "videos": saved["videos"], "notes": saved["notes"],
                          "articles": saved["articles"],
                          "result_file": saved["result_file"]})
    if not listing.complete:
        state["warnings"].append({"scope": "作者作品清单", "reason": listing.reason or "incomplete"})
    author = None
    if listing.author_id:
        author = {"sec_uid": listing.author_id, "nickname": listing.nickname or ""}
    else:
        state["warnings"].append({"scope": "作者", "reason": "未能核实作者 sec_uid；只处理输入作品，不写作者文档"})
    state["author"] = author
    vault = Path(config["vault"]).expanduser().resolve()
    known = library.existing_map(vault)
    skipped = {}
    if author:
        entry = author_docs.load_state(vault, config)["authors"].get(author["sec_uid"]) or {}
        skipped = entry.get("skipped") or {}
    works = list(listing.items)
    if seed_id not in {w["id"] for w in works}:
        works.insert(0, {"id": seed_id, "kind": seed_kind, "title": ""})
    items = []
    for work in works:
        url = author_works.work_url(work)
        kind = {"note": "image", "article": "article"}.get(work["kind"], "video")
        item = {"id": work["id"], "url": url, "source_url": url, "kind": kind,
                "title": work.get("title", ""), "listing_title": work.get("title", ""),
                "author": (author or {}).get("nickname", ""), "collections": [], "status": "pending"}
        if work["id"] in known:
            item.update(status="exists", note=str(vault / known[work["id"]]["path"]))
        elif work["id"] in skipped:
            item.update(status="skipped", reason="作者文档已记录跳过：" + str(skipped[work["id"]].get("reason", "")))
        elif kind == "article":
            # Not downloaded: the feed has only a truncated abstract and the media is music.
            item.update(status="unsupported", stage="plan", reason=UNSUPPORTED_ARTICLE)
        items.append(item)
    state["items"] = items
    if author:
        listing_record = {"checked_at": library.now(), "complete": listing.complete, "reason": listing.reason,
                          "access": listing.access, "items": [{"id": w["id"], "kind": w["kind"]} for w in works]}
        unsupported = {i["id"]: UNSUPPORTED_ARTICLE for i in items if i["status"] == "unsupported"}
        result = library.write_extras(config, lambda v, changes, ei, el: author_docs.stage_author(
            v, config, changes, author["sec_uid"], nickname=author["nickname"], listing=listing_record,
            unsupported=unsupported))
        state["author_doc"] = result.get("author_doc")
    state["planned"] = True
    write_state(run, state)
    return report(state)


def raw_material(vid, config):
    """Read-only original evidence for an existing note (own cache, then shared /dy cache)."""
    roots = [config.get("cache_dir")] + list(config.get("shared_cache_dirs") or [])
    for root in roots:
        if not root:
            continue
        directory = Path(root).expanduser() / vid
        bundle_path, transcript_path = directory / "bundle.json", directory / "transcript.json"
        if not bundle_path.is_file() or not transcript_path.is_file():
            continue
        try:
            bundle, transcript = pipeline.load_json(bundle_path), pipeline.load_json(transcript_path)
        except (OSError, ValueError):
            continue
        if str(bundle.get("aweme_id")) != vid or str(transcript.get("aweme_id")) != vid:
            continue
        text = str(transcript.get("text") or "")
        if not text.strip():
            continue
        return {"bundle": str(bundle_path), "transcript": str(transcript_path),
                "title": str(bundle.get("title") or ""), "caption": str(bundle.get("caption") or ""),
                "text": text, "media_kind": bundle.get("media_kind", "video")}
    return None


def plan_supplement(run, state, config):
    vault = Path(config["vault"]).expanduser().resolve()
    notes = library.scan(vault)
    covered = question_bank.by_work(question_bank.scan_questions(vault, config))
    state.update(source_complete=True, seed=None, author=None, listing=None)
    items = []
    for row in notes:
        vid = row["aweme_id"]
        if vid in covered:
            continue
        note_path = vault / row["path"]
        raw = raw_material(vid, config)
        if raw:
            interview, basis = question_bank.mentions_interview(raw["title"], raw["caption"], raw["text"]), "原始材料"
        else:
            front, body = library.read_note(note_path)
            interview = question_bank.mentions_interview(front.get("title"), " ".join(map(str, front.get("tags") or [])), body)
            basis = "笔记正文（缺少可核对的原始材料）"
        if not interview:
            continue
        url = row.get("source_url") or f"https://www.douyin.com/video/{vid}"
        items.append({"id": vid, "url": url, "source_url": row.get("source_url") or "", "kind": "video",
                      "title": row.get("title", ""), "status": "pending", "supplement": True,
                      "note": str(note_path), "basis": basis, "raw": raw and {k: raw[k] for k in ("bundle", "transcript")},
                      "author_id": row.get("douyin_author_id"), "author": row.get("douyin_author", ""),
                      "collections": []})
    state["items"] = items
    state["planned"] = True
    write_state(run, state)
    return report(state)


# ---------------------------------------------------------------- prepare

def call_pipeline(function, args, config):
    with redirect_stdout(io.StringIO()) as output:
        function(args, config)
    return json.loads(output.getvalue().strip().splitlines()[-1])


def ready_result(item):
    required = [] if item.get("supplement") else ["knowledge_file", "analysis_file"]
    if item.get("interview"):
        required.append("interview_file")
    draft_files = {key: bool(item["artifacts"].get(key)) and Path(item["artifacts"][key]).is_file()
                   for key in required}
    return {"status": "ready", "id": item["id"], "mode": "supplement" if item.get("supplement") else "author",
            "source_url": item["url"], "source_title": item.get("title", ""),
            "media_kind": item.get("kind", "video"), "interview_required": bool(item.get("interview")),
            "collections": item.get("collections", []), "draft_files": draft_files,
            "draft_files_present": bool(draft_files) and all(draft_files.values()), **item["artifacts"]}


def model_material(transcript, bundle, target):
    """Lossless video text view; full timestamps/hashes stay in transcript.json."""
    if bundle.get("media_kind", "video") == "image":
        return transcript
    raw = pipeline.load_json(transcript)
    pipeline.validate_transcript(raw)
    path = target / "material.txt"
    path.write_text(raw["text"], encoding="utf-8")
    return path


def material_text(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    if str(path).endswith(".json"):
        try:
            data = json.loads(raw)
            return str(data.get("text") or "") + "\n" + str(data.get("caption") or "")
        except ValueError:
            return raw
    return raw


def write_catalogs(target, config):
    import catalog_context
    notes, questions = catalog_context.compact_catalogs(config)
    catalog_path = target / "catalog.json"
    pipeline.write_json(catalog_path, notes)
    question_path = target / "question-catalog.json"
    pipeline.write_json(question_path, questions)
    return catalog_path, question_path


def catalog_search(run, state, config, vid, limit=8):
    import catalog_context
    item = select_item(state, vid)
    if item["status"] != "ready":
        raise ValueError("只为已准备好完整材料、尚未提交的作品检索目录。")
    target = Path(item["artifacts"]["item_dir"])
    if target.resolve() != (Path(run) / "items" / item["id"]).resolve():
        raise ValueError("检索文件必须位于本条作品的运行目录。")
    # Also replaces heavy catalog files in interrupted runs; material/drafts stay intact.
    write_catalogs(target, config)
    terms_file, matches_file = target / "catalog-terms.txt", target / "catalog-matches.json"
    result = catalog_context.search(config, catalog_context.read_terms(terms_file),
                                    interview=item.get("interview", False), limit=limit)
    pipeline.write_json(matches_file, result)
    item["catalog_search_count"] = item.get("catalog_search_count", 0) + 1
    data = usage.load(run)
    entry = usage.item(data, item["id"])
    usage.record_text(entry, "read", f"catalog_matches_{item['catalog_search_count']}",
                      json.dumps(result, ensure_ascii=False))
    usage.save(run, data)
    write_state(run, state)
    return {"status": "catalog_matches", "id": item["id"], "matches_file": str(matches_file), **result}


def prepare_worker(run, state, item, config):
    # Each process has its own stdout and durable checkpoint; only the parent
    # writes the batch state. Shared browser/GPU resources have separate locks.
    run = Path(run)
    checkpoint = run / ".workers" / item["id"]
    previous = checkpoint / "state.json"
    if previous.is_file():
        old = pipeline.load_json(previous)
        if len(old.get("items", [])) == 1 and old["items"][0]["id"] == item["id"]:
            item = old["items"][0]
    child = dict(state, items=[item])
    next_item(checkpoint, child, dict(config, _artifacts_run=str(run)), prepare_all=True, limit=1, workers=1)
    return child["items"][0]


def select_batch(state, limit):
    limit = min(limit, 10)
    ready = sum(item["status"] == "ready" for item in state["items"])
    room = max(0, limit - ready)
    return [item for item in state["items"] if item["status"] not in TERMINAL | {"ready"}][:room]


def prepare_parallel(run, state, config, workers, limit):
    batch = select_batch(state, limit)
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(prepare_worker, str(run), state, item, config): item for item in batch}
        for future in as_completed(futures):
            item = futures[future]
            # Worker failures propagate; checkpoints preserve recovery budgets.
            item.update(future.result())
            write_state(run, state)
    return finish_prepare(run, state, config, batch)


def finish_prepare(run, state, config, batch):
    ready_ids = [item["id"] for item in state["items"] if item["status"] == "ready"]
    state["last_batch_ids"] = list(dict.fromkeys([*ready_ids, *(item["id"] for item in batch)]))
    write_state(run, state)
    data = usage.load(run)
    for item in batch:
        entry = usage.item(data, item["id"])
        for stage, seconds in (item.get("timings") or {}).items():
            entry["timings"][stage] = seconds
        if item["status"] == "ready":
            artifacts = item["artifacts"]
            usage.record_text(entry, "read", "material", material_text(artifacts["material_file"]))
            usage.record_text(entry, "read", "catalog", usage.file_text(artifacts["catalog_file"]))
            usage.record_text(entry, "read", "question_catalog", usage.file_text(artifacts["question_catalog_file"]))
            if artifacts.get("note_file"):
                usage.record_text(entry, "read", "note", usage.file_text(artifacts["note_file"]))
            entry["image_pages"] = len(artifacts.get("image_files") or [])
            usage.mark(entry, "ready_at")
    usage.save(run, data)
    failed = {item["id"]: item.get("reason", "")[:300] for item in batch if item["status"] == "failed"}
    unsupported = {item["id"]: UNSUPPORTED_ARTICLE for item in batch if item["status"] == "unsupported"}
    author = state.get("author")
    if (failed or unsupported) and author and state.get("mode") == "author":
        library.write_extras(config, lambda v, changes, ei, el: author_docs.stage_author(
            v, config, changes, author["sec_uid"], nickname=author.get("nickname"), failed=failed,
            unsupported=unsupported or None))
    ready = [ready_result(item) for item in state["items"] if item["status"] == "ready"]
    pending = sum(item["status"] == "pending" for item in state["items"])
    status = "prepared" if ready else ("continue" if pending else "done")
    return {"status": status, "ready": ready, "model_analysis_needed": bool(ready),
            "remaining_pending": pending, **report(state)}


def prepare_item(run, state, item, config):
    args = argparse.Namespace(source=item["url"], url=None, browser=None,
                              title=item.get("title") or None, author=item.get("author") or None,
                              collection=item.get("collections") or None)
    try:
        initial_config = config
        attempted = item.get("browser_recovery", {}).get("attempted")
        browser_flow = (config.get("browser") == "chrome" and pipeline.sys.platform == "darwin"
                        and config.get("auto_browser_recovery", True) and not attempted)
        if browser_flow and config.get("prefer_browser_media", False):
            initial_config = dict(config, _browser_media_only=True)
        elif browser_flow and config.get("background_detail_first", False):
            # Same matched detail JSON as the page would deliver, fetched in the background;
            # only when it is unavailable does recovery open (and later close) a tab.
            initial_config = dict(config, _background_detail_only=True)
        return call_pipeline(pipeline.prepare, args, initial_config)
    except pipeline.image_posts.StageError as exc:
        if exc.code != "DETAIL_UNAVAILABLE" or config.get("auto_browser_recovery", True) is not True:
            raise
        if item.get("browser_recovery", {}).get("attempted"):
            raise pipeline.image_posts.StageError("detail", "浏览器恢复机会已使用；本条停止重试。",
                                                  "RECOVERY_EXHAUSTED") from None
        recovery = {"attempted": True, "status": "started", "first_failure": str(exc),
                    "started_at": datetime.now(timezone.utc).isoformat()}
        item.update(stage="browser_recovery", browser_recovery=recovery)
        # Persist before opening: interrupted/resumed runs cannot loop browser refresh.
        write_state(run, state)
    with locked(Path(config["cache_dir"]) / ".locks" / "browser-recovery.lock"):
        return recover_item(run, state, item, config, args, recovery)


def recover_item(run, state, item, config, args, recovery):
    # Close only the tab this recovery opens, after its media has been read (or it failed).
    track = bool(config.get("close_opened_tabs")) and config.get("browser") == "chrome"
    before = pipeline.douyin_access.douyin_tab_ids(item["id"], "chrome") if track else None
    try:
        return _recover_item(run, state, item, config, args, recovery)
    finally:
        if track:
            closed = pipeline.douyin_access.close_new_tabs(item["id"], before, "chrome")
            recovery["opened_tab_closed"] = closed if closed is not None else "unavailable"
            write_state(run, state)


def _recover_item(run, state, item, config, args, recovery):
    baseline = pipeline.browser_cache.snapshot_entries(config.get("browser"), config.get("browser_cache_dir"))
    probe = {}
    recovery["cache_baseline_entries"] = len(baseline) if baseline is not None else None
    recovery["cache_probe"] = probe
    cache_config = dict(config, _browser_cache_baseline=baseline, _browser_cache_diagnostics=probe)
    try:
        recovery.update(pipeline.douyin_access.refresh_browser_session(
            item["id"], config.get("browser"), config.get("browser_recovery_wait_seconds", 8),
            source_url=item["url"], profile=config.get("browser_profile")))
        recovery["status"] = "retrying"
        retry_config = dict(cache_config, _browser_media_wait_seconds=config.get("browser_media_timeout_seconds", 40))
        recovery["media_wait_timeout_seconds"] = retry_config["_browser_media_wait_seconds"]
        write_state(run, state)
        try:
            result = call_pipeline(pipeline.prepare, args, retry_config)
        except pipeline.image_posts.StageError as exc:
            if exc.code != "DETAIL_UNAVAILABLE":
                raise
            result = call_pipeline(pipeline.prepare, args,
                                   dict(cache_config, _browser_media_only=True, _browser_media_wait_seconds=0))
    except Exception as exc:
        recovery.update(status="failed", finished_at=datetime.now(timezone.utc).isoformat(),
                        failure_code=getattr(exc, "code", None) or type(exc).__name__)
        write_state(run, state)
        if isinstance(exc, pipeline.image_posts.StageError) and exc.code == "DETAIL_UNAVAILABLE":
            if probe.get("scan_incomplete") or probe.get("deadline_reached"):
                raise pipeline.image_posts.StageError("detail",
                    "缓存查找未完成或超时，尚不能判断作品媒体是否可提取。"
                    "已停止本条、未写入笔记；应检查缓存读取性能，不能据此要求重新登录或等待几天。",
                    "CACHE_LOOKUP_INCOMPLETE") from None
            raise pipeline.image_posts.StageError("detail", "媒体提取失败：本次未取得与作品 ID 匹配的完整媒体。"
                "已停止本条，不再循环；未写入笔记。浏览器登录/验证码状态未观察，不能据此建议重新登录。"
                "页面能正常播放时应检查提取适配；扫描统计见 browser_recovery.cache_probe。",
                "RECOVERY_EXHAUSTED") from None
        raise
    recovery.update(status="recovered", finished_at=datetime.now(timezone.utc).isoformat())
    write_state(run, state)
    return result


def prepare_supplement_item(run, item, config):
    """No download: stage the note and any verified raw material for question writing."""
    target = Path(config.get("_artifacts_run", run)) / "items" / item["id"]
    target.mkdir(parents=True, exist_ok=True)
    note = Path(item["note"])
    if not note.is_file():
        raise ValueError("待补录的知识笔记已不存在。")
    material = target / "material.txt"
    raw = item.get("raw")
    if raw:
        material.write_text(material_text(raw["transcript"]), encoding="utf-8")
    else:
        material.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")
    catalog_path, question_path = write_catalogs(target, config)
    if not item.get("author_id") and not item.get("lookup_author"):
        import backfill_authors
        try:
            found = backfill_authors.Lookup(config)(item["id"])
        except backfill_authors.RateLimited:
            found = {"reason": "rate_limited"}
        if found.get("sec_uid"):
            item["lookup_author"] = {"sec_uid": found["sec_uid"], "nickname": found.get("nickname") or "",
                                     "route": found.get("route")}
        else:
            item["author_lookup_reason"] = found.get("reason", "")
    item.update(status="ready", stage="analysis", interview=True,
                note_sha256=hashlib.sha256(note.read_bytes()).hexdigest(),
                material_sha256=hashlib.sha256(material.read_bytes()).hexdigest(),
                artifacts={"note_file": str(note), "material_file": str(material),
                           "transcript_file": raw["transcript"] if raw else "",
                           "catalog_file": str(catalog_path), "question_catalog_file": str(question_path),
                           "interview_file": str(target / "interview.json"), "item_dir": str(target),
                           "reason_file": str(target / "reason.txt"), "basis": item.get("basis", ""),
                           "image_files": []})


def next_item(run, state, config, prepare_all=False, limit=None, workers=None):
    if not state["planned"]:
        raise ValueError("请先执行 plan。")
    checked_input(run, state)
    workers = config.get("prepare_workers", 1) if workers is None else workers
    if type(workers) is not int or not 1 <= workers <= 3:
        raise ValueError("prepare_workers 必须是 1–3 的整数。")
    limit = limit or config.get("prepare_batch", 10)
    if prepare_all and workers > 1 and state.get("mode") != "supplement":
        return prepare_parallel(run, state, config, workers, limit)
    batch = select_batch(state, limit) if prepare_all else []
    batch_ids = {item["id"] for item in batch}
    for item in state["items"]:
        if item["status"] in TERMINAL:
            continue
        vid = item["id"]
        if not item.get("supplement"):
            note = existing(vid, config)
            if note:
                item.update(status="exists", note=str(note))
                write_state(run, state)
                continue
        if item["status"] == "ready":
            if prepare_all:
                continue
            return ready_result(item)
        if not prepare_all:
            continue
        if vid not in batch_ids:
            continue
        stage = "prepare"
        timings = item.setdefault("timings", {})
        try:
            item.update(stage=stage)
            write_state(run, state)
            if item.get("supplement"):
                prepare_supplement_item(run, item, config)
                write_state(run, state)
                continue
            with locked(Path(config["cache_dir"]) / ".locks" / f"{vid}.lock"):
                started = time.monotonic()
                result = prepare_item(run, state, item, config)
                timings["prepare"] = round(time.monotonic() - started, 3)
                bundle_path = Path(result["bundle"])
                stage = "extract"
                item.update(stage=stage)
                write_state(run, state)
                started = time.monotonic()
                with locked(Path(config["cache_dir"]) / ".locks" / "local-recognition.lock"):
                    call_pipeline(pipeline.transcribe, argparse.Namespace(bundle=str(bundle_path), model=None), config)
                timings["extract"] = round(time.monotonic() - started, 3)
                target = Path(config.get("_artifacts_run", run)) / "items" / vid
                target.mkdir(parents=True, exist_ok=True)
                transcript = target / "transcript.json"
                shutil.copyfile(bundle_path.parent / "transcript.json", transcript)
                bundle = pipeline.load_json(bundle_path)
                material = model_material(transcript, bundle, target)
                catalog_path, question_path = write_catalogs(target, config)
                interview = question_bank.mentions_interview(
                    item.get("listing_title"), bundle.get("title"), bundle.get("caption"), material_text(material))
                item.update(status="ready", stage="analysis", kind=bundle.get("media_kind", "video"),
                            title=bundle["title"], bundle=str(bundle_path), interview=interview,
                            transcript_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest(),
                            material_sha256=hashlib.sha256(material.read_bytes()).hexdigest(),
                            artifacts={"transcript_file": str(transcript), "material_file": str(material),
                                       "knowledge_file": str(target / "knowledge.md"),
                                       "analysis_file": str(target / "analysis.json"),
                                       "interview_file": str(target / "interview.json"),
                                       "item_dir": str(target), "reason_file": str(target / "reason.txt"),
                                       "catalog_file": str(catalog_path), "question_catalog_file": str(question_path),
                                       "image_files": [str(bundle_path.parent / p["file"]) for p in bundle.get("images", [])]})
        except Exception as exc:
            if getattr(exc, "code", None) == "UNSUPPORTED_ARTICLE":
                item.update(status="unsupported", kind="article", stage=getattr(exc, "stage", stage),
                            error_code="UNSUPPORTED_ARTICLE", reason=UNSUPPORTED_ARTICLE)
                write_state(run, state)
                continue
            safe_reason = str(exc) if isinstance(exc, (ValueError, pipeline.image_posts.StageError)) else type(exc).__name__
            item.update(status="failed", stage=getattr(exc, "stage", stage),
                        error_code=getattr(exc, "code", type(exc).__name__), reason=f"{stage} 失败：{safe_reason}"[:1200])
        write_state(run, state)
    if prepare_all:
        if config.get("_artifacts_run"):
            return {"status": "worker_done"}
        return finish_prepare(run, state, config, batch)
    pending = sum(item["status"] == "pending" for item in state["items"])
    if pending:
        return {"status": "need_prepare", "remaining_pending": pending, **report(state)}
    return {"status": "done", **report(state)}


# ---------------------------------------------------------------- commit

def select_item(state, vid):
    vid = pipeline.video_id(vid)
    found = next((i for i in state["items"] if i["id"] == vid), None)
    if found is None:
        raise ValueError("该作品不在本次输入清单中。")
    return found


def questions_for(item, target, required):
    path = target / "interview.json"
    if required:
        if not path.is_file():
            raise ValueError("该作品含“面试”，需要按面试题规则写 interview.json 与题目正文后再提交。")
        return question_bank.load_interview(path, target)
    if path.exists():
        raise ValueError("非面试类作品不能提交题目文档；请删除 interview.json。")
    return []


def commit(run, state, config, vid):
    item = select_item(state, vid)
    if item.get("supplement"):
        return commit_supplement(run, state, config, item)
    vault = Path(config["vault"]).expanduser().resolve()
    author = state.get("author")
    started = time.monotonic()
    with locked(vault / ".douyin-import.lock"):
        note = existing(vid, config)
        if note:
            prior = item["status"]
            item.update(status="saved" if prior == "saved" else "exists", note=str(note))
            write_state(run, state)
            return {"status": "exists", "id": vid, "note": str(note), "written": False}
        if item["status"] != "ready":
            raise ValueError("此作品尚未准备好知识正文，不能提交。")
        target = run / "items" / vid
        analysis = pipeline.load_json(target / "analysis.json")
        title, tags = analysis.get("title"), analysis.get("tags", [])
        if not isinstance(title, str) or not title.strip() or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("analysis.json 需要非空 title 和字符串数组 tags。")
        questions = questions_for(item, target, bool(item.get("interview")))
        with locked(Path(config["cache_dir"]) / ".locks" / f"{vid}.lock"):
            current_transcript = Path(item["bundle"]).parent / "transcript.json"
            if hashlib.sha256(current_transcript.read_bytes()).hexdigest() != item["transcript_sha256"]:
                raise ValueError("音频转写在本次提炼后发生变化；请重新获取该作品并提炼，未写入笔记。")
            if hashlib.sha256(Path(item["artifacts"]["material_file"]).read_bytes()).hexdigest() != item["material_sha256"]:
                raise ValueError("本次模型材料已变化，未写入笔记。")

            def before(vault_path, changes, extra_index, extra_logs, note_path, front):
                if not questions:
                    return {}
                return {"questions": question_bank.stage_questions(
                    vault_path, config, changes, extra_index, extra_logs, vid=vid, url=front["source_url"],
                    note_path=note_path, note_title=front["title"], questions=questions, author_front=front)}

            def after(vault_path, changes, note_path):
                if author:
                    author_docs.stage_author(vault_path, config, changes, author["sec_uid"],
                                             nickname=author.get("nickname"), clear_failed=[vid])

            result = pipeline.save_bundle(item["bundle"], target / "analysis.json", target / "knowledge.md",
                                          config, collections=item.get("collections") or [],
                                          author=author, before_metadata=before, after_metadata=after)
        item.update(status=result["status"], stage="done", note=result["note"],
                    knowledge_title=result.get("title", title), questions=result.get("questions", []))
        write_state(run, state)
    record_commit_usage(run, vid, target, questions, time.monotonic() - started)
    return {"id": vid, **result}


def record_commit_usage(run, vid, target, questions, seconds):
    data = usage.load(run)
    entry = usage.item(data, vid)
    usage.record_text(entry, "written", "knowledge", usage.file_text(target / "knowledge.md"))
    usage.record_text(entry, "written", "analysis", usage.file_text(target / "analysis.json"))
    usage.record_text(entry, "written", "questions", "\n".join(q["body"] for q in questions))
    merged = [q for q in questions if q["action"] == "merge"]
    if merged:
        usage.record_text(entry, "read", "merge_targets",
                          "\n".join(usage.file_text(Path(json.loads((run / "config.json").read_text())["vault"]) / q["target"])
                                    for q in merged))
    ready_at = entry.get("ready_at")
    if ready_at:
        waited = (datetime.now(timezone.utc) - datetime.fromisoformat(ready_at)).total_seconds()
        usage.record_timing(entry, "analysis_wait", waited)
    usage.record_timing(entry, "commit", seconds)
    usage.save(run, data)


def commit_supplement(run, state, config, item):
    vid = item["id"]
    vault = Path(config["vault"]).expanduser().resolve()
    target = run / "items" / vid
    started = time.monotonic()
    with locked(vault / ".douyin-import.lock"):
        if item["status"] != "ready":
            raise ValueError("此笔记尚未准备好题目材料，不能提交。")
        questions = questions_for(item, target, True)
        note = existing(vid, config)
        if not note:
            raise ValueError("待补录的知识笔记已不存在，未写入题目。")
        if hashlib.sha256(note.read_bytes()).hexdigest() != item["note_sha256"]:
            raise ValueError("知识笔记在准备后被修改，请重新运行补录。")
        author = item.get("lookup_author")

        def stage(vault_path, changes, extra_index, extra_logs):
            front, _ = library.read_note(note)
            try:
                url = library.note_source_url(config, vault_path, vid, None, front)
            except ValueError:
                url = None
            summary = question_bank.stage_questions(vault_path, config, changes, extra_index, extra_logs,
                                                    vid=vid, url=url, note_path=note,
                                                    note_title=front.get("title", note.stem), questions=questions,
                                                    author_front=library.author_front(author) if author else front)
            sec_uid = front.get("douyin_author_id")
            nickname = front.get("douyin_author")
            if not sec_uid and author:
                changes[note] = author_docs.insert_author_keys(note.read_bytes(), author["nickname"], author["sec_uid"])
                sec_uid, nickname = author["sec_uid"], author["nickname"]
            if sec_uid:
                author_docs.stage_author(vault_path, config, changes, sec_uid, nickname=nickname)
            return {"questions": summary}

        result = library.write_extras(config, stage)
        item.update(status="saved", stage="done", knowledge_title=item.get("title"),
                    questions=result.get("questions", []))
        write_state(run, state)
    record_commit_usage(run, vid, target, questions, time.monotonic() - started)
    return {"id": vid, "status": "saved", "note": str(note), **result}


def mark_item(run, state, config, vid, status):
    item = select_item(state, vid)
    if item["status"] in TERMINAL:
        raise ValueError("该作品已有最终结果，不修改它。")
    reason_file = run / "items" / vid / "reason.txt"
    reason = reason_file.read_text(encoding="utf-8").strip() if reason_file.is_file() else ""
    if not reason:
        raise ValueError("请将具体原因写入该作品的 reason_file。")
    item.update(status=status, reason=reason[:1000])
    write_state(run, state)
    author = state.get("author")
    if author and not item.get("supplement"):
        key = "skipped" if status == "skipped" else "failed"
        library.write_extras(config, lambda v, changes, ei, el: author_docs.stage_author(
            v, config, changes, author["sec_uid"], nickname=author.get("nickname"), **{key: {vid: reason[:300]}}))
    return {"id": vid, "status": status, "reason": item["reason"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(pipeline.DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="创建隔离的本次运行目录")
    for command in ("plan", "next", "prepare-all", "catalog-search", "commit", "skip", "fail", "progress", "status"):
        p = commands.add_parser(command)
        p.add_argument("--run", required=True)
        if command in ("commit", "skip", "fail", "catalog-search"):
            p.add_argument("--id", required=True)
        if command == "catalog-search":
            p.add_argument("--limit", type=int, default=8)
        if command == "prepare-all":
            p.add_argument("--limit", type=int)
        if command == "progress":
            p.add_argument("--notify", action="store_true", help="向当前 cc-connect 会话发送本批进度")
    args = parser.parse_args()
    try:
        config = pipeline.load_json(args.config)
        if args.command == "init":
            result = initialize(config)
        else:
            run, _, _ = load_run(args.run, config)
            with locked(run / ".lock"):
                run, state, frozen = load_run(args.run, config)
                if args.command == "plan":
                    result = plan(run, state, frozen)
                elif args.command == "next":
                    result = next_item(run, state, frozen)
                elif args.command == "prepare-all":
                    if args.limit is not None and not 1 <= args.limit <= 10:
                        raise ValueError("--limit 必须是 1–10，每批最多十条。")
                    result = next_item(run, state, frozen, prepare_all=True, limit=args.limit)
                elif args.command == "catalog-search":
                    checked_input(run, state)
                    result = catalog_search(run, state, frozen, args.id, args.limit)
                elif args.command == "progress":
                    result = progress_report(state)
                    if args.notify:
                        checked_input(run, state)
                        import batch_notice
                        result["notification"] = batch_notice.notify(
                            state, result, lambda: write_state(run, state), config=frozen)
                elif args.command == "commit":
                    if frozen.get("vault") != str(Path(config["vault"]).expanduser().resolve()) or frozen.get("notes_dir") != config.get("notes_dir"):
                        raise ValueError("批次创建于旧知识目录，请重新 init；禁止写回迁移前位置。")
                    result = commit(run, state, frozen, args.id)
                elif args.command in ("skip", "fail"):
                    result = mark_item(run, state, frozen, args.id, "skipped" if args.command == "skip" else "failed")
                else:
                    result = report(state)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
