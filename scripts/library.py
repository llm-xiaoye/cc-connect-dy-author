#!/usr/bin/env python3
"""dy-author vault writes: work-ID deduplication, note rendering and atomic commits.

This package owns its catalog readers, file locks and transaction journal."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

REMOVED_SECTIONS = {"回听定位", "来源", "原音频", "带时间戳的语音转写"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def inside(root, relative):
    root = Path(root).expanduser().resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise ValueError("输出路径必须位于指定知识库内，且不能是符号链接。")
    return path


def read_note(path):
    return read_note_text(Path(path).read_text(encoding="utf-8"))


def read_note_text(text):
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            front = yaml.safe_load(parts[1])
            if isinstance(front, dict):
                return front, parts[2].lstrip("\n")
    return {}, text


def render_note(front, body):
    return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body.rstrip() + "\n"


def note_source_url(config, vault, vid, supplied=None, front=None):
    """Resolve a verified work URL; never guess video versus image from an ID."""
    import review_log
    current = review_log.source_url(supplied, vid) if supplied else None
    previous = front.get("source_url") if isinstance(front, dict) else None
    if previous:
        try:
            previous = review_log.source_url(previous, vid)
        except ValueError:
            if not current:
                raise
            previous = None
    if previous:
        if current and current != previous:
            raise ValueError("本次作品链接与笔记原链接不一致，未覆盖。")
        return previous
    if current:
        return current
    state_path = inside(vault, config.get("review_state_file", review_log.STATE_FILE))
    state = review_log.load_state(state_path, {})
    recorded = state["items"].get(str(vid), {}).get("source_url")
    if recorded:
        return review_log.source_url(recorded, vid)
    cached = review_log.cached_source(config, vid)
    if cached:
        return cached
    raise ValueError("缺少可核对的抖音原作品链接，未保存笔记。")


def scan(vault, replacements=None):
    """Vault notes, including user moves, are the deduplication source of truth."""
    vault = Path(vault).resolve()
    replacements = replacements or {}
    paths = set(vault.rglob("*.md")) | {p for p in replacements if p.suffix == ".md"}
    rows, ids = [], set()
    for path in sorted(paths):
        if path.is_symlink() or any(p.startswith(".") for p in path.relative_to(vault).parts):
            continue
        content = replacements.get(path, ...)
        if content is None:
            continue
        try:
            if content is ...:
                front, _ = read_note(path)
            else:
                front = yaml.safe_load(content.decode().split("---", 2)[1])
        except (ValueError, IndexError, yaml.YAMLError):
            continue
        if not isinstance(front, dict) or not re.fullmatch(r"\d{16,22}", str(front.get("aweme_id", ""))):
            continue
        vid = str(front["aweme_id"])
        if vid in ids:
            raise ValueError(f"同一作品存在多篇笔记，请先消除重复：{path.name}")
        ids.add(vid)
        rows.append(dict(front, aweme_id=vid, path=path.relative_to(vault).as_posix()))
    return rows


def existing_map(vault, replacements=None):
    """One vault scan -> {aweme_id: row}; callers re-check under the lock before writing."""
    return {row["aweme_id"]: row for row in scan(vault, replacements)}


def find_existing(vault, vid):
    return next((Path(vault).resolve() / r["path"] for r in scan(vault) if r["aweme_id"] == str(vid)), None)


def slug(title):
    value = re.sub(r'[\\/:*?"<>|\[\]#^\x00-\x1f：]+', "-", title)
    value = re.sub(r"\s+", "-", value).strip(" .-")
    value = re.sub("-+", "-", value)[:100].rstrip(" .-")
    if not value or value.isdigit():
        raise ValueError("标题必须是可读的知识主题，不能只有数字。")
    return value


def validate_analysis(data, *, strict_topics=False):
    if not isinstance(data, dict):
        raise ValueError("analysis.json 必须是对象。")
    title, tags, category, insight = (data.get(k) for k in ("title", "tags", "category", "insight"))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("analysis.json 缺少文章标题 title。")
    title = " ".join(title.split())
    slug(title)
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
        raise ValueError("tags 需要非空字符串数组。")
    if not isinstance(category, list) or not 2 <= len(category) <= 4:
        raise ValueError("category 需要 2–4 级语义目录，例如 [\"AI\",\"Agent记忆\"]。")
    for part in category:
        if not isinstance(part, str) or not part.strip() or part != part.strip() or len(part) > 24 or re.search(r'[\\/:*?"<>|\[\]#^\x00-\x1f]', part) or part.startswith("."):
            raise ValueError("分类目录名必须简短，不得含路径符号或隐藏目录。")
    from topic_taxonomy import validate_category
    if strict_topics:
        validate_category(category)
    elif category[0] == "面试题库":
        raise ValueError("知识笔记不能放入面试题库目录；题目文档由 interview.json 单独提交。")
    if not isinstance(insight, str) or not 1 <= len(insight.strip()) <= 60:
        raise ValueError("insight 需要一句简短的知识结论，最多 60 字。")
    return {"title": title, "tags": list(dict.fromkeys(t.strip() for t in tags)),
            "category": category, "insight": insight.strip(), "type": str(data.get("type") or "知识笔记")}


def clean_body(body):
    # Remove entire requested appendices, preserving later sections/personal notes.
    personal = re.search(r"^## 我的补充\s*$", body, re.M)
    tail = body[personal.start():] if personal else ""
    main = body[:personal.start()] if personal else body
    lines, result, dropping = main.splitlines(), [], False
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            level, heading = len(match[1]), match[2]
            if dropping and level <= dropping:
                dropping = False
            if not dropping and heading in REMOVED_SECTIONS:
                dropping = level
        if not dropping:
            result.append(line)
    return "\n".join(result).strip() + ("\n\n" + tail if tail else "")


def body_for_title(body, title):
    body = clean_body(body)
    body = re.sub(r"\A# [^\n]+\n*", "", body).strip()
    if not body:
        raise ValueError("知识正文不能为空。")
    if not re.search(r"^## 我的补充\s*$", body, re.M):
        body += "\n\n## 我的补充\n"
    return f"# {title}\n\n{body}\n"


def new_path(vault, config, analysis, occupied=()):
    directory = inside(vault, config.get("notes_dir", "抖音"))
    directory = inside(vault, directory.relative_to(vault) / Path(*analysis["category"]))
    stem = slug(analysis["title"])
    target, suffix = directory / f"{stem}.md", 2
    while target.exists() or target.is_symlink() or target in occupied:
        target = directory / f"{stem}-{suffix}.md"
        suffix += 1
    return target


def metadata_changes(vault, config, changes, events, extra_index=(), extra_logs=()):
    """Render this skill's knowledge index and import history."""
    if not config.get("unified_only"):
        raise ValueError("dy-author 使用知识总索引和导入总日志，请设置 unified_only=true。")
    import unified_store
    rows = scan(vault, changes)
    return unified_store.dy_changes(vault, config, changes, events, rows,
                                    extra_index=extra_index, extra_logs=extra_logs)


