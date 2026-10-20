"""Small model views over complete, freshly scanned knowledge/question catalogs."""
from __future__ import annotations

import json
from pathlib import Path

import library
import question_bank


def compact_catalogs(config):
    notes = library.catalog(config)
    questions = question_bank.catalog(Path(config["vault"]).expanduser().resolve(), config)
    return (
        {"schema_version": 2, "notes_root": notes["notes_root"],
         "categories": notes["categories"], "note_count": len(notes["notes"]),
         "retrieval": "catalog-search：按完整材料提取检索词，再查候选；不按标题猜内容。"},
        {"schema_version": 2, "question_root": questions["question_root"],
         "suggested_first_levels": questions["suggested_first_levels"],
         "categories": questions["categories"], "question_count": len(questions["questions"]),
         "retrieval": "面试类必须 catalog-search；合并前读目标全文并核对 sha256。"},
    )


def read_terms(path):
    text = Path(path).read_text(encoding="utf-8")
    if len(text) > 2000:
        raise ValueError("检索词文件过长；每行一个核心主题或技术概念。")
    terms = list(dict.fromkeys(line.strip().casefold() for line in text.splitlines() if line.strip()))
    if not 1 <= len(terms) <= 20 or any(len(term) > 80 for term in terms):
        raise ValueError("需要 1–20 个检索词，每个不超过 80 字符。")
    return terms


def matches(rows, terms, limit):
    ranked = []
    for row in rows:
        searchable = json.dumps({key: row.get(key) for key in ("title", "tags", "insight", "forms", "path")},
                                ensure_ascii=False).casefold()
        hits = [term for term in terms if term in searchable]
        if hits:
            title = str(row.get("title") or "").casefold()
            score = len(hits) + 2 * sum(term in title for term in hits)
            ranked.append((score, row["path"], row, hits))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    candidates = []
    for _, _, row, hits in ranked[:limit]:
        candidate = {key: row[key] for key in ("path", "title", "category", "sha256") if key in row}
        candidate.update(tags=list(row.get("tags") or [])[:12],
                         insight=str(row.get("insight") or "")[:300], matched_terms=hits)
        if "forms" in row:
            candidate["forms"] = str(row["forms"])[:400]
            candidate["source_work_count"] = len(row.get("source_aweme_ids") or [])
        candidates.append(candidate)
    return {"total_matches": len(ranked), "has_more": len(ranked) > len(candidates),
            "candidates": candidates}


def search(config, terms, *, interview, limit=8):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("候选数量必须是 1–50。")
    notes = library.catalog(config)
    questions = question_bank.catalog(Path(config["vault"]).expanduser().resolve(), config) if interview else None
    result = {"terms": terms, "ranking_is_semantic_judgment": False,
              "knowledge": matches(notes["notes"], terms, limit),
              "questions": matches(questions["questions"], terms, limit) if questions else
                           {"not_required": True, "total_matches": 0, "has_more": False, "candidates": []}}
    # Bound the model response as well as the number of rows. The script still
    # searches the entire vault; has_more always exposes omitted candidates.
    while len(json.dumps(result, ensure_ascii=False)) > 12000:
        group = max((result["knowledge"], result["questions"]), key=lambda value: len(value["candidates"]))
        if not group["candidates"]:
            raise ValueError("候选字段超出响应预算，请缩小检索词范围。")
        group["candidates"].pop()
        group["has_more"] = True
    return result
