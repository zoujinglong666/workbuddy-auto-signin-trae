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
  • 脚本不打印 token / api_key，不上传任何东西。
  • 自动续期：剩余有效期不足 1 小时（或已过期）时，自动用 refreshToken 调
    account.minimax.cn/oauth2/token 换新 token，并把轮换后的新 refreshToken
    原子写回凭据文件（generation+1），与客户端共用同一条 token 链。
    客户端几天没开也能正常签到；refreshToken 彻底失效才需要重新登录客户端。
  • 设 MINIMAX_NO_REFRESH=1 可禁用自动续期（保守模式）。
────────────────────────────────────────────────────────────
"""
import glob
import json
import os
import sys
import time
import urllib.parse
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
    """返回 [(来源路径, 记录key, 记录dict)]；记录须含 accessToken。"""
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
        for k, rec in (data.get("records") or {}).items():
            if isinstance(rec, dict) and rec.get("accessToken"):
                out.append((path, k, rec))
    return out


def _pick_record():
    """取有效期最长的记录；无则 None。"""
    records = _load_auth_records()
    if not records:
        return None, None, None
    records.sort(key=lambda x: int(x[2].get("expiresAtMs") or 0), reverse=True)
    return records[0]


def _mask(v, keep=8):
    v = str(v or "")
    if len(v) <= keep * 2:
        return "*" * len(v)
    return v[:keep] + "..." + v[-keep:]


# ══════════════════ 自动续期（OAuth2 refresh） ══════════════════
# 端点逆向自桌面端 3.1.6：POST https://account.minimax.cn/oauth2/token
# 表单编码 grant_type=refresh_token & refresh_token & client_id；
# 成功返回轮换后的新 refreshToken，必须写回凭据文件延续 token 链，
# 否则客户端持有的旧 refreshToken 失效，需要重新登录。
EP_OAUTH_TOKEN = "https://account.minimax.cn/oauth2/token"
REFRESH_AHEAD_MS = 60 * 60 * 1000   # 剩余不足 1 小时（或已过期）时提前续期


def _write_back(path, record_key, updates):
    """把新凭据合并进磁盘上的 auth.json（先重读盘，避免覆盖客户端并发修改），原子替换。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        sys.stderr.write("重读凭据文件失败: %s\n" % exc)
        return False
    recs = data.get("records") or {}
    target = recs.get(record_key)
    if not isinstance(target, dict):
        sys.stderr.write("凭据记录 %s 已不存在，放弃写回\n" % record_key[:16])
        return False
    target.update(updates)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        sys.stderr.write("写回凭据文件失败: %s\n" % exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _do_refresh(path, record_key, rec):
    """执行一次 OAuth2 刷新；成功返回 (True, 新token, None)，失败返回 (False, None, 原因)。"""
    refresh_token = rec.get("refreshToken") or ""
    if not refresh_token:
        return False, None, "凭据里没有 refreshToken"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": rec.get("clientId") or "mcode-public",
    })
    req = urllib.request.Request(EP_OAUTH_TOKEN, data=body.encode("utf-8"), method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://agent.minimax.cn",
        "User-Agent": USER_AGENT,
    })
    last_err = ""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                js = _try_json(resp.read().decode("utf-8", "replace")) or {}
            access = js.get("access_token") or ""
            new_refresh = js.get("refresh_token") or refresh_token
            expires_in = int(js.get("expires_in") or 0)
            if not access:
                return False, None, "刷新响应缺少 access_token"
            updates = {
                "accessToken": access,
                "refreshToken": new_refresh,
                "tokenType": js.get("token_type") or rec.get("tokenType") or "Bearer",
                "expiresAtMs": int(time.time() * 1000) + expires_in * 1000,
                "generation": int(rec.get("generation") or 0) + 1,
            }
            rec.update(updates)
            if _write_back(path, record_key, updates):
                return True, access, None
            # 写回失败：token 链会断，但仍可把新 token 用于本次运行
            sys.stderr.write("警告：新凭据未能写回凭据文件，客户端下次刷新可能失败\n")
            return True, access, None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:120]
            except Exception:
                pass
            if e.code in (400, 401, 403):
                return False, None, "refreshToken 已失效（http=%s %s），请打开一次 MiniMax Code 客户端重新登录" % (e.code, detail)
            last_err = "刷新失败 http=%s %s" % (e.code, detail)
            break   # 4xx/5xx 明确拒绝，重试无意义
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt == 0:
                time.sleep(5)
    return False, None, last_err


