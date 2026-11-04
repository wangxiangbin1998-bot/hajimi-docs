#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合伙人百分比返佣结算脚本（外挂式，不改 New API 源码）—— 生产自动版

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


def yuan(quota: int) -> float:
    return quota / QUOTA_PER_UNIT


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

    def close(self):
        try:
            self.conn.rollback()  # 未提交的读事务/残留一律丢弃
        except Exception:
            pass
        self.conn.close()


def run(target: str, apply: bool) -> int:
    db = DB(target)
    try:
        mode = "正式结算 (--apply)" if apply else "影子模式（只算账，不写库）"
        log.info("=== 合伙人返佣结算 | %s | 驱动=%s | 目标=%s ===",
                 mode, db.kind, mask_target(target))

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
        for u in db.execute("SELECT id, username, inviter_id FROM users").fetchall():
            inviter_of[u["id"]] = u["inviter_id"] or 0
            username_of[u["id"]] = u["username"]

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
                    "| invitee %s (id=%d) | 面额 ¥%.2f | 佣金 ¥%.2f（%d 额度）",
                    rid, username_of.get(inv, "?"), inv, effective_count.get(inv, 0),
                    f"{slot['rate'] * 100:g}", username_of.get(invitee_id, "?"), invitee_id,
                    yuan(face), yuan(comm), comm,
                )
        if skipped_no_inviter:
            log.info("跳过（云猫已兑换但兑换人无邀请人）：redemption %s", skipped_no_inviter)
        log.info(
            "summary | inviters=%d | settled_items=%d | 计佣面额 ¥%.2f | 佣金 ¥%.2f（%d 额度）| mode=%s",
            len(per_inviter), total_items, yuan(total_face), yuan(total_comm), total_comm,
            "apply" if apply else "shadow",
        )

        if not apply:
            log.info("影子模式结束：未写账本、未改 aff_quota。")
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
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
        n1, total1 = db.ledger_stats()
        log.info("已写入账本 %d 笔，aff_quota 已更新 | 账本现状：rows=%d total_commission_quota=%d",
                 total_items, n1, total1)
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
    args = ap.parse_args()

    target = args.db or os.environ.get("AFFILIATE_DSN")
    if not target:
        log.error("未指定数据库：请用 --db 或环境变量 AFFILIATE_DSN")
        return EXIT_USAGE

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
        return run(target, args.apply)
    except Exception:
        log.exception("结算失败：事务已回滚，未写入任何数据；不重试，等下一次调度")
        return EXIT_ERROR
    finally:
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())