def atomic_write(path, content, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".dy-", delete=False) as f:
        temp = Path(f.name)
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    try:
        if exclusive:
            os.link(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(vault):
    # Serialize catalog writes before acquiring this package's note lock.
    with inside(vault, ".unified-catalog.lock").open("a") as shared:
        fcntl.flock(shared, fcntl.LOCK_EX)
        try:
            with inside(vault, ".dy-author-library.lock").open("a") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    marker = Path(vault).resolve() / ".knowledge-migration-in-progress.json"
                    parent_marker = Path(vault).resolve().parent / ".knowledge-migration-in-progress.json"
                    if marker.exists() or parent_marker.exists():
                        raise ValueError("知识库正在迁移，请等待迁移完成后重新导入。")
                    recover(vault)
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            fcntl.flock(shared, fcntl.LOCK_UN)


def recover(vault):
    journal = inside(vault, ".dy-author-transaction.json")
    if not journal.exists():
        return
    data = json.loads(journal.read_text())
    # Never undo an unrelated edit made after an interrupted transaction.
    for row in data["files"]:
        path = inside(vault, row["path"])
        current = path.read_bytes() if path.exists() else None
        before = base64.b64decode(row["before"]) if row["before"] is not None else None
        after = base64.b64decode(row["after"]) if row["after"] is not None else None
        if current not in (before, after):
            raise ValueError(f"中断后文件已被修改，保留现场供恢复：{path}")
    for row in reversed(data["files"]):
        path = inside(vault, row["path"])
        if row["before"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(row["before"]))
    journal.unlink()


def transact(vault, changes):
    journal = inside(vault, ".dy-author-transaction.json")
    records = []
    for path, after in changes.items():
        path = inside(vault, path.relative_to(vault))
        before = path.read_bytes() if path.exists() else None
        records.append({"path": path.relative_to(vault).as_posix(),
                        "before": base64.b64encode(before).decode() if before is not None else None,
                        "after": base64.b64encode(after).decode() if after is not None else None})
    atomic_write(journal, json.dumps({"files": records}, ensure_ascii=False).encode(), exclusive=True)
    applied = []
    try:
        for row in records:
            path = inside(vault, row["path"])
            expected = base64.b64decode(row["before"]) if row["before"] is not None else None
            if (path.read_bytes() if path.exists() else None) != expected:
                raise ValueError(f"文件在提交前发生变化，未覆盖：{path.name}")
            if row["after"] is None:
                path.unlink()
            else:
                atomic_write(path, base64.b64decode(row["after"]), exclusive=row["before"] is None)
            applied.append(row)
    except BaseException:
        # Only roll back files this invocation actually wrote; preserve a racing writer.
        for row in reversed(applied):
            path = inside(vault, row["path"])
            if row["before"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, base64.b64decode(row["before"]))
        journal.unlink(missing_ok=True)
        raise
    journal.unlink()


def author_front(author):
    """Only verified author identity is written; unknown authors are omitted, never guessed."""
    if not author:
        return {}
    nickname, sec_uid = author.get("nickname"), author.get("sec_uid")
    if not isinstance(sec_uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,120}", sec_uid):
        return {}
    result = {"douyin_author": nickname.strip() if isinstance(nickname, str) and nickname.strip() else sec_uid[:12]}
    result["douyin_author_id"] = sec_uid
    return result


def save_note(config, vid, analysis, body, collections=(), images=(), source_url=None, *,
              author=None, before_metadata=None, after_metadata=None):
    """Write one knowledge note plus optional same-transaction extras.

    before_metadata(vault, changes, extra_index, extra_logs, note_path, front) adds
    question documents and their catalog rows. after_metadata(vault, changes, note_path)
    adds author documents/state, which are rendered from the post-change vault scan.
    """
    vault = Path(config["vault"]).expanduser().resolve()
    analysis = validate_analysis(analysis, strict_topics=config.get("notes_dir") == "知识库")
    with locked(vault):
        existing = find_existing(vault, vid)
        if existing:
            return {"status": "exists", "note": str(existing), "written": False}
        source_url = note_source_url(config, vault, vid, source_url)
        path = new_path(vault, config, analysis)
        front = {"title": analysis["title"], "source_url": source_url, "aweme_id": str(vid)}
        front.update(author_front(author))
        front.update({"type": analysis["type"], "tags": analysis["tags"],
                      "insight": analysis["insight"], "created": now()})
        if collections:
            front["collections"] = list(collections)
        from note_assets import stage
        body, changes, attachments = stage(vault, config, path, clean_body(body), images)
        changes[path] = render_note(front, body_for_title(body, analysis["title"])).encode()
        events = [{"time": now(), "action": "新增", "path": path.relative_to(vault).as_posix(),
                   "title": analysis["title"], "source_url": source_url}]
        extra_index, extra_logs, extras = [], [], {}
        if before_metadata:
            extras = before_metadata(vault, changes, extra_index, extra_logs, path, front) or {}
        index, log = metadata_changes(vault, config, changes, events, extra_index, extra_logs)
        if after_metadata:
            after_metadata(vault, changes, path)
        transact(vault, changes)
        return {"status": "saved", "note": str(path), "title": analysis["title"],
                "index": str(index), "log": str(log), "written": True, "attachments": attachments,
                **extras}


def write_extras(config, stage):
    """Transactional write of non-note files (author docs, question docs, catalog rows).

    stage(vault, changes, extra_index, extra_logs) fills `changes`; catalogs are only
    re-rendered when question rows were added. Unchanged files are not rewritten.
    """
    vault = Path(config["vault"]).expanduser().resolve()
    with locked(vault):
        changes, extra_index, extra_logs = {}, [], []
        info = stage(vault, changes, extra_index, extra_logs) or {}
        if extra_index or extra_logs:
            metadata_changes(vault, config, changes, [], extra_index, extra_logs)
        changes = {p: c for p, c in changes.items()
                   if c is None or not p.exists() or p.read_bytes() != c}
        if changes:
            transact(vault, changes)
        return dict(info, changed_files=[str(p) for p in changes])


def catalog(config):
    vault = Path(config["vault"]).resolve()
    rows = scan(vault)
    return {"notes_root": config.get("notes_dir", "抖音"),
            "categories": sorted({str(Path(r["path"]).parent) for r in rows}),
            "notes": [{k: r.get(k) for k in ("path", "title", "tags", "insight")} for r in rows]}
