"""Rough per-run usage estimate (usage.json). Not a billing number.

It counts characters the model is expected to read (material, catalogs, merge
targets) and write (knowledge note, question documents) plus stage timings.
It cannot see model reasoning or tool-call overhead.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

CJK_TOKENS_PER_CHAR = 1.0
OTHER_CHARS_PER_TOKEN = 4.0
NOTE = ("估算：CJK 字符按每字 1 token、其他字符按每 4 字符 1 token 折算；"
        "不含模型思考、工具调用与系统提示的开销，不是计费数字。图片只记页数。")
CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def estimate_tokens(chars_cjk: int, chars_other: int) -> int:
    return round(chars_cjk * CJK_TOKENS_PER_CHAR + chars_other / OTHER_CHARS_PER_TOKEN)


def split_count(text: str) -> tuple[int, int]:
    cjk = len(CJK.findall(text))
    return cjk, len(text) - cjk


def file_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, TypeError):
        return ""


def load(run: Path) -> dict:
    path = Path(run) / "usage.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema_version": 1, "note": NOTE, "items": {}}


def save(run: Path, data: dict) -> None:
    from pipeline import write_json
    write_json(Path(run) / "usage.json", data)


def item(data: dict, vid: str) -> dict:
    return data["items"].setdefault(vid, {"timings": {}, "read": {}, "written": {}})


def record_text(entry: dict, section: str, key: str, text: str) -> None:
    cjk, other = split_count(text)
    entry[section][key] = {"chars": len(text), "cjk": cjk, "other": other}


def record_timing(entry: dict, stage: str, seconds: float) -> None:
    entry["timings"][stage] = round(entry["timings"].get(stage, 0) + seconds, 3)


def mark(entry: dict, key: str) -> None:
    entry[key] = datetime.now(timezone.utc).isoformat()


def summary(data: dict) -> dict:
    read = {"chars": 0, "cjk": 0, "other": 0}
    written = {"chars": 0, "cjk": 0, "other": 0}
    pages, timings = 0, {}
    for entry in data["items"].values():
        for bucket, target in (("read", read), ("written", written)):
            for value in entry.get(bucket, {}).values():
                for k in target:
                    target[k] += value.get(k, 0)
        pages += entry.get("image_pages", 0)
        for stage, seconds in entry.get("timings", {}).items():
            timings[stage] = round(timings.get(stage, 0) + seconds, 3)
    return {"items": len(data["items"]), "read_chars": read["chars"], "written_chars": written["chars"],
            "image_pages": pages,
            "estimated_input_tokens": estimate_tokens(read["cjk"], read["other"]),
            "estimated_output_tokens": estimate_tokens(written["cjk"], written["other"]),
            "timings_seconds": timings, "note": NOTE}
