"""Shared fixtures for dy-author tests. Paths are derived from resolved temp dirs."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import wave

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline  # noqa: E402
import unified_store  # noqa: E402

AUTHOR = "MS4wLjABAAAATestAuthorSecUid0001"
OTHER_AUTHOR = "MS4wLjABAAAATestAuthorSecUid0002"


def skeleton(section="机制", extra="") -> str:
    return ("> 一句话结论：状态与历史需要分开读取。\n\n"
            "## 核心要点\n\n- 要点一\n- 要点二\n- 要点三\n\n"
            f"## {section}\n\n具体机制说明。{extra}\n\n"
            "## 模型补充\n\n### 补充知识\n\n补充说明。\n\n### 纠错与边界\n\n边界说明。\n\n"
            "### 模型判断\n\n判断与依据。\n\n"
            "## 自测\n\n题目：怎样区分？\n\n答案：看是否需要当前状态。\n")


def analysis(title="记忆的状态与历史", category=("AI", "Agent", "记忆管理"), section="机制", **extra):
    data = {"title": title, "category": list(category), "tags": ["agent", "memory"],
            "insight": "分开状态与历史证据",
            "learning_review": {"learning_goal": "能区分状态与历史",
                                "key_claims": [{"claim": "机制结论", "evidence": "转写片段", "section": section},
                                               {"claim": "模型判断", "evidence": "材料例子", "section": "模型判断"}],
                                "application": "代入例子", "uncertainties": []}}
    data.update(extra)
    return data


def question_body(extra="") -> str:
    return ("## 题目与常见问法\n\n怎样设计意图识别？\n\n"
            "## 答题要点\n\n- 先分层\n- 再路由\n- 最后兜底\n\n"
            "## 答题重点\n\n考察工程取舍。\n\n"
            "## 参考回答\n\n我会分三层。\n\n"
            "## 追问与应对\n\n状态怎么存？\n\n"
            f"## 模型补充\n\n比例只是示例。{extra}\n")


def temp_root(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name).resolve()


def make_vault(root: Path) -> Path:
    vault = root / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    (vault / unified_store.INDEX_NAME).write_bytes(unified_store.render_index([]))
    (vault / unified_store.LOG_NAME).write_bytes(unified_store.render_log([]))
    return vault


def make_config(root: Path, vault: Path, **extra) -> dict:
    config = {"vault": str(vault), "cache_dir": str(root / "cache"), "notes_dir": "知识库",
              "unified_only": True, "attachments_dir": "附件", "authors_dir": "抖音作者",
              "question_root": "面试题库", "review_state_file": ".dy-review-state.json",
              "asr_model": "fixture", "browser": None, "prepare_workers": 1, "prepare_batch": 10}
    config.update(extra)
    return config


def write_wav(path: Path, frames=160):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * frames)


def video_bundle(cache: Path, vid: str, *, author_id=None, author="Test", title="Synthetic source",
                 text="Synthetic knowledge evidence.", kind="video") -> Path:
    target = cache / vid
    write_wav(target / "audio.wav")
    digest = hashlib.sha256((target / "audio.wav").read_bytes()).hexdigest()
    pipeline.write_json(target / "bundle.json", {
        "aweme_id": vid, "source_url": f"https://www.douyin.com/{kind}/{vid}", "media_kind": "video",
        "title": title, "author": author, "author_id": author_id, "collections": [], "audio_sha256": digest})
    pipeline.write_json(target / "transcript.json", {
        "aweme_id": vid, "text": text, "segments": [], "asr_model": "fixture", "audio_sha256": digest})
    return target / "bundle.json"


def existing_note(vault: Path, vid: str, relative="知识库/AI/旧主题/用户改名.md", extra_front="") -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nsource_url: \"https://www.douyin.com/video/{vid}\"\ntitle: 用户标题\n"
                    f"aweme_id: '{vid}'\n{extra_front}type: 知识笔记\ntags:\n- 用户标签\ninsight: 旧结论\n"
                    f"created: '2026-09-01T10:00:00-05:00'\n---\n\n# 用户标题\n\n正文。\n\n## 我的补充\n请完整保留。\n",
                    encoding="utf-8")
    return path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
