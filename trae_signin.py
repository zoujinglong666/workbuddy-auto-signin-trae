#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae 每日自动领取积分 · trae_signin.py
────────────────────────────────────────────────────────────
本仓库（workbuddy-auto-signin）风格的单文件封装：读本机 Trae 客户端登录态，
每天自动签到领 Credits，输出一行 JSON，静默模式写日志。

核心接口与设备证明逻辑来自 MIT 项目 L0NE-6/Trae-AutoCheckin
（源码已 vendor 在 trae/ 目录，并把解密改成纯标准库实现，保持零依赖）。

🚀 用法
  python trae_signin.py doctor       # 离线自检：凭据 / 设备号 / 设备密钥，不联网
  python trae_signin.py status       # 只查今日签到状态与积分，不领取
  python trae_signin.py auto         # 查状态 + 未签则领取（幂等）
  python trae_signin.py silent       # 同 auto，结果写 trae_signin.log（配合定时任务）
  python trae_signin.py silent-poll  # 轮询补签：已签则零请求

📦 环境变量（一般不用设）
  TRAE_SIGNIN_LOG          silent 模式日志路径（默认脚本同目录 trae_signin.log）
  TRAE_SIGNIN_LOG_EMPTY=1  空跑（已签过）也写日志
  TRAE_STORAGE_PATH        指定客户端 storage.json
  TRAE_NO_REFRESH=1        禁止自动续期（token 过期时只提示，避免与客户端争抢轮换）
  TRAE_ACCOUNTS / TRAE_REFRESH_TOKEN ...  见 trae/trae_checkin.py 顶部说明

📌 说明
  • 只读本机自己的登录态，不打印 token，不上传任何东西。
  • 签到接口与设备证明均逆向自桌面端，服务端改版即可能失效。
────────────────────────────────────────────────────────────
"""
import contextlib
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(HERE, "trae")
sys.path.insert(0, VENDOR_DIR)

# 本机单账号定时任务不需要 0~20s 随机抖动，避免计划任务空转
os.environ.setdefault("TRAE_JITTER", "0")

try:
    import trae_checkin as core
except ImportError as exc:  # 内嵌目录被删/改动
    print(json.dumps({"result": "ERROR", "needs_attention": True,
                      "report": "缺少 trae/trae_checkin.py：%s" % exc}, ensure_ascii=False))
    sys.exit(1)

LOG_PATH = os.environ.get("TRAE_SIGNIN_LOG") or os.path.join(HERE, "trae_signin.log")
NO_REFRESH = (os.environ.get("TRAE_NO_REFRESH", "").strip().lower()
              in ("1", "true", "yes", "on"))
SOFT_STATUS = ("待重试", "待确认")          # 限流/未确认：交给下一轮，不算失败
HARD_STATUS = ("鉴权失败", "失败", "异常", "无token", "token失效")


# ══════════════════ 通用工具 ══════════════════
def _capture(fn, *args, **kwargs):
    """执行上游函数并吞掉它的终端输出，返回 (结果, 输出文本)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _mask(text, keep=6):
    text = str(text or "")
    if len(text) <= keep * 2:
        return "*" * len(text)
    return text[:keep] + "*" * (len(text) - keep * 2) + text[-keep:]


def _acct_name(acc):
    return (acc.get("_name") or acc.get("name") or acc.get("uid")
            or core._acct_key(acc) or "(未知账号)")


def _load_accounts():
    """读账号：环境变量优先，否则解密本机 Trae 客户端凭据。"""
    accounts, output = _capture(core.load_accounts)
    if accounts:
        _decorate_names(accounts)
        return accounts, output
    return None, output


