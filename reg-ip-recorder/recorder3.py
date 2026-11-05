#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
注册 IP 记录器（外挂式，不改 New API 源码）

做什么：
  从 BunnyCDN 的访问日志里捞「注册成功」事件（POST /api/user/register 且
  状态码 200），按时间把事件对到 New API 刚建好的用户行上，把注册 IP
  合并写入该用户 users.setting 的 hajimi_reg_ip 键：
      {"ip": "120.231.203.0", "net": "/24", "at": 1762345563}
  用途：邀请总览展示注册 IP + 薅羊毛同网段识别（见 skin v2.9.17 规格）。

数据源（Bunny 官方文档核实，2026-11-06，见同目录 DEPLOY.md「事实来源」）：
  Logging API v2：GET https://logging.bunnycdn.com/v2/pullzones/{id}/logs
  账号 API Key 走 AccessKey 请求头；日志只保留 3 天；IP 默认匿名化
  （IPv4 抹末段 = /24，IPv6 抹到 /64），所以存下来的就是网段精度。

匹配口径：
  时间差 diff = 用户 created_at − 事件时间（秒）。2026-11-06 生产实测：
  注册要走人机验证＋邮箱验证码，事件常比建号早几分钟（实测 Δ 达 +100s
  与 +670s），故窗口放宽为 diff ∈ [-60, +900]（-60 容时钟偏差与建号后
  的补发请求，+900 容用户慢慢走完注册流程）。候选对按 |diff| 从小到大贪心一对一（一事件一人、
  一人一事件）。若某用户的最优 |diff| 被 ≥2 个事件并列取得 → 歧义，
  该用户整单跳过、记进报告待人工看，绝不猜。
  已禁用/已删除用户照常记录（薅羊毛的常被封，IP 更要留底）——本脚本
  不按 status/deleted_at 过滤。

幂等与状态：
  状态文件（默认脚本同目录 state.json）记两样：covered_until（已成功
  拉取覆盖到的时间点）与 seen（已处理事件键集合，键 = 时间戳|IP|路径，
  只留最近 4 天）。已写过 hajimi_reg_ip 的用户不再是候选，DB 层是最终
  保险。状态只在 --apply 成功后落盘；--report 纯只读、不推进状态，
  方便站长先看清单再决定。歧义事件与「setting 损坏被跳过」用户的事件
  不标 seen，下一轮还会重试/再报（自愈 + 人工队列）。

写入安全（与返佣结算 settle.py 同一套规矩）：
  - 只合并 hajimi_reg_ip 一个键，setting 里原有键（含 hajimi_aff）一律保留
  - 某用户现有 setting 不是合法 JSON 对象 → 跳过该用户并告警，绝不覆盖
  - PG 走 SQL 层 jsonb 合并；写完在同一事务内回读逐条校验，不符整单回滚
  - 单实例文件锁；失败即停、不重试（退出码 1=失败已回滚，2=参数/配置
    问题，3=锁被占用）

数据库（双驱动，与 settle.py 相同约定）：
  --db 传 postgres:// DSN（生产）或 SQLite 文件路径（实验室）；
  省略则读环境变量 RECORDER_DSN。生产 DSN 由 run.sh 经 dsn.sh 自动解析
  （同返佣引擎：取 New API 现行 SQL_DSN 并把主机换成 postgres 容器 IP）。

Bunny 密钥：从本地配置文件读（默认 /opt/reg-ip-recorder/config，
600 权限，内含 BUNNY_API_KEY= 与可选 BUNNY_PULLZONE_ID=）；
测试可用环境变量 BUNNY_API_KEY / BUNNY_PULLZONE_ID 覆盖。

用法：
  python3 recorder.py                       # --report 空跑（默认）：只出清单
  python3 recorder.py --apply               # 真写（拉最近 2 个 UTC 日）
  python3 recorder.py --apply --backfill 3  # 首次：拉最近 3 个 UTC 日
  python3 recorder.py --report --backfill 2 # 指定天数的空跑清单
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import fcntl  # Linux 单实例文件锁；生产为 Linux，必需
except ImportError:  # pragma: no cover
    fcntl = None

