#!/usr/bin/env python3
"""Configure a downloaded skill using only the installer's own local paths."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def directory(value, label):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label} 必须是已有目录")
    return path


def configure(args, root=ROOT):
    root = Path(root).resolve(strict=True)
    vault = directory(args.vault, "Vault")
    config_path = root / "config.json"
    if config_path.exists() or config_path.is_symlink():
        raise ValueError("config.json 已存在；请备份后手工更新，不自动覆盖个人配置")
    config = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    config.update(vault=str(vault), cache_dir=str(root / ".cache"),
                  python=os.path.abspath(sys.executable), material_cache_dirs=[])
    if args.no_browser:
        if args.chrome_profile or args.chrome_cache:
            raise ValueError("--no-browser 不能与 Chrome 路径同时使用")
        config.update(browser=None, browser_profile=None, browser_cache_dir=None,
                      auto_browser_recovery=False)
    else:
        default_root = Path.home() / "Library/Application Support/Google/Chrome"
        profile = directory(args.chrome_profile or default_root / "Default", "Chrome profile")
        if args.chrome_cache:
            cache = directory(args.chrome_cache, "Chrome Cache_Data")
        elif profile.parent == default_root.resolve():
            cache = directory(Path.home() / "Library/Caches/Google/Chrome" / profile.name / "Cache/Cache_Data",
                              "Chrome Cache_Data")
        else:
            raise ValueError("自定义 Chrome user-data 目录须同时指定 --chrome-cache")
        config.update(browser_profile=str(profile), browser_cache_dir=str(cache))
    if bool(args.cc_binary) != bool(args.cc_data_dir):
        raise ValueError("--cc-binary 和 --cc-data-dir 必须一起指定")
    if args.cc_binary:
        binary = Path(args.cc_binary).expanduser().resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("cc-connect binary 必须是可执行文件")
        config.update(cc_binary=str(binary), cc_data_dir=str(directory(args.cc_data_dir, "cc-connect data")))
    skill_link = command_path = None
    command_text = None
    if args.cc_workspace:
        workspace = directory(args.cc_workspace, "cc-connect work_dir")
        skill_link = workspace / ".claude/skills/dy-author"
        command_path = workspace / ".claude/commands/dy-author.md"
        if skill_link.exists() or skill_link.is_symlink():
            raise ValueError("work_dir 已有 dy-author skill；不覆盖现有安装")
        if command_path.exists() or command_path.is_symlink():
            raise ValueError("work_dir 已有 dy-author 命令；不覆盖现有安装")
        template = (root / "integrations/cc-connect/dy-author.md").read_text(encoding="utf-8")
        if template.count("@@SKILL_DIR@@") != 1:
            raise ValueError("命令模板的 skill 路径标记必须恰好一个")
        if any(char in str(root) for char in "\r\n`"):
            raise ValueError("安装路径不能含换行或 Markdown backtick")
        command_text = template.replace("@@SKILL_DIR@@", str(root))
    # All supplied paths and existing installation conflicts are checked before writes.
    with config_path.open("x", encoding="utf-8") as handle:
        os.chmod(config_path, 0o600)
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    if skill_link is not None:
        skill_link.parent.mkdir(parents=True, exist_ok=True)
        command_path.parent.mkdir(parents=True, exist_ok=True)
        skill_link.symlink_to(root, target_is_directory=True)
        with command_path.open("x", encoding="utf-8") as handle:
            handle.write(command_text)
    return {"status": "configured", "config_file": str(config_path),
            "skill_link": str(skill_link) if skill_link else None,
            "command_file": str(command_path) if command_path else None}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--vault", required=True, help="已有 Obsidian Vault 的绝对路径")
    result.add_argument("--chrome-profile", help="Chrome profile，例如 Default 或 Profile 1 的完整目录")
    result.add_argument("--chrome-cache", help="对应 profile 的 Cache_Data 完整目录")
    result.add_argument("--no-browser", action="store_true", help="明确禁用浏览器读取与自动恢复")
    result.add_argument("--cc-workspace", help="cc-connect 项目的实际 work_dir；安装技能链接与命令")
    result.add_argument("--cc-binary", help="cc-connect 可执行文件的绝对路径")
    result.add_argument("--cc-data-dir", help="当前 cc-connect 服务使用的实际 data-dir")
    return result


def main():
    try:
        print(json.dumps(configure(parser().parse_args()), ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
