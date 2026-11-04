#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合伙人百分比返佣结算脚本（外挂式，不改 New API 源码）—— 生产自动版 + 统计发布 + 防刷标记

口径（用户 2026-11-04 拍板，详见同目录 SPEC.md）：
- 计佣范围：只有云猫寄售卖出的兑换码被兑换入账才算
  （redemptions 表，靠批次 name 以「云猫」开头识别；status=3 已兑换、
   redeemed_time>0、used_user_id>0）
- 在线支付 top_ups、管理员手动入账、非云猫兑换码，一律不算
- 计佣基数：兑换码面额 quota / 500000 = 元（price=1，不计云猫手续费）
- 不冻结，入账即结：每次运行把全部未结算记录一次结清
- 阶梯比例按邀请人名下「累计有效用户数」定档：
  0-49 人 5%，50-99 人 7%，100-199 人 8.5%，200+ 人 10%
  有效用户 = 该邀请人名下、累计兑换云猫码面额 >= 10 元的被邀请人
- 佣金写入邀请人 users.aff_quota 并同步 aff_history（仿原生邀请账本）
- 幂等：自建账本表 affiliate_settlements，redemption_id 唯一键，
  已结算的兑换码永不重复结算

本版新增（2026-11-04，详见 SPEC.md「数据桥」与「防刷标记」两节）：
- 统计发布（数据桥）：--apply 时把每个邀请人的返利统计（档位/有效用户/
  累计佣金/最近记录/邀请名单）合并写入其 users.setting 的 hajimi_aff 键，
  供站内 /referrals 展示页同域读取；影子模式只报将发布人数、不写库。
  setting 原有键一律保留；某用户 setting 解析失败时跳过该用户并告警，
  保留原值不用坏数据覆盖。
- 防刷标记层（只标记、不拦截、不改即结口径）：每轮报表末尾输出异常标记，
  供站长人工复核——同注册 IP（users 表有该字段时）、单日新增被邀请人
  激增、人工黑名单（denylist.txt）邀请人的本期佣金单独标出汇总。
  黑名单默认不拦截发放，是否拦截等站长后续定。

数据库（双驱动，自动识别）：
- --db 传 SQLite 文件路径  -> sqlite3
- --db 传 postgres:// 或 postgresql:// 开头的 DSN -> psycopg (PostgreSQL)
- --db 省略时读环境变量 AFFILIATE_DSN（同样两种形态都认）

生产自动模式（见 SPEC.md「自动模式」一节）：
- 单实例文件锁：同一时刻只允许一个进程结算；已有实例在跑则跳过、退出码 3
  锁文件默认：SQLite 时为 <db>.lock，PG 时为 /tmp/affiliate-settle.lock，
  可用 --lock-file 或环境变量 AFFILIATE_LOCK 覆盖
- --apply 前先把账本表当前计数（行数、佣金总额）打进日志作备份点
- 结构化日志：每行带时间戳与级别；每笔结算一行、末尾一行汇总
- 失败即停：任何异常整事务回滚、非零退出（1=结算失败，2=参数/驱动问题，
  3=锁被占用），不重试、不连发，等下一次调度

用法：
  python3 settle.py --db test.db              # 影子模式：只算账，不写库
  python3 settle.py --db test.db --apply      # 正式结算（单事务）
  AFFILIATE_DSN=postgres://u:p@127.0.0.1:5432/newapi python3 settle.py --apply
