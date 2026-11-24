#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMax Code 每日自动签到 · minmax_signin.py
────────────────────────────────────────────────────────────
读取本机 MiniMax Code 客户端的登录态（~/.minimax/auth/**/auth.json，明文），
每天自动签到领积分，输出一行 JSON，静默模式写日志。

🚀 用法
  python minmax_signin.py doctor       # 离线自检：凭据存在性与有效期，不联网
  python minmax_signin.py status       # 只查今日签到状态与积分余额，不领取
  python minmax_signin.py auto         # 查状态 + 未签则领取（幂等，已签不会重复领）
  python minmax_signin.py silent       # 同 auto，结果写 minmax_signin.log
  python minmax_signin.py silent-poll  # 轮询补签：已签则零请求

📦 环境变量（一般不用设）
  MINIMAX_AUTH_FILE        指定凭据文件（自动探测失败时）
  MINIMAX_SIGNIN_LOG       silent 模式日志路径（默认脚本同目录 minmax_signin.log）
  MINIMAX_SIGNIN_LOG_EMPTY=1  空跑（已签过）也写日志
  MINIMAX_TIME_BUDGET      单次运行网络时间预算秒数（默认 180，上限 300）

📌 说明
  • 签到接口与请求参数逆向自 MiniMax Code 桌面端 3.1.6，仅供个人自动化使用。
  • 脚本不打印 token / api_key，不上传任何东西；登录与续期仍由客户端负责。
  • 登录态过期时请打开一次 MiniMax Code 客户端，它会自动刷新凭据文件。
