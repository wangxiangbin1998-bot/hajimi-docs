#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保底作品奖 · 后台管理工具（外挂式，不改 New API 源码）

配合 settle.py 的作品奖结算（2026-11-17 站长定案新增）：
- 合伙人须先申请取得作品奖资格（本工具 grant 批准），无资格者
  提交的作品不计奖；
- 合格作品 = 公开发布的介绍本站内容（抖音/B站/小红书/LINUX DO/
  V2EX/知乎/公众号等），带本站名或链接、官方 QQ 群号其一；
  合伙人把链接＋截图交群主/机器人，站长用本工具录入并审核；
- 审核通过（approve）即按当月计奖：每个 ¥5，每人每月封顶 20 个，
  由 settle.py 每日结算时发放（进 aff_quota / aff_history）；
- 作品须保留至少 7 天：发现删除用 delete 标记，settle.py 在后续
  结算中自动扣回已发奖金（只扣 aff_quota，留痕于账本表）。

表（与 settle.py 同一套 DDL，init 或 settle --apply 时自动建）：
- affiliate_work_eligibility：资格名单（user_id 唯一，status
  approved/revoked）
- affiliate_works：作品记录（status pending/approved/rejected/
  deleted，counted_month 为计奖月份 YYYY-MM，approve 时写入）
- affiliate_work_awards：发放/扣回账本（settle.py 写，本工具只读）

数据库连接（与 affcode.py 同一套解析，优先级从高到低）：
  1. --db 参数（SQLite 路径或 postgres:// DSN，实验室用）
  2. 环境变量 AFFILIATE_DSN
  3. 脚本同目录 affiliate.env 里的 AFFILIATE_DSN

用法：
  python3 works_admin.py init                       # 建三张表
  python3 works_admin.py grant <用户id或用户名> [--note 备注]
  python3 works_admin.py revoke <用户id或用户名>
  python3 works_admin.py eligibility                # 看资格名单
  python3 works_admin.py submit <用户> <链接> <平台> [--shot 截图说明]
  python3 works_admin.py pending                    # 待审核作品
  python3 works_admin.py approve <作品id>
  python3 works_admin.py reject <作品id>
  python3 works_admin.py delete <作品id>            # 标记已删除（触发扣回）
  python3 works_admin.py list [--user <用户>] [--status 状态]
  python3 works_admin.py awards [--user <用户>]     # 发放/扣回账本