"""

import argparse
import json
import logging
import os
import sys
import time

try:
    import fcntl  # Linux 单实例文件锁；生产为 Linux，必需
except ImportError:  # pragma: no cover
    fcntl = None

QUOTA_PER_UNIT = 500000          # 500000 额度 = ¥1
NAME_PREFIX = "云猫"              # 云猫批次识别前缀（唯一识别依据）
EFFECTIVE_THRESHOLD_QUOTA = 10 * QUOTA_PER_UNIT  # 有效用户门槛：累计面额 >= ¥10
TIERS = [                        # (累计有效用户下限, 比例)，从高到低匹配
    (200, 0.10),
    (100, 0.085),
    (50, 0.07),
    (0, 0.05),
]
TIER_DEFS = [                    # (下限, 上限或 None, 档位名, 比例)，从低到高
    (0, 49, "普通合伙人", 0.05),
    (50, 99, "银牌合伙人", 0.07),
    (100, 199, "金牌合伙人", 0.085),
    (200, None, "钻石合伙人", 0.10),
]

# ---- 统计发布（数据桥）----
STATS_KEY = "hajimi_aff"         # users.setting 内的自定义键
STATS_RECORDS_LIMIT = 20         # 发布最近多少条返利记录
STATS_INVITEES_LIMIT = 100       # 发布的邀请名单上限

# ---- 防刷标记（只标记不拦截）----
DAILY_INVITEE_ALERT = 20         # 单个邀请人单日新增被邀请人 > 此数 → 激增标记
FLAG_WINDOW_DAYS = 7             # 激增标记只看近 N 天（避免历史噪音刷屏）
REGISTER_IP_COLUMNS = ("register_ip", "reg_ip", "signup_ip", "ip")  # 候选注册 IP 列名

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3

LEDGER_DDL = {
    "sqlite": """
        CREATE TABLE IF NOT EXISTS affiliate_settlements (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            redemption_id    INTEGER NOT NULL UNIQUE,
            inviter_id       INTEGER NOT NULL,
            invitee_id       INTEGER NOT NULL,
            face_quota       INTEGER NOT NULL,
            rate             REAL    NOT NULL,
            commission_quota INTEGER NOT NULL,
            created_at       INTEGER NOT NULL
        )
    """,
    "pg": """
        CREATE TABLE IF NOT EXISTS affiliate_settlements (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            redemption_id    BIGINT NOT NULL UNIQUE,
            inviter_id       BIGINT NOT NULL,
            invitee_id       BIGINT NOT NULL,
            face_quota       BIGINT NOT NULL,
            rate             DOUBLE PRECISION NOT NULL,
            commission_quota BIGINT NOT NULL,
            created_at       BIGINT NOT NULL
        )
    """,
}

log = logging.getLogger("affiliate-settle")


def tier_rate(effective_count: int) -> float:
    for threshold, rate in TIERS:
        if effective_count >= threshold:
            return rate
    return TIERS[-1][1]


def tier_info(effective_count: int):
    """返回 (档位名, 比例, 下一档 dict 或 None)。下一档含 name/at/gap。"""
    for i, (lo, hi, name, rate) in enumerate(TIER_DEFS):
        if hi is None or effective_count <= hi:
            nxt = None
            if i + 1 < len(TIER_DEFS):
                nlo, _, nname, _ = TIER_DEFS[i + 1]
                nxt = {"name": nname, "at": nlo, "gap": nlo - effective_count}
            return name, rate, nxt
    name, rate = TIER_DEFS[-1][2], TIER_DEFS[-1][3]
    return name, rate, None


def yuan(quota: int) -> float:
    return quota / QUOTA_PER_UNIT


def yuan2(quota: int) -> float:
    return round(quota / QUOTA_PER_UNIT, 2)


def mask_target(target: str) -> str:
    """日志里展示连接目标：DSN 打码密码部分。"""
    if "://" in target:
        scheme, rest = target.split("://", 1)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return target
    return target


class Locked(Exception):
    pass


def acquire_lock(lock_path: str):
    """非阻塞独占文件锁。返回打开的文件对象（进程退出自动释放）；被占用抛 Locked。"""
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


def load_denylist(path: str):
    """读人工黑名单文件：每行一个 user id，# 开头为注释，空行忽略。
    文件不存在返回空集（黑名单是可选项）。坏行告警跳过。"""
    ids = set()
    if not path or not os.path.exists(path):
        return ids
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            try:
                ids.add(int(s))
            except ValueError:
                log.warning("denylist %s 第 %d 行不是合法 user id，已跳过：%r", path, lineno, s)
    return ids


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

    def has_ledger(self) -> bool:
        if self.kind == "pg":
            row = self.execute(
                "SELECT to_regclass('public.affiliate_settlements') IS NOT NULL AS ok"
            ).fetchone()
            return bool(row["ok"])
        row = self.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='affiliate_settlements'"
        ).fetchone()
        return row is not None

    def ledger_stats(self):
        """账本当前计数：(行数, 佣金总额 quota)。表不存在返回 (0, 0)。"""
        if not self.has_ledger():
            return 0, 0
        row = self.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(commission_quota), 0) AS total "
            "FROM affiliate_settlements"
        ).fetchone()
        return int(row["n"]), int(row["total"])

    def user_columns(self):
        """users 表的列名集合（防刷同 IP 检查用来探测注册 IP 字段）。"""
        if self.kind == "pg":
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            ).fetchall()
            return {r["column_name"] for r in rows}
        rows = self.execute("PRAGMA table_info(users)").fetchall()
        return {r["name"] for r in rows}

    def close(self):
        try:
            self.conn.rollback()  # 未提交的读事务/残留一律丢弃
        except Exception:
            pass
        self.conn.close()


# ================= 防刷标记层（只标记、不拦截、不扣留） =================

def collect_fraud_flags(db, users, denylist_ids, per_inviter, username_of, now):
    """返回一组日志行（字符串）。users 为全量用户行（含 created_at/inviter_id）。
    标记只是给站长人工看的提示：不影响结算、不扣留佣金。"""
    lines = []

    # ① 邀请人与被邀请人注册 IP 相同（users 表有注册 IP 字段才查）
    cols = db.user_columns()
    ip_col = next((c for c in REGISTER_IP_COLUMNS if c in cols), None)
    if ip_col is None:
        lines.append("flag-check | 同注册 IP 检查跳过：users 表无注册 IP 字段")
    else:
        ip_of = {}
        rows = db.execute(f"SELECT id, {ip_col} AS reg_ip FROM users").fetchall()
        for r in rows:
            ip_of[r["id"]] = (r["reg_ip"] or "").strip()
        hits = {}
        for u in users:
            inv = u["inviter_id"] or 0
            if inv and ip_of.get(u["id"]) and ip_of.get(u["id"]) == ip_of.get(inv):
                hits.setdefault(inv, []).append(u["id"])
        for inv, invitee_ids in sorted(hits.items()):
            lines.append(
                "flag | same-ip | inviter %s (id=%d) 与 %d 名被邀请人注册 IP 相同：invitee ids=%s"
                % (username_of.get(inv, "?"), inv, len(invitee_ids), invitee_ids)
            )

    # ② 单个邀请人单日新增被邀请人激增（近 FLAG_WINDOW_DAYS 天）
    window_start = now - FLAG_WINDOW_DAYS * 86400
    buckets = {}  # (inviter_id, 本地日期) -> count
    for u in users:
        inv = u["inviter_id"] or 0
        created = u["created_at"] or 0
        if inv and created >= window_start:
            day = time.strftime("%Y-%m-%d", time.localtime(created))
            buckets[(inv, day)] = buckets.get((inv, day), 0) + 1
    for (inv, day), cnt in sorted(buckets.items()):
        if cnt > DAILY_INVITEE_ALERT:
            lines.append(
                "flag | daily-spike | inviter %s (id=%d) 在 %s 单日新增被邀请人 %d 人（阈值 %d）"
                % (username_of.get(inv, "?"), inv, day, cnt, DAILY_INVITEE_ALERT)
            )

    # ③ 黑名单邀请人：本期佣金单独标出汇总（默认不拦截发放）
    if denylist_ids:
        for inv in sorted(denylist_ids):
            slot = per_inviter.get(inv)
            if slot:
                lines.append(
                    "flag | denylist | inviter %s (id=%d) 在黑名单内，本期照常结算（未拦截）："
                    "面额 ¥%.2f | 佣金 ¥%.2f（%d 额度）| %d 笔"
                    % (username_of.get(inv, "?"), inv, yuan(slot["face_quota"]),
                       yuan(slot["commission_quota"]), slot["commission_quota"], len(slot["items"]))
                )
    return lines


# ================= 统计发布（数据桥） =================

def build_inviter_stats(db, inviter_id, invitees, invitee_face, effective_count, username_of, now):
    """组装一个邀请人的 hajimi_aff 统计载荷。
    invitees: 该邀请人名下全部被邀请人行（按注册时间升序）。"""
    eff = effective_count.get(inviter_id, 0)
    tname, rate, nxt = tier_info(eff)

    total_commission_quota = 0
    records = []
    if db.has_ledger():
        total_row = db.execute(
            "SELECT COALESCE(SUM(commission_quota), 0) AS total FROM affiliate_settlements "
            "WHERE inviter_id = ?",
            (inviter_id,),
        ).fetchone()
        total_commission_quota = int(total_row["total"])
        rows = db.execute(
            "SELECT invitee_id, face_quota, rate, commission_quota, created_at "
            "FROM affiliate_settlements WHERE inviter_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (inviter_id, STATS_RECORDS_LIMIT),
        ).fetchall()
        for r in rows:
            records.append({
                "time": int(r["created_at"]),
                "from_user": username_of.get(r["invitee_id"], "?"),
                "face_yuan": yuan2(r["face_quota"]),
                "commission_yuan": yuan2(r["commission_quota"]),
                "rate": r["rate"],
            })

    invitee_list = []
    for u in invitees[:STATS_INVITEES_LIMIT]:
        face = invitee_face.get(u["id"], 0)
        invitee_list.append({
            "username": u["username"],
            "created_at": int(u["created_at"] or 0),
            "redeemed_yuan": yuan2(face),
            "effective": face >= EFFECTIVE_THRESHOLD_QUOTA,
        })

    return {
        "v": 1,
        "updated_at": now,
        "tier_name": tname,
        "rate": rate,
        "effective_users": eff,
        "invited_total": len(invitees),
        "next_tier": nxt,
        "total_commission_yuan": yuan2(total_commission_quota),
        "records": records,
        "invitees": invitee_list,
    }


def publish_stats(db, invitees_by_inviter, ledger_inviters, invitee_face,
                  effective_count, username_of, now):
    """把统计合并写入每个邀请人的 users.setting[hajimi_aff]。
    返回 (已发布人数, 跳过人数)。setting 解析失败的用户跳过并告警——
    保留原值，绝不用覆盖写毁掉用户既有设置；下一轮结算会重试（自愈）。"""
    targets = sorted(set(invitees_by_inviter.keys()) | set(ledger_inviters))
    published = skipped = 0
    for inv in targets:
        row = db.execute("SELECT setting FROM users WHERE id = ?", (inv,)).fetchone()
        if row is None:
            continue
        raw = row["setting"]
        obj = {}
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if not isinstance(parsed, dict):
                log.warning(
                    "stats-publish | 跳过 user id=%d：现有 setting 不是合法 JSON 对象，保留原值不覆盖",
                    inv,
                )
                skipped += 1
                continue
            obj = parsed
        obj[STATS_KEY] = build_inviter_stats(
            db, inv, invitees_by_inviter.get(inv, []), invitee_face,
            effective_count, username_of, now,
        )
        db.execute(
            "UPDATE users SET setting = ? WHERE id = ?",
            (json.dumps(obj, ensure_ascii=False), inv),
        )
        published += 1
    return published, skipped


def run(target: str, apply: bool, denylist_path: str = None) -> int:
    db = DB(target)
    try:
        mode = "正式结算 (--apply)" if apply else "影子模式（只算账，不写库）"
        log.info("=== 合伙人返佣结算 | %s | 驱动=%s | 目标=%s ===",
                 mode, db.kind, mask_target(target))

        denylist_ids = load_denylist(denylist_path)
        if denylist_ids:
            log.info("黑名单已加载：%s（%d 个 user id，仅标记不拦截）",
                     denylist_path, len(denylist_ids))

        # ---- --apply 前备份点：账本当前计数先落日志 ----
        if apply:
            n0, total0 = db.ledger_stats()
            log.info("[ledger-backup] apply 前账本：rows=%d total_commission_quota=%d（¥%.2f）",
                     n0, total0, yuan(total0))

        settled_ids = set()
        if db.has_ledger():
            settled_ids = {
                r["redemption_id"]
                for r in db.execute("SELECT redemption_id FROM affiliate_settlements").fetchall()
            }

        # 全部「云猫已兑换」记录（定档算累计有效用户要用全量历史，不只本期新增）
        all_yunmao = db.execute(
            """
            SELECT id, quota, used_user_id
            FROM redemptions
            WHERE status = 3 AND redeemed_time > 0 AND used_user_id > 0
              AND name LIKE ?
            """,
            (NAME_PREFIX + "%",),
        ).fetchall()

        invitee_face = {}   # invitee_id -> 累计云猫面额 quota
        for r in all_yunmao:
            invitee_face[r["used_user_id"]] = invitee_face.get(r["used_user_id"], 0) + r["quota"]

        inviter_of = {}     # user_id -> inviter_id
        username_of = {}
        created_at_of = {}
        users = db.execute("SELECT id, username, inviter_id, created_at FROM users").fetchall()
        for u in users:
            inviter_of[u["id"]] = u["inviter_id"] or 0
            username_of[u["id"]] = u["username"]
            created_at_of[u["id"]] = u["created_at"] or 0

        invitees_by_inviter = {}  # inviter_id -> [被邀请人行，按注册时间升序]
        for u in sorted(users, key=lambda x: (x["created_at"] or 0, x["id"])):
            inv = u["inviter_id"] or 0
            if inv:
                invitees_by_inviter.setdefault(inv, []).append(u)

        effective_count = {}  # inviter_id -> 累计有效用户数
        for invitee_id, face in invitee_face.items():
            inv = inviter_of.get(invitee_id, 0)
            if inv and face >= EFFECTIVE_THRESHOLD_QUOTA:
                effective_count[inv] = effective_count.get(inv, 0) + 1

        pending = [r for r in all_yunmao if r["id"] not in settled_ids]

        per_inviter = {}    # inviter_id -> dict(rate, face_quota, commission_quota, items[])
        skipped_no_inviter = []
        for r in pending:
            invitee_id = r["used_user_id"]
            inv = inviter_of.get(invitee_id, 0)
            if not inv:
                skipped_no_inviter.append(r["id"])
                continue
            rate = tier_rate(effective_count.get(inv, 0))
            commission = round(r["quota"] * rate)
            slot = per_inviter.setdefault(
                inv, {"rate": rate, "face_quota": 0, "commission_quota": 0, "items": []}
            )
            slot["face_quota"] += r["quota"]
            slot["commission_quota"] += commission
            slot["items"].append((r["id"], invitee_id, r["quota"], commission))

        # ---------- 报表（逐笔一行 + 汇总） ----------
        log.info("口径：仅云猫兑换码 | 基数=面额 | 已结算跳过 %d 笔", len(settled_ids))
        total_face = total_comm = total_items = 0
        for inv, slot in sorted(per_inviter.items()):
            total_face += slot["face_quota"]
            total_comm += slot["commission_quota"]
            total_items += len(slot["items"])
            for rid, invitee_id, face, comm in slot["items"]:
                log.info(
                    "settle redemption #%d | inviter %s (id=%d, 有效用户 %d, 档位 %s%%) "
                    "| invitee %s (id=%d) | 面额 ¥%.2f | 佣金 ¥%.2f（%d 额度）%s",
                    rid, username_of.get(inv, "?"), inv, effective_count.get(inv, 0),
                    f"{slot['rate'] * 100:g}", username_of.get(invitee_id, "?"), invitee_id,
                    yuan(face), yuan(comm), comm,
                    " | [黑名单标记]" if inv in denylist_ids else "",
                )
        if skipped_no_inviter:
            log.info("跳过（云猫已兑换但兑换人无邀请人）：redemption %s", skipped_no_inviter)
        log.info(
            "summary | inviters=%d | settled_items=%d | 计佣面额 ¥%.2f | 佣金 ¥%.2f（%d 额度）| mode=%s",
            len(per_inviter), total_items, yuan(total_face), yuan(total_comm), total_comm,
            "apply" if apply else "shadow",
        )

        # ---------- 防刷标记（只标记，两种模式都输出，供站长人工复核） ----------
        now0 = int(time.time())
        flag_lines = collect_fraud_flags(db, users, denylist_ids, per_inviter, username_of, now0)
        real_flags = [l for l in flag_lines if l.startswith("flag |")]
        for line in flag_lines:
            if line.startswith("flag |"):
                log.warning("%s", line)
            else:
                log.info("%s", line)
        if not real_flags:
            log.info("flag | 无异常标记")

        if not apply:
            would_publish = len(set(invitees_by_inviter.keys()))
            log.info("影子模式结束：未写账本、未改 aff_quota、未发布统计（--apply 时将为 %d 个邀请人发布 hajimi_aff 统计）。",
                     would_publish)
            return EXIT_OK

        # ---------- 正式结算（单事务，异常整单回滚） ----------
        # 注意：不用 `with conn:` 做事务——psycopg3 的连接上下文管理器退出时会
        # 直接关闭连接（与 sqlite3 只管事务的语义不同），故统一手动 commit/rollback。
        now = int(time.time())
        try:
            db.execute(LEDGER_DDL[db.kind])
            for inv, slot in per_inviter.items():
                for rid, invitee_id, face, comm in slot["items"]:
                    db.execute(
                        """
                        INSERT INTO affiliate_settlements
                            (redemption_id, inviter_id, invitee_id, face_quota, rate, commission_quota, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (rid, inv, invitee_id, face, slot["rate"], comm, now),
                    )
                db.execute(
                    "UPDATE users SET aff_quota = aff_quota + ?, "
                    "aff_history = aff_history + ? WHERE id = ?",
                    (slot["commission_quota"], slot["commission_quota"], inv),
                )
            # ---- 统计发布：与结算同一事务，失败整单回滚 ----
            ledger_inviters = {
                r["inviter_id"]
                for r in db.execute("SELECT DISTINCT inviter_id FROM affiliate_settlements").fetchall()
            }
            published, pub_skipped = publish_stats(
                db, invitees_by_inviter, ledger_inviters, invitee_face,
                effective_count, username_of, now,
            )
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
        n1, total1 = db.ledger_stats()
        log.info("已写入账本 %d 笔，aff_quota 已更新 | 账本现状：rows=%d total_commission_quota=%d",
                 total_items, n1, total1)
        log.info("stats-publish | 已发布 %d 个邀请人的 hajimi_aff 统计（跳过 %d 个）",
                 published, pub_skipped)
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
    ap = argparse.ArgumentParser(description="合伙人百分比返佣结算（默认影子模式）")
    ap.add_argument("--db", default=None,
                    help="SQLite 文件路径，或 postgres:// DSN；省略则读环境变量 AFFILIATE_DSN")
    ap.add_argument("--apply", action="store_true",
                    help="正式结算：写账本表并更新 aff_quota/aff_history")
    ap.add_argument("--lock-file", default=None,
                    help="单实例锁文件路径（默认 SQLite: <db>.lock，PG: /tmp/affiliate-settle.lock）")
    ap.add_argument("--denylist", default=None,
                    help="黑名单文件路径（默认读环境变量 AFFILIATE_DENYLIST，再默认脚本同目录 denylist.txt）")
    args = ap.parse_args()

    target = args.db or os.environ.get("AFFILIATE_DSN")
    if not target:
        log.error("未指定数据库：请用 --db 或环境变量 AFFILIATE_DSN")
        return EXIT_USAGE

    denylist_path = (
        args.denylist
        or os.environ.get("AFFILIATE_DENYLIST")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "denylist.txt")
    )

    is_pg = target.startswith(("postgres://", "postgresql://"))
    lock_path = (
        args.lock_file
        or os.environ.get("AFFILIATE_LOCK")
        or ("/tmp/affiliate-settle.lock" if is_pg else target + ".lock")
    )
    try:
        lock_fd = acquire_lock(lock_path)
    except Locked:
        log.warning("已有结算实例在运行（锁 %s 被占用），本次跳过", lock_path)
        return EXIT_LOCKED
    except RuntimeError as e:
        log.error(str(e))
        return EXIT_USAGE

    try:
        return run(target, args.apply, denylist_path)
    except Exception:
        log.exception("结算失败：事务已回滚，未写入任何数据；不重试，等下一次调度")
        return EXIT_ERROR
    finally:
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