# ---- Bunny Logging API v2（官方文档核实）----
BUNNY_BASE = os.environ.get("RECORDER_BUNNY_BASE", "https://logging.bunnycdn.com")
DEFAULT_PULLZONE_ID = 6896217          # hajimiapi
REGISTER_PATH = "/api/user/register"
RETENTION_DAYS = 3                     # Bunny 可搜索日志保留期（官方文档）
PAGE_LIMIT = 10000                     # v2 单页上限（官方文档）

# ---- 匹配口径 ----
DIFF_MIN = -60                         # diff = created_at − 事件时间（秒）
DIFF_MAX = 900
SETTING_KEY = "hajimi_reg_ip"

# ---- 状态文件 ----
SEEN_KEEP_DAYS = 4                     # seen 集合只保留最近 N 天的事件键

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3

log = logging.getLogger("reg-ip-recorder")


# ================= 工具 =================

def mask_target(target: str) -> str:
    """日志里展示连接目标：DSN 打码密码部分。（同 settle.py）"""
    if "://" in target:
        scheme, rest = target.split("://", 1)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return target
    return target


def iso_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_ts(s: str) -> float:
    """解析 Bunny v2 的 ISO 8601 UTC 时间戳（带毫秒、Z 结尾）为 unix 秒（float）。"""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def net_of(ip: str) -> str:
    """匿名化宽度（官方文档）：IPv4 抹末段 = /24；IPv6 抹到 /64。"""
    return "/64" if ":" in ip else "/24"


class Locked(Exception):
    pass


def acquire_lock(lock_path: str):
    """非阻塞独占文件锁。（同 settle.py）"""
    if fcntl is None:
        raise RuntimeError("当前平台无 fcntl，无法使用单实例锁（生产应为 Linux）")
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise Locked(lock_path)
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


class ConfigError(Exception):
    """配置/凭据问题 → 退出码 2。"""


class FetchError(Exception):
    """拉日志失败（非单日问题）→ 退出码 1，本轮即停。"""


# ================= 配置 =================

def load_bunny_config(path: str):
    """读本地配置文件（KEY=VALUE 行，# 注释）。返回 dict。
    环境变量 BUNNY_API_KEY / BUNNY_PULLZONE_ID 优先覆盖文件值（方便测试）。"""
    cfg = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    if os.environ.get("BUNNY_API_KEY"):
        cfg["BUNNY_API_KEY"] = os.environ["BUNNY_API_KEY"]
    if os.environ.get("BUNNY_PULLZONE_ID"):
        cfg["BUNNY_PULLZONE_ID"] = os.environ["BUNNY_PULLZONE_ID"]
    return cfg


# ================= Bunny 日志拉取 =================