退出码：0 = 成功；1 = 业务拒绝（用户不存在、状态不对等）；2 = 参数/连接问题
"""

import argparse
import os
import sys
import time
from pathlib import Path

QUOTA_PER_UNIT = 500000
WORK_AWARD_QUOTA = 5 * QUOTA_PER_UNIT   # 与 settle.py 一致：每个合格作品 ¥5
WORK_MONTHLY_CAP = 20                   # 与 settle.py 一致：每人每月封顶

EXIT_OK = 0
EXIT_REJECT = 1
EXIT_USAGE = 2

ELIGIBILITY_DDL = {
    "sqlite": """
        CREATE TABLE IF NOT EXISTS affiliate_work_eligibility (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL UNIQUE,
            status     TEXT    NOT NULL DEFAULT 'approved',
            note       TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """,
    "pg": """
        CREATE TABLE IF NOT EXISTS affiliate_work_eligibility (
            id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id    BIGINT NOT NULL UNIQUE,
            status     TEXT   NOT NULL DEFAULT 'approved',
            note       TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    """,
}

WORKS_DDL = {
    "sqlite": """
        CREATE TABLE IF NOT EXISTS affiliate_works (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            url           TEXT    NOT NULL,
            platform      TEXT,
            screenshot    TEXT,
            status        TEXT    NOT NULL DEFAULT 'pending',
            submitted_at  INTEGER NOT NULL,
            reviewed_at   INTEGER,
            counted_month TEXT,
            note          TEXT
        )
    """,
    "pg": """
        CREATE TABLE IF NOT EXISTS affiliate_works (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id       BIGINT NOT NULL,
            url           TEXT   NOT NULL,
            platform      TEXT,
            screenshot    TEXT,
            status        TEXT   NOT NULL DEFAULT 'pending',
            submitted_at  BIGINT NOT NULL,
            reviewed_at   BIGINT,
            counted_month TEXT,
            note          TEXT
        )
    """,
}

AWARDS_DDL = {
    "sqlite": """
        CREATE TABLE IF NOT EXISTS affiliate_work_awards (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            work_id      INTEGER NOT NULL,
            kind         TEXT    NOT NULL,
            user_id      INTEGER NOT NULL,
            amount_quota INTEGER NOT NULL,
            month        TEXT,
            created_at   INTEGER NOT NULL,
            UNIQUE (work_id, kind)
        )
    """,
    "pg": """
        CREATE TABLE IF NOT EXISTS affiliate_work_awards (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            work_id      BIGINT NOT NULL,
            kind         TEXT   NOT NULL,
            user_id      BIGINT NOT NULL,
            amount_quota BIGINT NOT NULL,
            month        TEXT,
            created_at   BIGINT NOT NULL,
            UNIQUE (work_id, kind)
        )
    """,
}


class DB:
    """sqlite3 / psycopg 薄封装：SQL 内统一用 ? 占位符，PG 时自动换成 %s。
    （与 settle.py 同款，保证两边行为一致。）"""

    def __init__(self, target: str):
        self.target = target
        if target.startswith(("postgres://", "postgresql://")):
            self.kind = "pg"
            import psycopg
            from psycopg.rows import dict_row

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

    def has_table(self, name: str) -> bool:
        if self.kind == "pg":
            row = self.execute(
                f"SELECT to_regclass('public.{name}') IS NOT NULL AS ok"
            ).fetchone()
            return bool(row["ok"])
        row = self.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        self.conn.close()


def resolve_dsn(cli_db):
    if cli_db:
        return cli_db
    env = os.environ.get("AFFILIATE_DSN")
    if env:
        return env
    env_file = Path(__file__).resolve().parent / "affiliate.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AFFILIATE_DSN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fmt_ts(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))


def yuan(quota) -> float:
    return (quota or 0) / QUOTA_PER_UNIT


def resolve_user(db, who: str):
    """who 为纯数字按 user id 查，否则按用户名查。返回行或 None。"""
    if who.isdigit():
        row = db.execute(
            "SELECT id, username FROM users WHERE id = ?", (int(who),)
        ).fetchone()
        if row:
            return row
    return db.execute(
        "SELECT id, username FROM users WHERE username = ?", (who,)
    ).fetchone()


def ensure_tables(db):
    db.execute(ELIGIBILITY_DDL[db.kind])
    db.execute(WORKS_DDL[db.kind])
    db.execute(AWARDS_DDL[db.kind])
    db.commit()


def require_tables(db):
    if not db.has_table("affiliate_works"):
        print("作品奖三张表还没建，先跑：works_admin.py init")
        return False
    return True


def cmd_init(db, args):
    ensure_tables(db)
    print("已建表：affiliate_work_eligibility / affiliate_works / affiliate_work_awards")
    return EXIT_OK


def cmd_grant(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    user = resolve_user(db, args.user)
    if user is None:
        print(f"用户不存在：{args.user}")
        return EXIT_REJECT
    now = int(time.time())
    existing = db.execute(
        "SELECT id, status FROM affiliate_work_eligibility WHERE user_id = ?",
        (user["id"],),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE affiliate_work_eligibility SET status = 'approved', "
            "note = ?, updated_at = ? WHERE user_id = ?",
            (args.note, now, user["id"]),
        )
    else:
        db.execute(
            "INSERT INTO affiliate_work_eligibility "
            "(user_id, status, note, created_at, updated_at) "
            "VALUES (?, 'approved', ?, ?, ?)",
            (user["id"], args.note, now, now),
        )
    db.commit()
    print(f"已批准作品奖资格：{user['username']} (id={user['id']})")
    return EXIT_OK


def cmd_revoke(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    user = resolve_user(db, args.user)
    if user is None:
        print(f"用户不存在：{args.user}")
        return EXIT_REJECT
    now = int(time.time())
    existing = db.execute(
        "SELECT id FROM affiliate_work_eligibility WHERE user_id = ?",
        (user["id"],),
    ).fetchone()
    if not existing:
        print(f"{user['username']} (id={user['id']}) 本来就没有作品奖资格记录")
        return EXIT_REJECT
    db.execute(
        "UPDATE affiliate_work_eligibility SET status = 'revoked', "
        "updated_at = ? WHERE user_id = ?",
        (now, user["id"]),
    )
    db.commit()
    print(f"已撤销作品奖资格：{user['username']} (id={user['id']})"
          "（其未结算的已通过作品将不再计奖；已发的不受影响）")
    return EXIT_OK


def cmd_eligibility(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    rows = db.execute(
        "SELECT e.user_id, e.status, e.note, e.updated_at, u.username "
        "FROM affiliate_work_eligibility e "
        "LEFT JOIN users u ON u.id = e.user_id ORDER BY e.user_id"
    ).fetchall()
    if not rows:
        print("资格名单为空")
        return EXIT_OK
    for r in rows:
        print(f"id={r['user_id']} | {r['username'] or '?'} | {r['status']} | "
              f"更新 {fmt_ts(r['updated_at'])} | {r['note'] or ''}")
    return EXIT_OK


def cmd_submit(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    user = resolve_user(db, args.user)
    if user is None:
        print(f"用户不存在：{args.user}")
        return EXIT_REJECT
    now = int(time.time())
    cur = db.execute(
        "INSERT INTO affiliate_works "
        "(user_id, url, platform, screenshot, status, submitted_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (user["id"], args.url, args.platform, args.shot, now),
    )
    db.commit()
    wid = cur.lastrowid
    if wid is None and db.kind == "pg":
        row = db.execute("SELECT MAX(id) AS id FROM affiliate_works").fetchone()
        wid = row["id"]
    elig = db.execute(
        "SELECT status FROM affiliate_work_eligibility WHERE user_id = ?",
        (user["id"],),
    ).fetchone()
    warn = "" if (elig and elig["status"] == "approved") else \
        "（注意：该用户暂无作品奖资格，通过审核也不会计奖，先 grant）"
    print(f"已录入作品 #{wid}：{user['username']} (id={user['id']}) | "
          f"{args.platform} | {args.url} | 待审核{warn}")
    return EXIT_OK


def cmd_pending(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    rows = db.execute(
        "SELECT w.id, w.user_id, w.url, w.platform, w.screenshot, "
        "w.submitted_at, u.username FROM affiliate_works w "
        "LEFT JOIN users u ON u.id = w.user_id "
        "WHERE w.status = 'pending' ORDER BY w.id"
    ).fetchall()
    if not rows:
        print("没有待审核作品")
        return EXIT_OK
    for r in rows:
        print(f"#{r['id']} | {r['username'] or '?'} (id={r['user_id']}) | "
              f"{r['platform'] or '?'} | {r['url']} | 截图：{r['screenshot'] or '-'} | "
              f"提交 {fmt_ts(r['submitted_at'])}")
    return EXIT_OK


def _get_work(db, work_id):
    return db.execute(
        "SELECT w.*, u.username FROM affiliate_works w "
        "LEFT JOIN users u ON u.id = w.user_id WHERE w.id = ?",
        (work_id,),
    ).fetchone()


def cmd_approve(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    w = _get_work(db, args.work_id)
    if w is None:
        print(f"作品不存在：#{args.work_id}")
        return EXIT_REJECT
    if w["status"] != "pending":
        print(f"作品 #{args.work_id} 当前状态是 {w['status']}，只有 pending 能通过审核")
        return EXIT_REJECT
    now = int(time.time())
    month = time.strftime("%Y-%m", time.localtime(now))
    db.execute(
        "UPDATE affiliate_works SET status = 'approved', reviewed_at = ?, "
        "counted_month = ? WHERE id = ?",
        (now, month, args.work_id),
    )
    db.commit()
    # 本月已计奖数（含本次）提示封顶进度
    cnt = db.execute(
        "SELECT COUNT(*) AS n FROM affiliate_works "
        "WHERE user_id = ? AND status = 'approved' AND counted_month = ?",
        (w["user_id"], month),
    ).fetchone()["n"]
    print(f"已通过作品 #{args.work_id}：{w['username'] or '?'} (id={w['user_id']}) | "
          f"计奖月份 {month} | 该合伙人本月已通过 {cnt} 个（封顶 {WORK_MONTHLY_CAP} 个，"
          f"¥{yuan(WORK_AWARD_QUOTA):.0f}/个，随每日结算发放）")
    return EXIT_OK


def cmd_reject(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    w = _get_work(db, args.work_id)
    if w is None:
        print(f"作品不存在：#{args.work_id}")
        return EXIT_REJECT
    if w["status"] != "pending":
        print(f"作品 #{args.work_id} 当前状态是 {w['status']}，只有 pending 能驳回")
        return EXIT_REJECT
    now = int(time.time())
    db.execute(
        "UPDATE affiliate_works SET status = 'rejected', reviewed_at = ? WHERE id = ?",
        (now, args.work_id),
    )
    db.commit()
    print(f"已驳回作品 #{args.work_id}：{w['username'] or '?'} (id={w['user_id']})")
    return EXIT_OK


def cmd_delete(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    w = _get_work(db, args.work_id)
    if w is None:
        print(f"作品不存在：#{args.work_id}")
        return EXIT_REJECT
    if w["status"] == "deleted":
        print(f"作品 #{args.work_id} 已经是 deleted 状态")
        return EXIT_REJECT
    now = int(time.time())
    db.execute(
        "UPDATE affiliate_works SET status = 'deleted', reviewed_at = ? WHERE id = ?",
        (now, args.work_id),
    )
    db.commit()
    awarded = db.execute(
        "SELECT id FROM affiliate_work_awards WHERE work_id = ? AND kind = 'award'",
        (args.work_id,),
    ).fetchone()
    tip = "已发放过奖金，下一次每日结算将自动扣回 ¥5" if awarded else "未发放过奖金，不涉及扣回"
    print(f"已标记删除作品 #{args.work_id}：{w['username'] or '?'} (id={w['user_id']}) | {tip}")
    return EXIT_OK


def cmd_list(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    sql = ("SELECT w.*, u.username FROM affiliate_works w "
           "LEFT JOIN users u ON u.id = w.user_id WHERE 1=1")
    params = []
    if args.user:
        user = resolve_user(db, args.user)
        if user is None:
            print(f"用户不存在：{args.user}")
            return EXIT_REJECT
        sql += " AND w.user_id = ?"
        params.append(user["id"])
    if args.status:
        sql += " AND w.status = ?"
        params.append(args.status)
    sql += " ORDER BY w.id"
    rows = db.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("没有符合条件的作品")
        return EXIT_OK
    for r in rows:
        print(f"#{r['id']} | {r['username'] or '?'} (id={r['user_id']}) | "
              f"{r['platform'] or '?'} | {r['status']} | 计奖月份 {r['counted_month'] or '-'} | "
              f"{r['url']} | 提交 {fmt_ts(r['submitted_at'])}")
    return EXIT_OK


def cmd_awards(db, args):
    if not require_tables(db):
        return EXIT_USAGE
    sql = ("SELECT a.*, u.username FROM affiliate_work_awards a "
           "LEFT JOIN users u ON u.id = a.user_id WHERE 1=1")
    params = []
    if args.user:
        user = resolve_user(db, args.user)
        if user is None:
            print(f"用户不存在：{args.user}")
            return EXIT_REJECT
        sql += " AND a.user_id = ?"
        params.append(user["id"])
    sql += " ORDER BY a.id"
    rows = db.execute(sql, tuple(params)).fetchall()
    if not rows:
        print("作品奖账本为空")
        return EXIT_OK
    total = 0
    for r in rows:
        total += int(r["amount_quota"])
        print(f"#{r['id']} | work #{r['work_id']} | {r['kind']} | "
              f"{r['username'] or '?'} (id={r['user_id']}) | "
              f"¥{yuan(r['amount_quota']):+.2f} | 月份 {r['month'] or '-'} | {fmt_ts(r['created_at'])}")
    print(f"合计净额 ¥{yuan(total):.2f}")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="保底作品奖后台管理（资格/作品/审核）")
    ap.add_argument("--db", default=None,
                    help="SQLite 文件路径或 postgres:// DSN；省略则读 AFFILIATE_DSN / affiliate.env")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建三张作品奖表")
    p = sub.add_parser("grant", help="批准某用户的作品奖资格")
    p.add_argument("user")
    p.add_argument("--note", default=None)
    p = sub.add_parser("revoke", help="撤销某用户的作品奖资格")
    p.add_argument("user")
    sub.add_parser("eligibility", help="列出资格名单")
    p = sub.add_parser("submit", help="录入一个待审核作品")
    p.add_argument("user")
    p.add_argument("url")
    p.add_argument("platform")
    p.add_argument("--shot", default=None, help="截图说明/链接")
    sub.add_parser("pending", help="列出待审核作品")
    p = sub.add_parser("approve", help="审核通过（按当月计奖）")
    p.add_argument("work_id", type=int)
    p = sub.add_parser("reject", help="驳回作品")
    p.add_argument("work_id", type=int)
    p = sub.add_parser("delete", help="标记作品已删除（触发扣回）")
    p.add_argument("work_id", type=int)
    p = sub.add_parser("list", help="列出作品")
    p.add_argument("--user", default=None)
    p.add_argument("--status", default=None)
    p = sub.add_parser("awards", help="查看发放/扣回账本")
    p.add_argument("--user", default=None)

    args = ap.parse_args()
    target = resolve_dsn(args.db)
    if not target:
        print("未指定数据库：请用 --db 或环境变量 AFFILIATE_DSN")
        return EXIT_USAGE
    db = DB(target)
    try:
        handler = {
            "init": cmd_init,
            "grant": cmd_grant,
            "revoke": cmd_revoke,
            "eligibility": cmd_eligibility,
            "submit": cmd_submit,
            "pending": cmd_pending,
            "approve": cmd_approve,
            "reject": cmd_reject,
            "delete": cmd_delete,
            "list": cmd_list,
            "awards": cmd_awards,
        }[args.cmd]
        return handler(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
