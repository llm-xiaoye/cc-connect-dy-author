"""Import-state helpers shared with /dy's `.dy-review-state.json` (copied, pruned)."""
from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

STATE_FILE = ".dy-review-state.json"


def display_time(value):
    """Keep the recorded wall clock; do not invent time for date-only history."""
    if value in (None, "", "—"):
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def source_url(value, vid):
    """Persist only a canonical public work URL matching the full work ID."""
    parsed = urlsplit(str(value or ""))
    if (parsed.scheme != "https" or parsed.netloc != "www.douyin.com"
            or not re.fullmatch(r"/(video|note)/" + re.escape(str(vid)) + r"/?", parsed.path)):
        raise ValueError("日志来源必须是与作品 ID 一致的抖音视频或图文地址。")
    return "https://www.douyin.com" + parsed.path.rstrip("/")


def cached_source(config, vid):
    """Own cache first, then the configured read-only project cache used by /dy (data only)."""
    roots = [config.get("cache_dir")] + list(config.get("shared_cache_dirs") or [])
    for root in roots:
        if not root:
            continue
        path = Path(root) / str(vid) / "bundle.json"
        if not path.is_file():
            continue
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if str(bundle.get("aweme_id")) != str(vid):
            raise ValueError("缓存来源与文章作品 ID 不一致，未补写 URL。")
        return source_url(bundle.get("source_url"), vid)
    return ""


def load_state(path, changes):
    raw = changes.get(path, path.read_bytes() if path.exists() else None)
    if raw is None:
        return {"version": 1, "items": {}}
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("items"), dict):
        raise ValueError("复审记录格式不受支持，未覆盖。")
    for vid, item in state["items"].items():
        if not re.fullmatch(r"\d{16,22}", vid) or not isinstance(item, dict):
            raise ValueError("复审记录包含无效的作品条目，未覆盖。")
        if item.get("initial_status") not in ("未登记", "未复审"):
            raise ValueError("复审记录的初始状态无效，未覆盖。")
        if item.get("source_url"):
            source_url(item["source_url"], vid)
        review = item.get("review")
        if review is not None:
            if (not isinstance(review, dict) or review.get("outcome") not in ("复审通过", "复审待完善")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(review.get("note_sha256", "")))
                    or not isinstance(review.get("reviewed_at"), str) or not review["reviewed_at"]):
                raise ValueError("已登记的复审结果不完整，未覆盖。")
    return state


def encode_state(state):
    return (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
