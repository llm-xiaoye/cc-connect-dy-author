"""Send one completed batch notice to the explicitly bound cc-connect session."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

CC_BINARY = Path.home() / ".local/bin/cc-connect"
CC_DATA_DIR = Path.home() / ".cc-connect"


def notify(state, report, persist, *, config=None):
    if not report["batch_finished"]:
        return {"status": "batch_unfinished"}
    project = os.environ.get("CC_PROJECT", "").strip()
    session = os.environ.get("CC_SESSION_KEY", "").strip()
    if not project and not session:
        return {"status": "not_cc_connect"}
    if any(not value or len(value) > 512 or any(c in value for c in "\r\n\x00")
           for value in (project, session)):
        return {"status": "failed", "reason": "缺少或无效的当前 cc-connect 会话标识；未发送。"}
    config = config or {}
    binary = Path(config.get("cc_binary") or CC_BINARY).expanduser().resolve()
    data_dir = Path(config.get("cc_data_dir") or CC_DATA_DIR).expanduser().resolve()
    if not binary.is_file() or not data_dir.is_dir():
        return {"status": "failed", "reason": "cc-connect 程序或数据目录不可用；未发送。"}
    message = report["message"]
    binding = json.dumps([state.get("last_batch_ids", []), message, project, session],
                         ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha256(binding.encode()).hexdigest()
    previous = state.get("batch_notice") or {}
    if previous.get("key") == key:
        return {"status": "already_attempted", "previous_status": previous.get("status")}
    argv = [str(binary), "send", "--project", project, "--session", session,
            "--data-dir", str(data_dir), "--stdin"]
    # Record the attempt before sending: a timeout/crash must not silently resend.
    state["batch_notice"] = {"key": key, "status": "attempting"}
    persist()
    try:
        result = subprocess.run(argv, input=message + "\n", text=True, capture_output=True,
                                timeout=30, check=False)
        outcome = {"status": "sent"} if result.returncode == 0 else {
            "status": "failed", "reason": f"批次提示发送失败（退出码 {result.returncode}）；已保存作品不受影响。"}
    except subprocess.TimeoutExpired:
        outcome = {"status": "delivery_unknown", "reason": "批次提示发送超时，送达状态未知；不自动重发。"}
    except OSError:
        outcome = {"status": "failed", "reason": "批次提示发送程序未能启动；已保存作品不受影响。"}
    state["batch_notice"] = {"key": key, **outcome}
    persist()
    return outcome