────────────────────────────────────────────────────────────
"""
import glob
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.environ.get("MINIMAX_SIGNIN_LOG") or os.path.join(HERE, "minmax_signin.log")
LOG_EMPTY = os.environ.get("MINIMAX_SIGNIN_LOG_EMPTY", "").strip().lower() in ("1", "true", "yes", "on")

HOST_UG = "https://agent.minimax.cn"
HOST_ACCOUNT = "https://www.minimaxi.com"
EP_STATUS = HOST_UG + "/minimax-cloud/api/v1/signin/status"
EP_CLAIM = HOST_UG + "/minimax-cloud/api/v1/signin/claim"
EP_CREDIT = HOST_ACCOUNT + "/backend/account/token_plan_credit"

USER_AGENT = ("MiniMaxAgent Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) MiniMax/3.1.6 Chrome/148.0.7778.280 Safari/537.36")

QUERY_BASE = ("device_platform=web&biz_id=3&app_id=3001&version_code=22201"
              "&unix=%(unix)d&timezone_offset=28800&is_desktop=1&desktop_version=3.1.6"
              "&sys_language=zh&lang=zh&client=desktop")

REQUEST_TIMEOUT = 20
STATUS_CHECKED = 3          # days[].status: 3 = 已签到
STATUS_UNSIGNED = 1         # 1 = 未签到
CLAIM_DUP = 2               # claim_result: 2 = 今日已签（重复领取去重）


def _budget():
    try:
        v = int(os.environ.get("MINIMAX_TIME_BUDGET", "180"))
    except ValueError:
        v = 180
    return max(30, min(v, 300))


BUDGET = _budget()
_DEADLINE = time.time() + BUDGET


# ══════════════════ 凭据 ══════════════════
def _load_auth_records():
    """返回 [(来源路径, 记录dict)]；记录须含 accessToken。"""
    out = []
    candidates = []
    env = os.environ.get("MINIMAX_AUTH_FILE", "").strip()
    if env:
        candidates.append(env)
    else:
        root = os.path.join(os.path.expanduser("~"), ".minimax", "auth")
        candidates += glob.glob(os.path.join(root, "*", "*", "*", "auth.json"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        for rec in (data.get("records") or {}).values():
            if isinstance(rec, dict) and rec.get("accessToken"):
                out.append((path, rec))
    return out


def _pick_record():
    """取有效期最长的记录；无则 None。"""
    records = _load_auth_records()
    if not records:
        return None, None
    records.sort(key=lambda x: int(x[1].get("expiresAtMs") or 0), reverse=True)
    return records[0]


def _mask(v, keep=8):
    v = str(v or "")
    if len(v) <= keep * 2:
        return "*" * len(v)
    return v[:keep] + "..." + v[-keep:]


# ══════════════════ HTTP ══════════════════
def _query():
    return QUERY_BASE % {"unix": int(time.time() * 1000)}


def _headers(token):
    return {
        "User-Agent": USER_AGENT,
        "Origin": "https://agent.minimax.cn",
        "Referer": "https://agent.minimax.cn/",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
    }


def _request(method, url, token, body=None):
    """返回 (http, json_dict|None, raw_text)；网络异常时 http=-1。不打印 token。"""
    data = body.encode("utf-8") if isinstance(body, str) else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _try_json(raw), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _try_json(raw), raw
    except Exception as e:
        return -1, None, "%s: %s" % (type(e).__name__, e)


def _try_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _retry_request(method, url, token, body=None, retries=3):
    """只读查询用：网络错误/5xx 退避重试，受时间预算约束。"""
    attempt = 0
    last = (-1, None, "no attempt")
    while attempt <= retries:
        left = int(_DEADLINE - time.time())
        if left <= 0:
            return last[0], last[1], (last[2] or "") + " [时间预算耗尽]"
        http, js, raw = _request(method, url, token, body)
        if http == 200:
            return http, js, raw
        last = (http, js, raw)
        retriable = (http < 0) or (http >= 500) or (http == 429)
        if not retriable or attempt == retries:
            break
        wait = (5, 15, 30)[min(attempt, 2)]
        if wait > left:
            break
        time.sleep(wait)
        attempt += 1
    return last


# ══════════════════ 业务封装 ══════════════════
def get_status(token):
    """返回 (ok, payload)。payload 为 data 字段或 None。"""
    http, js, raw = _retry_request("GET", EP_STATUS + "?" + _query(), token)
    if http != 200:
        return False, "status http=%s %s" % (http, (raw or "")[:120])
    base = ((js or {}).get("base_resp") or {})
    if base.get("status_code") != 0:
        return False, "status code=%s %s" % (base.get("status_code"), base.get("status_msg"))
    return True, (js or {}).get("data") or {}


def get_credit(token):
    """返回 (余额 or None, 错误 or None)。"""
    http, js, raw = _retry_request(
        "GET", EP_CREDIT + "?business=Agent&device_platform=desktop", token)
    if http != 200:
        return None, "credit http=%s" % http
    base = ((js or {}).get("base_resp") or {})
    if base.get("status_code") != 0:
        return None, "credit code=%s" % base.get("status_code")
    data = js.get("data") or js
    try:
        return float(data.get("remaining_credits")), None
    except (TypeError, ValueError):
        return None, "credit 解析失败"


def post_claim(token):
    """领取一次。返回 (http, payload|None, raw)。"""
    return _request("POST", EP_CLAIM + "?" + _query(), token, "{}")


def today_day(payload):
    days = (payload or {}).get("days") or []
    for d in days:
        if d.get("is_today"):
            return d
    return None


# ══════════════════ 输出 ══════════════════
def _write_log(payload):
    if payload.get("result") == "ALREADY" and not LOG_EMPTY:
        return
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, json.dumps(payload, ensure_ascii=False)))
    except Exception as exc:
        sys.stderr.write("写日志失败: %s\n" % exc)


# ══════════════════ doctor ══════════════════
def cmd_doctor():
    path, rec = _pick_record()
    if not rec:
        return {
            "result": "NO_AUTH", "needs_attention": True, "online_checked": False,
            "report": "没找到 ~/.minimax/auth/**/auth.json 登录凭据，请先安装并登录 MiniMax Code 桌面端",
        }, 1

    token = rec["accessToken"]
    exp_ms = int(rec.get("expiresAtMs") or 0)
    left_s = max(0, (exp_ms - int(time.time() * 1000)) // 1000) if exp_ms else 0
    expired = bool(exp_ms) and exp_ms < int(time.time() * 1000)

    detail = {
        "result": "OK" if not expired else "TOKEN_EXPIRED",
        "needs_attention": expired,
        "online_checked": False,
        "auth_file": path,
        "region": rec.get("region"),
        "audience": rec.get("audience"),
        "access_token": _mask(token),
        "has_refresh_token": bool(rec.get("refreshToken")),
        "token_expires_at": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(exp_ms / 1000)) if exp_ms else None,
        "token_valid_seconds": left_s,
    }
    if expired:
        detail["report"] = ("登录态已过期（%s）。请打开一次 MiniMax Code 客户端，"
                            "它会自动刷新凭据文件，然后重跑本脚本。" % detail["token_expires_at"])
    else:
        hours = left_s // 3600
        detail["report"] = "登录态可读，有效期至 %s（剩余 %d 小时）" % (
            detail["token_expires_at"], hours)
    return detail, (1 if expired else 0)


# ══════════════════ status ══════════════════
def cmd_status():
    path, rec = _pick_record()
    if not rec:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "没找到登录凭据，请先登录 MiniMax Code 桌面端"}, 1
    token = rec["accessToken"]

    ok, payload = get_status(token)
    if not ok:
        expired = "401" in str(payload) or "403" in str(payload)
        return {"result": "AUTH_ERROR" if expired else "NETWORK",
                "needs_attention": True, "online_checked": True,
                "report": ("登录态已失效，请打开一次 MiniMax Code 客户端重新刷新" if expired
                           else "签到状态查询失败：%s" % payload)}, 1

    day = today_day(payload)
    checked = bool(day) and day.get("status") == STATUS_CHECKED
    credits, err = get_credit(token)

    rows = {"today_checked_in": checked, "today_points": (day or {}).get("points"),
            "credits_remaining": credits, "credit_error": err}
    report = "今日%s（可领 %s 积分）" % (
        "已签到" if checked else "未签到", (day or {}).get("points", "-"))
    if credits is not None:
        report += "，积分余额 %s" % int(credits)
    return {"result": "OK", "needs_attention": False, "online_checked": True,
            **rows, "report": report}, 0


# ══════════════════ auto / silent ══════════════════
def run_checkin():
    path, rec = _pick_record()
    if not rec:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "没找到登录凭据，请先登录 MiniMax Code 桌面端"}, 1
    token = rec["accessToken"]

    exp_ms = int(rec.get("expiresAtMs") or 0)
    if exp_ms and exp_ms < int(time.time() * 1000):
        return {"result": "TOKEN_EXPIRED", "needs_attention": True, "online_checked": False,
                "report": "登录态已过期，请打开一次 MiniMax Code 客户端后重试"}, 1

    ok, payload = get_status(token)
    if not ok:
        expired = "401" in str(payload)
        return {"result": "AUTH_ERROR" if expired else "NETWORK",
                "needs_attention": expired, "online_checked": True,
                "report": ("登录态已失效，请打开一次 MiniMax Code 客户端重新刷新" if expired
                           else "签到状态查询失败：%s" % payload)}, (1 if expired else 0)

    day = today_day(payload)
    if day and day.get("status") == STATUS_CHECKED:
        credits, _ = get_credit(token)
        report = "今日已签到（可领 %s 积分）" % day.get("points", "-")
        if credits is not None:
            report += "，积分余额 %s" % int(credits)
        return {"result": "ALREADY", "needs_attention": False, "online_checked": True,
                "today_checked_in": True, "credits_remaining": credits, "report": report}, 0

    http, js, raw = post_claim(token)
    if http != 200:
        return {"result": "ERROR", "needs_attention": True, "online_checked": True,
                "report": "领取请求失败 http=%s %s" % (http, (raw or "")[:120])}, 1
    base = ((js or {}).get("base_resp") or {})
    if base.get("status_code") != 0:
        return {"result": "ERROR", "needs_attention": True, "online_checked": True,
                "report": "领取失败 code=%s %s" % (base.get("status_code"),
                                                  base.get("status_msg"))}, 1

    data = (js or {}).get("data") or {}
    claim_result = data.get("claim_result")
    day_after = today_day(data.get("panel") or {})
    confirmed = bool(day_after) and day_after.get("status") == STATUS_CHECKED

    if claim_result == CLAIM_DUP or confirmed:
        credits, _ = get_credit(token)
        if claim_result == CLAIM_DUP:
            report = "今日已签到（重复领取已去重）"
        else:
            report = "签到成功，本次 +%s 积分" % data.get("points", "-")
        if credits is not None:
            report += "，积分余额 %s" % int(credits)
        return {"result": "SUCCESS" if confirmed else "ALREADY",
                "needs_attention": False, "online_checked": True,
                "claim_result": claim_result, "points": data.get("points"),
                "credits_remaining": credits, "report": report}, 0

    return {"result": "ERROR", "needs_attention": True, "online_checked": True,
            "report": "领取结果未确认（claim_result=%s，回查未通过）" % claim_result}, 1


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
        payload, code = run_checkin()
    elif mode in ("silent", "silent-poll"):
        payload, code = run_checkin()
        payload["mode"] = mode
        _write_log(payload)
        return code
    else:
        payload, code = {"result": "ERROR", "needs_attention": True,
                         "report": "未知模式：%s（可用 auto/status/doctor/silent/silent-poll）" % mode}, 1

    payload.setdefault("mode", mode)
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