def fetch_day(api_key: str, pullzone_id: int, day: datetime):
    """拉一个 UTC 日的注册成功事件（已做服务端过滤 + 客户端复核）。
    返回 (events, skipped_reason)：
      events: [{ts_ms, ts, ip, path, key}]，按时间升序；
      skipped_reason: 该日被跳过时为原因字符串（保留期外等），否则 None。
    凭据/日志未启用等硬错误抛 ConfigError / FetchError。"""
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    params = {
        "from": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "200",
        "urlContains": REGISTER_PATH,
        "limit": str(PAGE_LIMIT),
        "order": "asc",
    }
    events = []
    offset = 0
    while True:
        params["offset"] = str(offset)
        url = (
            f"{BUNNY_BASE}/v2/pullzones/{pullzone_id}/logs?"
            + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(
            url,
            headers={"AccessKey": api_key, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                body = json.loads(e.read().decode("utf-8"))
                detail = (body.get("error") or {}).get("message", "") or str(body)
            except Exception:
                detail = e.reason or ""
            if e.code in (401, 403):
                raise ConfigError(
                    f"Bunny 凭据被拒（HTTP {e.code}）：{detail}。"
                    "请核对 config 里的 BUNNY_API_KEY 是否为账号 API Key"
                )
            if e.code == 404:
                raise FetchError(
                    "Bunny 返回 404 logging_disabled：该 Pull Zone 未开启日志，"
                    "先去 Bunny 后台开启 Logging 再跑"
                )
            if e.code == 400:
                # 单日问题（多为超出 3 天保留期）：跳过该日、由调用方记进报告
                return [], f"HTTP 400：{detail}"
            raise FetchError(f"Bunny 拉日志失败（HTTP {e.code}）：{detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"Bunny 拉日志网络错误：{e}")

        for entry in payload.get("data") or []:
            ev = entry_to_event(entry)
            if ev is not None:
                events.append(ev)
        pagination = payload.get("pagination") or {}
        if not pagination.get("hasMore"):
            break
        returned = int(pagination.get("returned") or 0)
        if returned <= 0:
            break  # 防御：声称还有更多却返回 0 条时不死循环
        offset += returned
    events.sort(key=lambda e: e["ts_ms"])
    return events, None


def entry_to_event(entry: dict):
    """把 v2 日志条目复核并转成内部事件；不合格返回 None。
    复核口径（不全信服务端过滤）：状态码须 200；path 剥掉 query 后须
    精确等于 /api/user/register；remoteIp 为空（文档允许 null）跳过。"""
    if entry.get("statusCode") != 200:
        return None
    raw_path = entry.get("path") or ""
    if raw_path.split("?", 1)[0] != REGISTER_PATH:
        return None
    ip = entry.get("remoteIp")
    if not ip:
        return None
    ts_raw = entry.get("timestamp") or ""
    try:
        ts = parse_iso_ts(ts_raw)
    except (ValueError, TypeError):
        return None
    return {
        "ts_ms": int(round(ts * 1000)),
        "ts": ts,
        "ip": ip,
        "path": raw_path,
        "key": f"{ts_raw}|{ip}|{raw_path}",
    }


def fetch_events(api_key: str, pullzone_id: int, days, fetcher=None):
    """逐日拉取。返回 (events, skipped_days, range_start, range_end)。
    fetcher 可注入（测试用），签名同 fetch_day。"""
    fetcher = fetcher or fetch_day
    all_events = []
    skipped = []
    for day in days:
        events, reason = fetcher(api_key, pullzone_id, day)
        if reason is not None:
            skipped.append((day.strftime("%Y-%m-%d"), reason))
            log.warning("fetch | 跳过 %s（UTC）：%s", day.strftime("%Y-%m-%d"), reason)
            continue
        log.info("fetch | %s（UTC）注册成功事件 %d 条", day.strftime("%Y-%m-%d"), len(events))
        all_events.extend(events)
    if days and len(skipped) == len(days):
        raise FetchError("请求的日期全部拉取失败，见上方跳过原因")
    # 事件内去重：键 = (时间戳, IP, 路径)
    deduped = {}
    for ev in all_events:
        deduped[ev["key"]] = ev
    range_start = days[0].replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    range_end = (days[-1].replace(hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=1)).timestamp()
    return sorted(deduped.values(), key=lambda e: e["ts_ms"]), skipped, range_start, range_end


# ================= 状态文件（水位 + 已处理事件键） =================

def load_state(path: str):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return {
                    "covered_until": int(obj.get("covered_until") or 0),
                    "seen": dict(obj.get("seen") or {}),
                }
        except (ValueError, OSError) as e:
            log.warning("状态文件 %s 读取失败（%s），按空状态继续（DB 层有最终保险）", path, e)
    return {"covered_until": 0, "seen": {}}


def save_state(path: str, state: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def prune_seen(seen: dict, covered_until: float) -> dict:
    floor = covered_until - SEEN_KEEP_DAYS * 86400
    return {k: v for k, v in seen.items() if float(v) >= floor}


# ================= 数据库（双驱动，同 settle.py 约定） =================

class DB:
    """sqlite3 / psycopg 薄封装：SQL 内统一用 ? 占位符，PG 时自动换成 %s。"""

    def __init__(self, target: str):
        self.target = target
        if target.startswith(("postgres://", "postgresql://")):
            self.kind = "pg"
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError:
                log.error("选择了 PostgreSQL 但未安装 psycopg，请先 pip install 'psycopg[binary]'")
                sys.exit(EXIT_USAGE)
            self.conn = psycopg.connect(target, row_factory=dict_row)
        else:
            self.kind = "sqlite"
            import sqlite3

            self.conn = sqlite3.connect(target)
            self.conn.row_factory = sqlite3.Row
        self.cur = self.conn.cursor()

    def sql(self, text: str) -> str:
        return text.replace("?", "%s") if self.kind == "pg" else text

    def execute(self, text: str, params=()):
        return self.cur.execute(self.sql(text), params)

    def parse_setting(self, raw):
        """返回 (dict, ok)。raw 为空 → ({}, True)；非法 JSON / 非对象 → (None, False)。"""
        if raw is None or raw == "":
            return {}, True
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None, False
        if not isinstance(parsed, dict):
            return None, False
        return parsed, True

    def candidate_users(self, range_start: float, range_end: float):
        """窗口内、尚无 hajimi_reg_ip 的用户行：
        created_at ∈ [range_start + DIFF_MIN, range_end + DIFF_MAX]（匹配窗口的外延）。
        不按 status / deleted_at 过滤：禁用、删除的用户照常记录。
        返回 (users, invalid_setting_ids)：setting 损坏的用户单独列出。"""
        lo = int(range_start) + DIFF_MIN          # created_at 最小 = 事件最早 + DIFF_MIN
        hi = int(range_end) + DIFF_MAX            # created_at 最大 = 事件最晚 + DIFF_MAX
        rows = self.execute(
            "SELECT id, username, created_at, setting FROM users "
            "WHERE created_at >= ? AND created_at <= ? ORDER BY created_at, id",
            (lo, hi),
        ).fetchall()
        users, invalid = [], []
        for r in rows:
            obj, ok = self.parse_setting(r["setting"])
            if not ok:
                invalid.append({
                    "id": int(r["id"]),
                    "username": r["username"],
                    "created_at": int(r["created_at"] or 0),
                })
                continue
            if SETTING_KEY in obj:
                continue
            users.append({
                "id": int(r["id"]),
                "username": r["username"],
                "created_at": int(r["created_at"] or 0),
            })
        return users, invalid

    def write_reg_ip(self, user_id: int, payload: dict, current_obj: dict):
        """把 hajimi_reg_ip 合并进 users.setting（只动这一个键）。
        PG：SQL 层 jsonb 合并（防与返佣引擎并发写时互相覆盖）；
        SQLite：Python 层合并（同 settle.py 的 publish_stats 做法）。
        须在调用方的事务内执行。"""
        if self.kind == "pg":
            self.execute(
                "UPDATE users SET setting = "
                "(COALESCE(NULLIF(setting, '')::jsonb, '{}'::jsonb) || ?::jsonb)::text "
                "WHERE id = ?",
                (json.dumps({SETTING_KEY: payload}), user_id),
            )
        else:
            merged = dict(current_obj)
            merged[SETTING_KEY] = payload
            self.execute(
                "UPDATE users SET setting = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), user_id),
            )

    def read_back(self, user_ids):
        """回读校验用：{user_id: 已解析的 hajimi_reg_ip 或 None}。"""
        out = {}
        for uid in user_ids:
            row = self.execute("SELECT setting FROM users WHERE id = ?", (uid,)).fetchone()
            if row is None:
                out[uid] = None
                continue
            obj, ok = self.parse_setting(row["setting"])
            out[uid] = obj.get(SETTING_KEY) if ok and obj else None
        return out

    def close(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        self.conn.close()


# ================= 匹配（纯函数，可单测） =================

def match_events(users, events):
    """贪心一对一匹配。users: [{id, username, created_at}]；
    events: [{ts, ip, path, key}]。
    返回 (matches, ambiguous, unmatched_users, unmatched_events)：
      matches: [{user, event, diff}]
      ambiguous: [{user, events, abs_diff}]（最优 |diff| 并列，整单跳过）"""
    free_events = {ev["key"]: ev for ev in events}
    pairs = []          # (abs_diff, user_id, event_key, diff)
    per_user = {}       # user_id -> [(abs_diff, event_key, diff)]
    for u in users:
        cands = []
        for ev in events:
            diff = u["created_at"] - ev["ts"]
            if DIFF_MIN <= diff <= DIFF_MAX:
                cands.append((abs(diff), ev["key"], diff))
        per_user[u["id"]] = cands
        for abs_diff, key, diff in cands:
            pairs.append((abs_diff, u["id"], key, diff))

    user_by_id = {u["id"]: u for u in users}
    ambiguous = []
    ambiguous_user_ids = set()
    for u in users:
        cands = per_user[u["id"]]
        if not cands:
            continue
        best = min(c[0] for c in cands)
        tied = [c for c in cands if c[0] == best]
        if len(tied) >= 2:
            ambiguous_user_ids.add(u["id"])
            ambiguous.append({
                "user": u,
                "events": [free_events[c[1]] for c in tied],
                "abs_diff": best,
            })

    pairs.sort(key=lambda p: (p[0], p[1], p[2]))
    used_users, used_events = set(), set()
    matches = []
    for abs_diff, uid, key, diff in pairs:
        if uid in ambiguous_user_ids:
            continue
        if uid in used_users or key in used_events:
            continue
        used_users.add(uid)
        used_events.add(key)
        matches.append({"user": user_by_id[uid], "event": free_events[key], "diff": diff})

    matched_user_ids = {m["user"]["id"] for m in matches}
    unmatched_users = [
        u for u in users
        if u["id"] not in matched_user_ids and u["id"] not in ambiguous_user_ids
    ]
    unmatched_events = [ev for ev in events if ev["key"] not in used_events]
    return matches, ambiguous, unmatched_users, unmatched_events


# ================= 主流程 =================

def utc_days(backfill: int):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [today - timedelta(days=i) for i in range(backfill - 1, -1, -1)]


def run(target: str, apply: bool, backfill: int, bunny_cfg: dict,
        state_path: str, fetcher=None) -> int:
    api_key = bunny_cfg.get("BUNNY_API_KEY", "").strip()
    if not api_key:
        log.error("没有 Bunny API Key：请在配置文件里填 BUNNY_API_KEY（见 DEPLOY.md）")
        return EXIT_USAGE
    try:
        pullzone_id = int(bunny_cfg.get("BUNNY_PULLZONE_ID") or DEFAULT_PULLZONE_ID)
    except ValueError:
        log.error("BUNNY_PULLZONE_ID 不是数字：%r", bunny_cfg.get("BUNNY_PULLZONE_ID"))
        return EXIT_USAGE
    if backfill > RETENTION_DAYS:
        log.warning("backfill=%d 超过 Bunny 日志保留期 %d 天，更早的日期会被跳过并报告",
                    backfill, RETENTION_DAYS)

    days = utc_days(backfill)
    state = load_state(state_path)
    seen = state["seen"]

    events, skipped_days, range_start, range_end = fetch_events(
        api_key, pullzone_id, days, fetcher=fetcher
    )
    fresh_events = [ev for ev in events if ev["key"] not in seen]
    seen_skip = len(events) - len(fresh_events)
    if seen_skip:
        log.info("fetch | 其中 %d 条已在先前轮次处理过（seen），跳过", seen_skip)

    db = DB(target)
    try:
        mode = "正式写入 (--apply)" if apply else "空跑 (--report，只出清单不写库)"
        log.info("=== 注册 IP 记录器 | %s | 驱动=%s | 目标=%s | Pull Zone=%d | 覆盖 %s ~ %s（UTC）===",
                 mode, db.kind, mask_target(target), pullzone_id,
                 iso_of(range_start), iso_of(range_end))

        users, invalid_users = db.candidate_users(range_start, range_end)
        invalid_ids = [u["id"] for u in invalid_users]
        if invalid_ids:
            log.warning("users | %d 个窗口内用户的 setting 不是合法 JSON 对象，已排除候选"
                        "（保留原值不覆盖）：ids=%s", len(invalid_ids), invalid_ids)

        matches, ambiguous, unmatched_users, unmatched_events = match_events(users, fresh_events)

        # ---------- 报表（两种模式都出） ----------
        for m in sorted(matches, key=lambda x: x["user"]["created_at"]):
            u, ev = m["user"], m["event"]
            log.info(
                "match | user %s (id=%d) created_at=%d (%s) | ip=%s (%s) | Δ=%+.1fs | event=%s",
                u["username"], u["id"], u["created_at"], iso_of(u["created_at"]),
                ev["ip"], net_of(ev["ip"]), m["diff"], iso_of(ev["ts"]),
            )
        for a in ambiguous:
            u = a["user"]
            log.warning(
                "ambiguous | user %s (id=%d) created_at=%d：%d 个候选事件时间差并列 Δ=%.1fs，"
                "已跳过待人工看（事件时间：%s）",
                u["username"], u["id"], u["created_at"], len(a["events"]), a["abs_diff"],
                ", ".join(iso_of(e["ts"]) for e in a["events"]),
            )
        ambiguous_keys = {e["key"] for a in ambiguous for e in a["events"]}
        for ev in unmatched_events:
            if ev["key"] in ambiguous_keys:
                continue  # 已在 ambiguous 行里列过，不重复报
            log.info("unmatched-event | %s | ip=%s | path=%s",
                     iso_of(ev["ts"]), ev["ip"], ev["path"])
        for u in unmatched_users:
            nearest = None
            for ev in fresh_events:
                d = u["created_at"] - ev["ts"]
                if nearest is None or abs(d) < abs(nearest[0]):
                    nearest = (d, ev)
            if nearest is not None:
                d, ev = nearest
                log.info("unmatched-user | user %s (id=%d) created_at=%d (%s)：窗口内无可对事件；"
                         "最近事件 %s ip=%s Δ=%+.1fs（窗口校准参考）",
                         u["username"], u["id"], u["created_at"], iso_of(u["created_at"]),
                         iso_of(ev["ts"]), ev["ip"], d)
            else:
                log.info("unmatched-user | user %s (id=%d) created_at=%d (%s)：窗口内无可对事件；本轮无事件可参考",
                         u["username"], u["id"], u["created_at"], iso_of(u["created_at"]))

        log.info(
            "summary | days=%d skipped_days=%d | events=%d fresh=%d seen_skip=%d | "
            "users_in_window=%d | matched=%d | ambiguous=%d | unmatched_events=%d | "
            "unmatched_users=%d | invalid_setting=%d | mode=%s",
            len(days), len(skipped_days), len(events), len(fresh_events), seen_skip,
            len(users), len(matches), len(ambiguous), len(unmatched_events),
            len(unmatched_users), len(invalid_ids), "apply" if apply else "report",
        )

        if not apply:
            log.info("空跑结束：未写库、未推进状态文件。下轮 --apply 将按同一清单写入。")
            return EXIT_OK

        # ---------- 正式写入（单事务 + 回读校验） ----------
        # 写前再逐个复检 setting（防 candidate 查询后被并发改坏）：
        # 解析失败者跳过、不消耗其事件（不标 seen，下一轮重试，自愈）。
        written = {}       # user_id -> payload
        skipped_write = set()   # 因 setting 复检失败被跳过的 user_id
        try:
            for m in matches:
                u = m["user"]
                row = db.execute("SELECT setting FROM users WHERE id = ?", (u["id"],)).fetchone()
                if row is None:
                    skipped_write.add(u["id"])
                    continue
                obj, ok = db.parse_setting(row["setting"])
                if not ok or SETTING_KEY in obj:
                    skipped_write.add(u["id"])
                    log.warning("write | 跳过 user id=%d：setting 复检未通过或已有 %s，保留原值",
                                u["id"], SETTING_KEY)
                    continue
                payload = {
                    "ip": m["event"]["ip"],
                    "net": net_of(m["event"]["ip"]),
                    "at": int(m["event"]["ts"]),
                }
                db.write_reg_ip(u["id"], payload, obj)
                written[u["id"]] = payload
            # 回读校验（同事务内，任一不符整单回滚）
            back = db.read_back(sorted(written.keys()))
            for uid, payload in written.items():
                got = back.get(uid)
                if not got or got.get("ip") != payload["ip"] or int(got.get("at") or 0) != payload["at"]:
                    raise RuntimeError(
                        f"回读校验失败：user id={uid} 期望 {payload}，读到 {got}"
                    )
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
        log.info("write | 已写入 %d 个用户的 %s（回读校验通过）", len(written), SETTING_KEY)

        # ---------- 推进状态（仅 apply 成功后） ----------
        # 标 seen：本轮全部 fresh 事件，但排除「歧义涉及」与「写跳过用户」的
        # 事件——这两类留待下一轮重试/人工处理，不提前消耗。
        hold_keys = set()
        for a in ambiguous:
            for e in a["events"]:
                hold_keys.add(e["key"])
        for m in matches:
            if m["user"]["id"] in skipped_write:
                hold_keys.add(m["event"]["key"])
        # setting 损坏用户窗口内的事件也留住：站长修好其 setting 后下一轮可自愈
        for iu in invalid_users:
            for ev in fresh_events:
                if DIFF_MIN <= iu["created_at"] - ev["ts"] <= DIFF_MAX:
                    hold_keys.add(ev["key"])
        new_seen = dict(seen)
        for ev in fresh_events:
            if ev["key"] not in hold_keys:
                new_seen[ev["key"]] = ev["ts"]
        covered_until = max(state["covered_until"], int(range_end))
        new_state = {"covered_until": covered_until, "seen": prune_seen(new_seen, covered_until)}
        save_state(state_path, new_state)
        log.info("state | 已推进：covered_until=%d (%s)，seen=%d 条",
                 covered_until, iso_of(covered_until), len(new_state["seen"]))
        return EXIT_OK
    finally:
        db.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    ap = argparse.ArgumentParser(description="注册 IP 记录器（默认 --report 空跑）")
    ap.add_argument("--db", default=None,
                    help="PostgreSQL DSN 或 SQLite 文件路径；省略则读环境变量 RECORDER_DSN")
    ap.add_argument("--apply", action="store_true",
                    help="正式写入（默认只出清单，不写库、不推进状态）")
    ap.add_argument("--report", action="store_true",
                    help="只出清单不写库（与默认行为相同，显式写出便于脚本调用；与 --apply 同给时以 --apply 为准）")
    ap.add_argument("--backfill", type=int, default=2,
                    help="拉最近 N 个 UTC 日的日志（默认 2；Bunny 只保留 3 天）")
    ap.add_argument("--bunny-config", default=None,
                    help="Bunny 配置文件路径（默认环境变量 BUNNY_CONFIG_FILE 或 /opt/reg-ip-recorder/config）")
    ap.add_argument("--state-file", default=None,
                    help="状态文件路径（默认环境变量 RECORDER_STATE 或脚本同目录 state.json）")
    ap.add_argument("--lock-file", default=None,
                    help="单实例锁文件路径（默认 PG: /tmp/reg-ip-recorder.lock，SQLite: <db>.lock）")
    args = ap.parse_args()

    if args.backfill < 1:
        log.error("--backfill 至少为 1")
        return EXIT_USAGE

    target = args.db or os.environ.get("RECORDER_DSN")
    if not target:
        log.error("未指定数据库：请用 --db 或环境变量 RECORDER_DSN")
        return EXIT_USAGE

    script_dir = os.path.dirname(os.path.abspath(__file__))
    bunny_config_path = (
        args.bunny_config
        or os.environ.get("BUNNY_CONFIG_FILE")
        or "/opt/reg-ip-recorder/config"
    )
    state_path = (
        args.state_file
        or os.environ.get("RECORDER_STATE")
        or os.path.join(script_dir, "state.json")
    )
    is_pg = target.startswith(("postgres://", "postgresql://"))
    lock_path = (
        args.lock_file
        or os.environ.get("RECORDER_LOCK")
        or ("/tmp/reg-ip-recorder.lock" if is_pg else target + ".lock")
    )

    bunny_cfg = load_bunny_config(bunny_config_path)

    try:
        lock_fd = acquire_lock(lock_path)
    except Locked:
        log.warning("已有记录器实例在运行（锁 %s 被占用），本次跳过", lock_path)
        return EXIT_LOCKED
    except RuntimeError as e:
        log.error(str(e))
        return EXIT_USAGE

    try:
        return run(target, args.apply, args.backfill, bunny_cfg, state_path)
    except ConfigError as e:
        log.error("配置问题：%s", e)
        return EXIT_USAGE
    except Exception:
        log.exception("记录失败：事务已回滚，未写入任何数据；不重试，等下一次调度")
        return EXIT_ERROR
    finally:
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
