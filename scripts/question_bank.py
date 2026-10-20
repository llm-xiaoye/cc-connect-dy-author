"""面试题库: question documents under 知识库/面试题库/<一级>/<子方向>/.

Question documents never carry `aweme_id` (the vault-wide dedup key of knowledge
notes); they record the works they cover in `source_aweme_ids`. New documents and
merges are staged into the same transaction as the knowledge note.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import uuid

import yaml

import library
import pipeline
import review_log
import topic_taxonomy
import unified_store

QUESTION_TYPE = "面试题"
SECTIONS = ("题目与常见问法", "答题要点", "答题重点", "参考回答", "追问与应对", "模型补充")
RELATED = "相关笔记"
PERSONAL = "我的补充"
INTERVIEW_WORD = "面试"


def root_relative(config):
    return Path(config.get("notes_dir", "知识库")) / config.get("question_root", topic_taxonomy.QUESTION_ROOT)


def mentions_interview(*texts):
    return any(INTERVIEW_WORD in str(text or "") for text in texts)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _front_and_body(raw: bytes):
    text = raw.decode("utf-8")
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            front = yaml.safe_load(parts[1])
            if isinstance(front, dict):
                return front, parts[2].lstrip("\n")
    return {}, text


def _section_text(body, name, limit=400):
    match = re.search(rf"^## {re.escape(name)}\s*$\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match[1].strip()[:limit] if match else ""


def scan_questions(vault, config, replacements=None):
    """All question documents (current bytes, including staged replacements)."""
    vault = Path(vault).resolve()
    replacements = replacements or {}
    root = vault / root_relative(config)
    paths = set(root.rglob("*.md")) if root.is_dir() else set()
    paths |= {p for p in replacements if p.suffix == ".md" and p.is_relative_to(root)}
    rows = []
    for path in sorted(paths):
        if path.is_symlink() or any(p.startswith(".") for p in path.relative_to(vault).parts):
            continue
        raw = replacements.get(path, ...)
        if raw is None:
            continue
        if raw is ...:
            raw = path.read_bytes()
        try:
            front, body = _front_and_body(raw)
        except (UnicodeError, yaml.YAMLError):
            continue
        if front.get("type") != QUESTION_TYPE:
            continue
        if "aweme_id" in front:
            raise ValueError(f"面试题目文档不得包含 aweme_id：{path.name}")
        ids = [str(v) for v in front.get("source_aweme_ids") or []]
        relative = path.relative_to(vault).as_posix()
        rows.append({"path": relative, "title": front.get("title") or path.stem,
                     "category": list(Path(relative).parent.parts[1:]),
                     "tags": front.get("tags") or [], "insight": front.get("insight") or "",
                     "source_aweme_ids": ids, "source_urls": front.get("source_urls") or [],
                     "created": front.get("created"), "sha256": sha256(raw),
                     "forms": _section_text(body, SECTIONS[0])})
    return rows


def by_work(rows):
    result = {}
    for row in rows:
        for vid in row["source_aweme_ids"]:
            result.setdefault(vid, []).append(row)
    return result


def catalog(vault, config):
    rows = scan_questions(vault, config)
    return {"question_root": root_relative(config).as_posix(),
            "suggested_first_levels": list(topic_taxonomy.QUESTION_TOPICS),
            "categories": sorted({"/".join(r["category"]) for r in rows}),
            "questions": [{k: r[k] for k in ("path", "title", "category", "tags", "insight", "forms",
                                              "sha256", "source_aweme_ids")} for r in rows]}


def validate_body(body):
    body = body.strip()
    if not body or body.startswith("---"):
        raise ValueError("题目正文不能为空，也不要包含 YAML。")
    blocks = pipeline.markdown_blocks(body)
    if any(level == 1 for level, _, _ in blocks):
        raise ValueError("题目正文不要写一级标题，脚本按 title 生成。")
    h2 = [(i, name.strip()) for i, (level, name, _) in enumerate(blocks) if level == 2]
    names = [name for _, name in h2]
    for forbidden in (RELATED, PERSONAL):
        if forbidden in names:
            raise ValueError(f"题目正文不要包含“{forbidden}”，脚本维护该小节。")
    for name in SECTIONS:
        if names.count(name) != 1:
            raise ValueError(f"题目正文需要且只能有一个“## {name}”。")
    positions = [names.index(name) for name in SECTIONS]
    if positions != sorted(positions):
        raise ValueError("题目正文小节顺序应为：" + " → ".join(SECTIONS))
    for pos, (start, name) in enumerate(h2):
        end = h2[pos + 1][0] if pos + 1 < len(h2) else len(blocks)
        lines = [line for _, _, content in blocks[start:end] for line in content]
        if name in SECTIONS and not any(line.strip() for line in lines):
            raise ValueError(f"“## {name}”不能为空。")
        if name == "答题要点":
            items = [line for line in blocks[start][2] if re.match(r"^(?:[-*+]|\d+[.)])\s+\S", line)]
            if not 3 <= len(items) <= 6:
                raise ValueError("“答题要点”需要 3–6 条顶层列表项（口述骨架）。")
    return body


def load_interview(path, item_dir):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or not questions:
        raise ValueError("interview.json 需要非空 questions 数组。")
    item_dir = Path(item_dir).resolve()
    result, targets = [], set()
    for q in questions:
        if not isinstance(q, dict) or q.get("action") not in ("new", "merge"):
            raise ValueError("每道题需要 action=new 或 merge。")
        title = q.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("每道题需要 title。")
        library.slug(title)
        tags = q.get("tags")
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
            raise ValueError("每道题需要非空 tags 字符串数组。")
        insight = q.get("insight")
        if not isinstance(insight, str) or not 1 <= len(insight.strip()) <= 60:
            raise ValueError("每道题需要 insight，最多 60 字。")
        body_file = (item_dir / str(q.get("body_file", ""))).resolve()
        if not body_file.is_relative_to(item_dir) or not body_file.is_file():
            raise ValueError("body_file 必须是本条目录中的已写文件。")
        entry = {"action": q["action"], "title": " ".join(title.split()),
                 "tags": list(dict.fromkeys(t.strip() for t in tags)), "insight": insight.strip(),
                 "body": validate_body(body_file.read_text(encoding="utf-8")), "body_file": str(body_file)}
        if q["action"] == "new":
            entry["category"] = topic_taxonomy.validate_question_category(q.get("category"))
        else:
            target = q.get("target")
            if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{64}", str(q.get("expected_sha256", ""))):
                raise ValueError("merge 需要 target 与读取时的 expected_sha256。")
            if target in targets:
                raise ValueError("同一题目文档在一次提交中只能合并一次。")
            targets.add(target)
            entry.update(target=target, expected_sha256=q["expected_sha256"])
        result.append(entry)
    return result


def related_line(note_relative, note_title, url):
    target = note_relative[:-3] if note_relative.endswith(".md") else note_relative
    label = str(note_title).replace("|", "-").replace("]", "")
    return f"- [[{target}|{label}]]｜[原作品]({url})"


def split_tail(body):
    """Return (content, related_lines, personal_tail) of an existing question body."""
    personal = re.search(rf"^## {PERSONAL}\s*$", body, re.M)
    tail = body[personal.start():].rstrip() + "\n" if personal else f"## {PERSONAL}\n"
    head = body[:personal.start()] if personal else body
    related = re.search(rf"^## {RELATED}\s*$", head, re.M)
    lines = []
    if related:
        lines = [line for line in head[related.end():].splitlines() if line.strip()]
        head = head[:related.start()]
    return head.strip(), lines, tail


def render(front, title, content, related, tail):
    body = f"# {title}\n\n{content.strip()}\n\n## {RELATED}\n\n" + "\n".join(related) + "\n\n" + tail
    return library.render_note(front, body).encode("utf-8")


def index_cells(title, relative, tags, insight, created):
    esc = unified_store.escape
    return [unified_store.note_link(title, relative), QUESTION_TYPE, esc("、".join(tags)),
            esc(insight), esc(review_log.display_time(created))]


def stage_questions(vault, config, changes, extra_index, extra_logs, *, vid, url, note_path,
                    note_title, questions, author_front=None):
    """Stage new/merged question documents and their catalog rows; return a summary."""
    vault = Path(vault).resolve()
    author_name = unified_store.verified_author_name(author_front or {})
    existing = {row["path"]: row for row in scan_questions(vault, config, changes)}
    covered = by_work(existing.values())
    if vid in covered:
        raise ValueError("该作品已收录进面试题库，不重复写入：" + covered[vid][0]["path"])
    note_relative = Path(note_path).resolve().relative_to(vault).as_posix()
    line = related_line(note_relative, note_title, url)
    stamp = library.now()
    summary, backups, occupied = [], {}, set()
    for q in questions:
        if q["action"] == "new":
            directory = library.inside(vault, root_relative(config) / Path(*q["category"][1:]))
            stem = library.slug(q["title"])
            target, n = directory / f"{stem}.md", 2
            while target.exists() or target.is_symlink() or target in changes or target in occupied:
                target, n = directory / f"{stem}-{n}.md", n + 1
            occupied.add(target)
            front = {"title": q["title"], "type": QUESTION_TYPE, "tags": q["tags"], "insight": q["insight"],
                     "source_aweme_ids": [vid], "source_urls": [url], "created": stamp, "updated": stamp}
            changes[target] = render(front, q["title"], q["body"], [line], f"## {PERSONAL}\n")
            relative = target.relative_to(vault).as_posix()
            action, created = "新增面试题", stamp
        else:
            relative = q["target"]
            row = existing.get(relative)
            if not row:
                raise ValueError(f"合并目标不是现有面试题目文档：{relative}")
            target = library.inside(vault, relative)
            raw = changes[target] if target in changes else target.read_bytes()
            if sha256(raw) != q["expected_sha256"]:
                raise ValueError(f"题目文档在读取后被修改，请重新读取再合并：{relative}")
            if vid in row["source_aweme_ids"]:
                raise ValueError(f"该作品已收录在题目文档中，不重复并入：{relative}")
            if q["title"] != row["title"]:
                raise ValueError("合并时保留原题目标题；改名请手动处理。")
            front, old_body = _front_and_body(raw)
            if "aweme_id" in front:
                raise ValueError("面试题目文档不得包含 aweme_id。")
            _, related, tail = split_tail(re.sub(r"\A# [^\n]+\n*", "", old_body.strip()))
            if line not in related:
                related.append(line)
            front.update(tags=q["tags"], insight=q["insight"],
                         source_aweme_ids=list(dict.fromkeys([*map(str, front.get("source_aweme_ids") or []), vid])),
                         source_urls=list(dict.fromkeys([*(front.get("source_urls") or []), url])),
                         updated=stamp)
            backups[relative] = base64.b64encode(raw).decode()
            changes[target] = render(front, row["title"], q["body"], related, tail)
            action, created = "合并面试题", front.get("created") or row.get("created")
        extra_index.append({"path": relative,
                            "cells": index_cells(q["title"], relative, q["tags"], q["insight"], created)})
        extra_logs.append([review_log.display_time(stamp), "DY", action,
                           unified_store.note_link(q["title"], relative),
                           unified_store.escape(unified_store.topic(relative)), f"[原作品]({url})", "未复审",
                           unified_store.escape(author_name)])
        summary.append({"action": action, "path": str(target), "title": q["title"]})
    if backups:
        backup = Path(config["cache_dir"]).expanduser().resolve() / "revisions" / (
            datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        (backup / "before.json").write_text(json.dumps(backups, ensure_ascii=False, indent=2), encoding="utf-8")
        summary.append({"action": "备份", "path": str(backup / "before.json")})
    return summary
