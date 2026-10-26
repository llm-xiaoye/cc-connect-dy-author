"""Read existing Chrome profiles and Douyin session metadata without opening tabs.

Cookie values stay inside the reader. Returned data is safe to put in run state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

AUTH_COOKIES = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt"}


def user_home():
    # ZCode may replace HOME with its application sandbox. Chrome uses the OS user.
    if sys.platform == "win32":
        return Path(os.environ["USERPROFILE"]).resolve()
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def chrome_roots(home=None):
    home = Path(home) if home is not None else user_home()
    if sys.platform == "darwin":
        return (home / "Library/Application Support/Google/Chrome",
                home / "Library/Caches/Google/Chrome")
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Google/Chrome/User Data"
        return root, root
    root = home / ".config/google-chrome"
    return root, home / ".cache/google-chrome"


def profile_candidates(profile=None, *, home=None):
    root, _ = chrome_roots(home)
    if profile not in (None, "", "auto"):
        path = Path(profile).expanduser()
        return [path.resolve()] if path.is_absolute() and path.is_dir() else []
    if not root.is_dir():
        return []
    names = []
    local_state = root / "Local State"
    try:
        if local_state.stat().st_size <= 4 * 1024 * 1024:
            value = json.loads(local_state.read_text(encoding="utf-8"))
            info = value.get("profile") if isinstance(value, dict) else None
            if isinstance(info, dict):
                names.append(info.get("last_used"))
                active = info.get("last_active_profiles")
                if isinstance(active, list):
                    names.extend(active[:100])
                cache = info.get("info_cache")
                if isinstance(cache, dict):
                    names.extend(list(cache)[:100])
    except (OSError, ValueError, UnicodeError):
        pass
    names.append("Default")
    names.extend(path.name for path in sorted(root.glob("Profile *"))[:100])
    result = []
    for name in names:
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            continue
        path = root / name
        if path.is_dir() and path.resolve().parent == root.resolve() and path.resolve() not in result:
            result.append(path.resolve())
    return result


def cache_path(profile, config, *, home=None):
    configured = config.get("browser_cache_dir")
    if configured and config.get("browser_profile") not in (None, "", "auto"):
        return str(Path(configured).expanduser().resolve())
    root, cache = chrome_roots(home)
    if profile.parent == root.resolve():
        return str((cache / profile.name / "Cache/Cache_Data").resolve())
    return None


def resolve_config(config, *, cookie_reader=None, home=None):
    effective = dict(config)
    effective.setdefault("auto_browser_recovery", False)
    if effective.get("browser") != "chrome":
        return effective, {"status": "disabled", "message": "未配置 Chrome 会话读取。"}
    automatic = config.get("browser_profile_auto", config.get("browser_profile") in (None, "", "auto"))
    effective["browser_profile_auto"] = automatic
    profiles = profile_candidates(None if automatic else config.get("browser_profile"), home=home)
    if not profiles:
        return effective, {"status": "profile_unavailable", "session_present": False,
                           "server_validated": False,
                           "message": "找不到 Chrome 用户资料。请手动在 Chrome 打开 https://www.douyin.com/ 并登录，再重试；自定义用户资料请配置 browser_profile。"}
    if cookie_reader is None:
        from douyin_access import browser_cookies
        cookie_reader = browser_cookies
    observations = []
    for profile in profiles:
        try:
            jar = cookie_reader("chrome", str(profile))
            present = any(cookie.domain.lstrip(".").endswith(".douyin.com") or
                          cookie.domain.lstrip(".") == "douyin.com"
                          for cookie in jar if cookie.name in AUTH_COOKIES and cookie.value
                          and not cookie.is_expired())
            observations.append((profile, "available" if present else "login_required", None))
            if present:
                break
        except Exception as exc:
            # Reader errors can contain credentials; never include their text.
            observations.append((profile, "read_error", type(exc).__name__))
    selected = next((item for item in observations if item[1] == "available"), None)
    selected = selected or next((item for item in observations if item[1] == "login_required"), observations[0])
    profile, status, error_type = selected
    cache_config = dict(config, browser_profile=None) if automatic else config
    effective.update(browser_profile=str(profile), browser_cache_dir=cache_path(profile, cache_config, home=home))
    messages = {
        "available": "已读取 Chrome 中现有的抖音会话，将直接复用；服务器有效性由后续请求确认。",
        "login_required": "未读取到可用的抖音登录会话。请手动在 Chrome 的对应用户资料中打开 https://www.douyin.com/ 并登录，再重试；无需保持作品页面打开。",
        "read_error": "无法只读读取 Chrome 的抖音会话。若尚未登录，请先手动登录；若已经登录，请检查浏览器用户资料路径和系统读取授权。未据此判定账号退出登录。",
    }
    result = {"status": status, "profile": str(profile), "session_present": status == "available",
              "server_validated": False, "message": messages[status]}
    if error_type:
        result["error_type"] = error_type
    return effective, result


def require_session(session):
    if session["status"] not in ("available", "disabled"):
        raise RuntimeError("CHROME_SESSION_" + session["status"].upper() + ": " + session["message"])