def _decorate_names(accounts):
    """凭据里没有昵称时用 uid 当名字，读得到 account.username 就换成昵称（只影响日志）。"""
    nickname = ""
    try:
        storage = os.environ.get("TRAE_STORAGE_PATH", "").strip() or core.find_storage_json()
        if storage:
            creds, _ = core.load_from_storage(storage)
            account = creds.get("account")
            if isinstance(account, dict):
                nickname = str(account.get("username") or "").strip()
    except Exception:
        nickname = ""
    if not nickname:
        return accounts
    for acc in accounts:
        current = acc.get("_name") or acc.get("name") or ""
        if not current or current == str(acc.get("uid") or ""):
            acc["_name"] = nickname
            acc["name"] = nickname
    return accounts


def _token_state(acc, cache):
    """确保有可用的 accessToken，返回 (ok, msg)。默认不主动续期以外的操作。"""
    token = acc.get("accessToken") or ""
    exp = core._jwt_exp(token) if token else 0
    if token and (not exp or exp > time.time() + 60):
        return True, "token 有效"
    if not acc.get("refreshToken"):
        return False, "无 refreshToken，请在 Trae 客户端重新登录"
    if NO_REFRESH and token:
        return False, "accessToken 已过期且已禁用自动续期，请打开一次 Trae 客户端"
    ok, msg = core.ensure_token(acc, cache)
    return ok, msg


# ══════════════════ doctor：离线自检 ══════════════════
def cmd_doctor():
    storage = os.environ.get("TRAE_STORAGE_PATH", "").strip() or core.find_storage_json()
    detail = {"version": "1.0.0", "platform": sys.platform,
              "online_checked": False, "needs_attention": False}

    if not storage or not os.path.isfile(storage):
        detail.update({"result": "NO_AUTH", "needs_attention": True,
                       "report": "没找到 Trae 客户端 storage.json，请先安装并登录 Trae CN 桌面端"})
        return detail, 1

    try:
        creds, real_did = core.load_from_storage(storage)
    except Exception as exc:
        detail.update({"result": "AUTH_ERROR", "needs_attention": True,
                       "report": "凭据解析失败（%s）：%s" % (type(exc).__name__, exc),
                       "auth_dir": os.path.dirname(storage)})
        return detail, 1

    access = creds.get("token") or creds.get("accessToken") or ""
    refresh = creds.get("refreshToken") or ""
    try:
        keys = core.local_device_keys()
    except Exception:
        keys = None

    exp = core._jwt_exp(access)
    left = max(0, int(exp - time.time())) if exp else 0
    detail.update({
        "result": "OK" if refresh else "NO_SESSION",
        "auth_dir": os.path.dirname(storage),
        "uid": str(creds.get("userId") or ""),
        "refresh_token": _mask(refresh),
        "access_token_valid_seconds": left,
        "real_device_id": bool(real_did),
        "device_keys": bool(keys),
        "needs_attention": not refresh,
    })
    if not refresh:
        detail["report"] = "storage.json 里没有 refreshToken，请先在客户端登录一次"
        return detail, 1
    detail["report"] = "登录态可读（UID %s）%s" % (
        detail["uid"] or "-",
        "；accessToken 有效，剩余 %d 小时" % (left // 3600) if left > 0
        else "；accessToken 已过期，签到时将自动续期")
    return detail, 0


# ══════════════════ status：只读查询 ══════════════════
def cmd_status():
    accounts, output = _load_accounts()
    if not accounts:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "未找到可用账号：" + (output.strip().splitlines() or ["未知原因"])[-1]}, 1

    cache = core.load_cache()
    rows, reports = [], []
    for acc in core.apply_cache(accounts, cache):
        name = _acct_name(acc)
        ok, msg = _token_state(acc, cache)
        if not ok:
            rows.append({"name": name, "status": "token失效", "detail": msg})
            reports.append("%s：%s" % (name, msg))
            continue
        acc["deviceId"] = core.device_id_for(acc, acc.get("deviceId", ""))
        checked, credits, enable, code, raw = core.status_of(acc["accessToken"], acc["deviceId"])
        rows.append({"name": name, "uid": acc.get("uid", ""), "checked_in": checked,
                     "credits": credits, "enable": enable, "code": code})
        if checked is None:
            reports.append("%s：状态查询失败（code=%s）" % (name, code))
        else:
            reports.append("%s：%s，积分 %s" % (name, "今日已签到" if checked else "今日未签到",
                                            credits if credits is not None else "-"))

    hard = any(r.get("checked_in") is None for r in rows)
    return {"result": "OK", "needs_attention": hard, "online_checked": True,
            "accounts": rows, "report": "；".join(reports)}, (1 if hard else 0)


