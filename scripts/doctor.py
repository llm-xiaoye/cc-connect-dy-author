#!/usr/bin/env python3
"""Local installation checks only: no network, imports of ML engines, or Vault writes."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    issues = []
    supported = platform.system() == "Darwin" and platform.machine() == "arm64"
    if not supported:
        issues.append("完整视频 ASR/图文 OCR 路线需要 macOS Apple Silicon；其他平台未验证")
    if sys.version_info[:2] != (3, 12):
        issues.append("请使用 Python 3.12")
    path = ROOT / "config.json"
    if not path.is_file():
        print(json.dumps({"status": "needs_setup", "issues": ["请先运行 scripts/configure.py"]}, ensure_ascii=False))
        return 1
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("vault",):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_dir():
            issues.append(f"{key} 目录不可用")
    if config.get("python") != os.path.abspath(sys.executable):
        issues.append("当前解释器入口与 config.json 的 python 不一致")
    if config.get("browser"):
        for key in ("browser_profile", "browser_cache_dir"):
            if not isinstance(config.get(key), str) or not Path(config[key]).is_dir():
                issues.append(f"{key} 目录不可用")
    modules = {name: importlib.util.find_spec(name) is not None for name in
               ("yaml", "PIL", "f2", "yt_dlp", "mlx_whisper", "browser_cookie3", "imageio_ffmpeg")}
    issues.extend(f"缺少依赖模块：{name}" for name, present in modules.items() if not present)
    if not Path("/usr/bin/swift").is_file():
        issues.append("缺少 Swift：请安装 Apple Command Line Tools")
    print(json.dumps({"status": "ready" if not issues else "needs_setup", "issues": issues,
                      "dependency_modules_present": modules,
                      "scope": "local_paths_and_dependency_discovery_only_not_live_Douyin_test"}, ensure_ascii=False))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
