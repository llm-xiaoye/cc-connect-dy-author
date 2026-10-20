"""Author documents under <vault>/抖音作者/ and dy-author's per-author state.

Rows are always recomputed from a vault scan (knowledge notes by aweme_id and
douyin_author_id, question documents by source_aweme_ids), so an author document
can be regenerated at any time. It never carries `aweme_id`.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

import library
import question_bank

STATE_NAME = ".dy-author-state.json"
START, END = "%% dy-author:managed:start %%", "%% dy-author:managed:end %%"
DOC_TYPE = "抖音作者"
UNKNOWN_TYPE = "抖音作者（未识别）"
UNKNOWN_NAME = "未识别作者.md"
SEC_UID_RE = re.compile(r"[A-Za-z0-9_-]{20,120}")
STATUS_LABEL = {True: "完整", False: "不完整", None: "未核对"}


def authors_root(vault, config):
    return library.inside(vault, config.get("authors_dir", "抖音作者"))


def state_path(vault, config):
    return library.inside(vault, Path(config.get("authors_dir", "抖音作者")) / STATE_NAME)


def load_state(vault, config, changes=None):
    path = state_path(vault, config)
    changes = changes or {}
    raw = changes.get(path, path.read_bytes() if path.exists() else None)
    if raw is None:
        return {"version": 1, "authors": {}}
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("authors"), dict):
        raise ValueError("作者状态文件格式不受支持，未覆盖。")
    for sec_uid, entry in state["authors"].items():
        if not SEC_UID_RE.fullmatch(sec_uid) or not isinstance(entry, dict):
            raise ValueError("作者状态包含无效作者条目，未覆盖。")
    return state


def encode_state(state):
    return (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def author_entry(state, sec_uid, nickname=None):
    if not SEC_UID_RE.fullmatch(str(sec_uid)):
        raise ValueError("作者 sec_uid 格式错误。")
    entry = state["authors"].setdefault(sec_uid, {"nickname": nickname or "", "doc_path": None,
                                                  "listing": None, "skipped": {}, "failed": {}})
    entry.setdefault("skipped", {})
    entry.setdefault("failed", {})
    if nickname:
        entry["nickname"] = nickname
    return entry


def safe_name(nickname, sec_uid):
    value = re.sub(r'[\\/:*?"<>|\[\]#^\x00-\x1f]+', "-", str(nickname or ""))
    value = re.sub(r"\s+", " ", value).strip(" .-")[:60].rstrip(" .-")
    return value or sec_uid[:12]


def _front_body(raw: bytes):
    text = raw.decode("utf-8")
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            front = yaml.safe_load(parts[1])
            if isinstance(front, dict):
                return front, parts[2].lstrip("\n")
    return {}, text


def author_docs(vault, config, changes=None):
    """{sec_uid: path} of existing author documents (staged replacements included)."""
    root = authors_root(vault, config)
    changes = changes or {}
    paths = set(root.glob("*.md")) if root.is_dir() else set()
    paths |= {p for p in changes if p.suffix == ".md" and p.parent == root}
    found = {}
    for path in sorted(paths):
        raw = changes.get(path, ...)
        if raw is None or path.is_symlink():
            continue
        raw = path.read_bytes() if raw is ... else raw
        try:
            front, _ = _front_body(raw)
        except (UnicodeError, yaml.YAMLError):
            continue
        if front.get("type") == DOC_TYPE and isinstance(front.get("douyin_author_id"), str):
            if "aweme_id" in front:
                raise ValueError(f"作者文档不得包含 aweme_id：{path.name}")
            if front["douyin_author_id"] in found:
                raise ValueError(f"同一作者存在多份作者文档：{path.name}")
            found[front["douyin_author_id"]] = path
    return found


def doc_path(vault, config, sec_uid, nickname, changes=None):
    existing = author_docs(vault, config, changes)
    if sec_uid in existing:
        return existing[sec_uid]
    root = authors_root(vault, config)
    changes = changes or {}
    name = safe_name(nickname, sec_uid)

    def free(path):
        return not (path.exists() or path.is_symlink() or path in changes or path.name == UNKNOWN_NAME)

    target = root / f"{name}.md"
    if free(target):
        return target
    # Every sec_uid starts with the same "MS4wLjABAAAA" header, so use its tail.
    for size in (6, 10, len(sec_uid)):
        target = root / f"{name}-{suffix_for(sec_uid, size)}.md"
        if free(target):
            return target
    raise ValueError(f"作者文档文件名被占用，未覆盖：{target.name}")


def suffix_for(sec_uid, size=6):
    return re.sub(r"[^A-Za-z0-9]", "", sec_uid)[-size:]


def wikilink(relative, title):
    target = relative[:-3] if relative.endswith(".md") else relative
    label = str(title or Path(target).name).replace("|", "-").replace("]", "").replace("[", "")
    return f"[[{target}\\|{label}]]"


def cell(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


KIND_LABEL = {"note": "图文", "article": "文章"}


def work_link(vid, kind):
    path = "note" if kind == "note" else "video"
    return f"[{vid}](https://www.douyin.com/{path}/{vid})"


def split_managed(old: str, name: str):
    if old.count(START) != 1 or old.count(END) != 1 or old.index(START) > old.index(END):
        raise ValueError(f"作者文档的管理区域缺失或重复，未覆盖：{name}")
    prefix, rest = old.split(START, 1)
    _, suffix = rest.split(END, 1)
    return prefix, suffix


def rows_for_author(vault, config, sec_uid, entry, changes):
    notes = library.existing_map(vault, changes)
    questions = question_bank.by_work(question_bank.scan_questions(vault, config, changes))
    listing = entry.get("listing") or {}
    order, kinds = [], {}
    for item in listing.get("items") or []:
        if item["id"] not in kinds:
            order.append(item["id"])
            kinds[item["id"]] = item.get("kind", "video")
    for vid, row in notes.items():
        if row.get("douyin_author_id") == sec_uid and vid not in kinds:
            order.append(vid)
            kinds[vid] = "note" if "/note/" in str(row.get("source_url", "")) else "video"
    rows = []
    for vid in order:
        note = notes.get(vid)
        if note:
            status, note_cell = "已入库", wikilink(note["path"], note.get("title"))
        elif vid in entry["skipped"]:
            status, note_cell = "跳过", cell(entry["skipped"][vid].get("reason", ""))
        elif kinds[vid] == "article" or vid in (entry.get("unsupported") or {}):
            reason = (entry.get("unsupported") or {}).get(vid, {}).get("reason") or "抖音文章暂不支持，未导入"
            status, note_cell = "暂不支持", cell(reason)
        elif vid in entry["failed"]:
            status, note_cell = "失败", cell(entry["failed"][vid].get("reason", ""))
        else:
            status, note_cell = "待导入", ""
        qs = "、".join(wikilink(q["path"], q["title"]) for q in questions.get(vid, []))
        rows.append({"id": vid, "kind": kinds[vid], "status": status, "note": note_cell, "questions": qs})
    return rows


def render_author(vault, config, sec_uid, entry, changes, *, path):
    rows = rows_for_author(vault, config, sec_uid, entry, changes)
    listing = entry.get("listing")
    complete = listing.get("complete") if listing else None
    reason = listing.get("reason") if listing else ("仅根据已入库笔记回填，主页清单未核对" if listing is None else None)
    counts = {k: sum(r["status"] == k for r in rows) for k in ("已入库", "待导入", "跳过", "失败", "暂不支持")}
    checked = library.now()
    status_text = STATUS_LABEL[complete] + (f"（{reason}）" if complete is False and reason else "")
    summary = (f"最近核对：{checked[:19].replace('T', ' ')}｜清单{status_text}｜共 {len(rows)}："
               f"已入库 {counts['已入库']}，待导入 {counts['待导入']}，跳过 {counts['跳过']}，失败 {counts['失败']}"
               + (f"，暂不支持 {counts['暂不支持']}" if counts["暂不支持"] else ""))
    table = ["| 作品 | 类型 | 状态 | 知识笔记 | 面试题 |", "| --- | --- | --- | --- | --- |"]
    for r in rows:
        table.append(f"| {work_link(r['id'], r['kind'])} | {KIND_LABEL.get(r['kind'], '视频')} | "
                     f"{r['status']} | {r['note']} | {r['questions']} |")
    region = f"{START}\n\n{summary}\n\n" + "\n".join(table) + f"\n\n{END}"
    old = changes.get(path, path.read_bytes() if path.exists() else None)
    front = {}
    prefix, suffix = "", "\n"
    if old is not None:
        front, body = _front_body(old)
        if front.get("douyin_author_id") not in (None, sec_uid):
            raise ValueError(f"作者文档属于其他作者，未覆盖：{path.name}")
        prefix, suffix = split_managed(body, path.name)
    front = {k: v for k, v in front.items() if k != "aweme_id"}
    front.update({"type": DOC_TYPE, "douyin_author": entry.get("nickname") or sec_uid[:12],
                  "douyin_author_id": sec_uid, "profile_url": f"https://www.douyin.com/user/{sec_uid}",
                  "updated": checked, "listing_status": STATUS_LABEL[complete],
                  "listing_reason": reason or ""})
    body = prefix + region + suffix
    return library.render_note(front, body).encode("utf-8"), counts, len(rows)


def stage_author(vault, config, changes, sec_uid, *, nickname=None, listing=None,
                 skipped=None, failed=None, clear_failed=(), unsupported=None):
    """Update state for one author and stage its document; returns summary info."""
    vault = Path(vault).resolve()
    state = load_state(vault, config, changes)
    entry = author_entry(state, sec_uid, nickname)
    if listing is not None:
        entry["listing"] = listing
    for vid, reason in (skipped or {}).items():
        entry["skipped"][vid] = {"reason": reason, "at": library.now()}
        entry["failed"].pop(vid, None)
    for vid, reason in (failed or {}).items():
        entry["failed"][vid] = {"reason": reason, "at": library.now()}
    for vid in clear_failed:
        entry["failed"].pop(vid, None)
    for vid, reason in (unsupported or {}).items():
        # Not a retryable failure: the type is simply not imported yet.
        entry.setdefault("unsupported", {})[vid] = {"reason": reason, "at": library.now()}
        entry["failed"].pop(vid, None)
    path = doc_path(vault, config, sec_uid, entry.get("nickname"), changes)
    content, counts, total = render_author(vault, config, sec_uid, entry, changes, path=path)
    entry["doc_path"] = path.relative_to(vault).as_posix()
    changes[path] = content
    changes[state_path(vault, config)] = encode_state(state)
    return {"author_doc": str(path), "counts": counts, "total": total}


def render_unknown(vault, config, rows, changes):
    """rows: [{"id","path","title","url","reason"}] of notes whose author could not be verified."""
    root = authors_root(vault, config)
    path = root / UNKNOWN_NAME
    table = ["| 作品 | 知识笔记 | 原因 |", "| --- | --- | --- |"]
    for r in rows:
        link = f"[{r['id']}]({r['url']})" if r.get("url") else r["id"]
        table.append(f"| {link} | {wikilink(r['path'], r.get('title'))} | {cell(r.get('reason'))} |")
    region = (f"{START}\n\n最近核对：{library.now()[:19].replace('T', ' ')}｜共 {len(rows)} 条作者未能核实\n\n"
              + "\n".join(table) + f"\n\n{END}")
    old = changes.get(path, path.read_bytes() if path.exists() else None)
    prefix, suffix = "", "\n"
    if old is not None:
        front, body = _front_body(old)
        if front.get("type") != UNKNOWN_TYPE:
            raise ValueError("未识别作者文档类型不符，未覆盖。")
        prefix, suffix = split_managed(body, path.name)
    front = {"type": UNKNOWN_TYPE, "updated": library.now()}
    changes[path] = library.render_note(front, prefix + region + suffix).encode("utf-8")
    return path


def insert_author_keys(raw: bytes, nickname: str, sec_uid: str) -> bytes:
    """Insert the two author keys right after `aweme_id:`; body bytes stay identical."""
    if not SEC_UID_RE.fullmatch(str(sec_uid)):
        raise ValueError("作者 sec_uid 格式错误。")
    nickname = str(nickname or "").strip() or sec_uid[:12]
    text = raw.decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError("笔记缺少属性区，未补作者。")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("笔记属性区未闭合，未补作者。")
    head, rest = text[:end + 1], text[end + 1:]
    if re.search(r"^douyin_author(?:_id)?:", head, re.M):
        raise ValueError("笔记已含作者属性。")
    match = re.search(r"^aweme_id:.*\n", head, re.M)
    if not match:
        raise ValueError("笔记属性区没有 aweme_id 行。")
    lines = (f"douyin_author: {json.dumps(nickname, ensure_ascii=False)}\n"
             f"douyin_author_id: {json.dumps(sec_uid, ensure_ascii=False)}\n")
    new = head[:match.end()] + lines + head[match.end():] + rest
    old_front, old_body = library.read_note_text(text)
    new_front, new_body = library.read_note_text(new)
    if new_body != old_body or new_front != dict(old_front, douyin_author=nickname, douyin_author_id=sec_uid):
        raise ValueError("补作者属性后回读不一致，未写入。")
    if not new.endswith(rest):
        raise ValueError("补作者属性改变了正文字节，未写入。")
    return new.encode("utf-8")