# ══════════════════ auto / silent：签到领取 ══════════════════
def _run_checkin():
    accounts, output = _load_accounts()
    if not accounts:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "未找到可用账号：" + (output.strip().splitlines() or ["未知原因"])[-1]}, 1

    cache = core.load_cache()
    state = core.load_state()
    rows = []
    for acc in core.apply_cache(accounts, cache):
        name = _acct_name(acc)
        key = core._acct_key(acc)

        done = (state.get("done") or {}).get(key)
        if done:
            rows.append({"name": name, "status": "已签到", "credits": done.get("credits"),
                         "detail": "今日已完成（%s），跳过请求" % done.get("at", "-")})
            continue
        left = core.cooldown_left(key, state)
        if left > 0:
            rows.append({"name": name, "status": "待重试",
                         "detail": "限流冷却中，还剩 %d 分钟" % ((left + 59) // 60)})
            continue

        before = acc.get("accessToken") or ""
        if not before or (core._jwt_exp(before) and core._jwt_exp(before) <= time.time() + 60):
            ok, msg = _token_state(acc, cache)
            if not ok:
                rows.append({"name": name, "status": "鉴权失败", "detail": msg})
                continue

        row, log = _capture(core.checkin_account, acc, cache)
        row["name"] = row.get("name") or name
        rows.append(row)
        if row.get("status") in ("签到成功", "已签到"):
            core.mark_done(key, {"status": row["status"], "credits": row.get("credits"),
                                 "at": core.bj()})

    claimed = [r for r in rows if r.get("status") == "签到成功"]
    already = [r for r in rows if r.get("status") == "已签到"]
    soft = [r for r in rows if r.get("status") in SOFT_STATUS]
    hard = [r for r in rows if r.get("status") in HARD_STATUS]

    if claimed:
        result = "SUCCESS"
    elif already and not hard and not soft:
        result = "ALREADY"
    elif soft and not hard:
        result = "PENDING"
    else:
        result = "ERROR"

    lines = []
    for r in rows:
        lines.append("%s：%s（%s）" % (r.get("name"), r.get("status"), r.get("detail") or "-"))
    return {"result": result, "needs_attention": bool(hard), "online_checked": True,
            "accounts": rows, "report": "；".join(lines)}, (1 if hard else 0)


def _write_log(payload, force=False):
    if not force and payload.get("result") in ("ALREADY",):
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, json.dumps(payload, ensure_ascii=False)))
    except Exception as exc:
        sys.stderr.write("写日志失败: %s\n" % exc)


def _log_enabled_for_empty():
    return os.environ.get("TRAE_SIGNIN_LOG_EMPTY", "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = (argv[0] if argv else "auto").strip().lower()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if mode in ("doctor", "check"):
        payload, code = cmd_doctor()
    elif mode in ("status", "query"):
        payload, code = cmd_status()
    elif mode in ("auto", "claim", "run"):
        payload, code = _run_checkin()
    elif mode in ("silent", "silent-poll", "silent-growth"):
        payload, code = _run_checkin()
        payload["mode"] = mode
        _write_log(payload, force=_log_enabled_for_empty())
        return code
    else:
        payload, code = {"result": "ERROR", "needs_attention": True,
                         "report": "未知模式：%s（可用 auto/status/doctor/silent/silent-poll）" % mode}, 1

    payload.setdefault("mode", mode)
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