def _ensure_fresh_token(path, record_key, rec):
    """确保 token 可用：剩余有效期不足时自动续期。
    返回 (token or None, 续期与否, 说明 or None)。"""
    exp = int(rec.get("expiresAtMs") or 0)
    now_ms = int(time.time() * 1000)
    no_refresh = os.environ.get("MINIMAX_NO_REFRESH", "").strip().lower() in ("1", "true", "yes", "on")

    if exp and exp - now_ms < REFRESH_AHEAD_MS:
        if no_refresh:
            if exp > now_ms:
                return rec["accessToken"], False, "已禁用自动续期，当前 token 仍有效"
            return None, False, "登录态已过期且禁用自动续期，请打开一次 MiniMax Code 客户端"
        ok, token, err = _do_refresh(path, record_key, rec)
        if ok:
            return token, True, None
        if exp > now_ms:
            return rec["accessToken"], False, "自动续期失败但当前 token 仍有效：%s" % err
        return None, False, err or "自动续期失败"
    return rec["accessToken"], False, None


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
    path, record_key, rec = _pick_record()
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
        "result": "OK",
        "needs_attention": False,
        "online_checked": False,
        "auth_file": path,
        "region": rec.get("region"),
        "audience": rec.get("audience"),
        "access_token": _mask(token),
        "has_refresh_token": bool(rec.get("refreshToken")),
        "auto_refresh": bool(rec.get("refreshToken")),
        "token_expires_at": time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(exp_ms / 1000)) if exp_ms else None,
        "token_valid_seconds": left_s,
    }
    if expired:
        detail["report"] = ("登录态已过期（%s），签到时会自动续期" % detail["token_expires_at"]
                            if rec.get("refreshToken") else
                            "登录态已过期（%s）且无 refreshToken，请打开一次 MiniMax Code 客户端重新登录"
                            % detail["token_expires_at"])
        detail["needs_attention"] = not rec.get("refreshToken")
    else:
        hours = left_s // 3600
        detail["report"] = "登录态可读，有效期至 %s（剩余 %d 小时），到期前自动续期" % (
            detail["token_expires_at"], hours)
    return detail, (1 if detail["needs_attention"] else 0)


# ══════════════════ status ══════════════════
def cmd_status():
    path, record_key, rec = _pick_record()
    if not rec:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "没找到登录凭据，请先登录 MiniMax Code 桌面端"}, 1

    token, renewed, refresh_err = _ensure_fresh_token(path, record_key, rec)
    if not token:
        return {"result": "AUTH_ERROR", "needs_attention": True, "online_checked": False,
                "report": refresh_err or "登录态不可用"}, 1

    ok, payload = get_status(token)
    if not ok:
        expired = "401" in str(payload) or "403" in str(payload)
        return {"result": "AUTH_ERROR" if expired else "NETWORK",
                "needs_attention": True, "online_checked": True,
                "renewed": renewed,
                "report": ("登录态已失效且续期未成功，请打开一次 MiniMax Code 客户端重新登录" if expired
                           else "签到状态查询失败：%s" % payload)}, 1

    day = today_day(payload)
    checked = bool(day) and day.get("status") == STATUS_CHECKED
    credits, err = get_credit(token)

    rows = {"today_checked_in": checked, "today_points": (day or {}).get("points"),
            "credits_remaining": credits, "credit_error": err, "renewed": renewed}
    report = "今日%s（可领 %s 积分）" % (
        "已签到" if checked else "未签到", (day or {}).get("points", "-"))
    if credits is not None:
        report += "，积分余额 %s" % int(credits)
    return {"result": "OK", "needs_attention": False, "online_checked": True,
            **rows, "report": report}, 0


# ══════════════════ auto / silent ══════════════════
def run_checkin():
    path, record_key, rec = _pick_record()
    if not rec:
        return {"result": "NO_AUTH", "needs_attention": True, "online_checked": False,
                "report": "没找到登录凭据，请先登录 MiniMax Code 桌面端"}, 1

    token, renewed, refresh_err = _ensure_fresh_token(path, record_key, rec)
    if not token:
        return {"result": "AUTH_ERROR", "needs_attention": True, "online_checked": False,
                "report": refresh_err or "登录态不可用"}, 1

    ok, payload = get_status(token)
    if not ok:
        expired = "401" in str(payload)
        return {"result": "AUTH_ERROR" if expired else "NETWORK",
                "needs_attention": expired, "online_checked": True, "renewed": renewed,
                "report": ("登录态已失效且续期未成功，请打开一次 MiniMax Code 客户端重新登录" if expired
                           else "签到状态查询失败：%s" % payload)}, (1 if expired else 0)

    day = today_day(payload)
    if day and day.get("status") == STATUS_CHECKED:
        credits, _ = get_credit(token)
        report = "今日已签到（可领 %s 积分）" % day.get("points", "-")
        if credits is not None:
            report += "，积分余额 %s" % int(credits)
        return {"result": "ALREADY", "needs_attention": False, "online_checked": True,
                "today_checked_in": True, "renewed": renewed, "credits_remaining": credits,
                "report": report}, 0

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
                "renewed": renewed, "credits_remaining": credits, "report": report}, 0

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
