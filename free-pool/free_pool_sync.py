#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费池资格同步 + 防滥用保险丝（外挂式，不改 New API 源码）

口径（用户 2026-11-06 拍板）：
- 免费池 = 分组「免费池（限调用次数）」（生产名，GroupRatio=0，调用不扣费），
  渠道只挂该组；组名可用环境变量 FREE_POOL_GROUP 覆盖
  （实验室组名「免费体验」，复跑实验室时须 FREE_POOL_GROUP=免费体验）；
- 资格机制沿用实验室验通的老办法（rc.47/rc.48 均实证）：
  options 的 group_special_usable_group 预置 {"free2": {"免费池（限调用次数）": ...}}，
  用户 users.group == "free2" 时调用校验层放行免费池组，其余用户 403；
  本脚本只改 users.group（default <-> free2），绝不写任何计费配置
  （GroupRatio / ModelRatio 等一律不碰——手写全量 JSON 曾把实验室分组写塌）；
- 谁有资格（目标资格集）= 自动达标 ∪ 手工名单 − 当日保险丝暂停：
  * 自动达标：users.setting 的 hajimi_aff.effective_users >= 2
    （effective_users 由返佣结算引擎维护：被邀请人累计兑换云猫码 >= ¥10 计 1 人）；
  * 手工名单：自建表 hajimi_free_manual（站长用本脚本 manual 子命令维护），
    自动名单怎么变都不冲手工名单；
  * 保险丝：当天 logs 中 group=免费池组名 的消费记录（type=2）单人 > 100 条
    → 写暂停表 hajimi_free_suspend（user_id + 日期），当日资格摘除；
    暂停按日期生效，次日自动失效（脚本顺带清理过期暂停行）。
- 每小时一趟（cron），影子模式只打印将变更名单、不写库。

数据库（双驱动，自动识别，与 settle.py 同款）：
- --db 传 SQLite 文件路径 -> sqlite3
- --db 传 postgres:// 或 postgresql:// 开头 DSN -> psycopg
- --db 省略时读环境变量 FREE_POOL_DSN

运行纪律（与 settle.py 同风格）：
- 单实例文件锁：已有实例在跑则跳过、退出码 3；
  锁文件默认：SQLite 时 <db>.lock，PG 时 /tmp/free-pool-sync.lock，
  可用 --lock-file 或环境变量 FREE_POOL_LOCK 覆盖；
- 结构化日志：每行带时间戳与级别，每次变更一行、末尾一行汇总；
- 失败即停：任何异常整事务回滚、非零退出
  （0=成功，1=运行失败，2=参数/驱动问题，3=锁被占用），不重试连发；
- 单个用户 setting 解析失败时跳过该用户并告警，不用坏数据做判定。

用法：
  python3 free_pool_sync.py --db one-api.db            # 影子模式（只报将变更）
  python3 free_pool_sync.py --db one-api.db --apply    # 正式同步（单事务）
  FREE_POOL_DSN=postgres://u:p@127.0.0.1:5432/newapi python3 free_pool_sync.py --apply
  python3 free_pool_sync.py --db one-api.db manual list
  python3 free_pool_sync.py --db one-api.db manual add 123 --note 站长手工给
  python3 free_pool_sync.py --db one-api.db manual remove 123

可调环境变量：FREE_POOL_GROUP（默认生产组名「免费池（限调用次数）」）、
FREE_POOL_THRESHOLD（默认 2）、FREE_POOL_DAILY_LIMIT（默认 100）、
FREE_POOL_TZ（默认 Asia/Shanghai，保险丝按此时区的自然日统计）。
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

try:
    import fcntl  # Linux 单实例文件锁；生产为 Linux，必需
except ImportError:  # pragma: no cover
    fcntl = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

FREE_GROUP = os.environ.get("FREE_POOL_GROUP", "免费池（限调用次数）")  # 免费池分组名（GroupRatio=0）
QUAL_GROUP = "free2"             # 达标用户的 users.group（special usable 的键）
DEFAULT_GROUP = "default"        # 未达标时回落的组
STATS_KEY = "hajimi_aff"         # users.setting 内的返佣统计键（结算引擎维护）
THRESHOLD = int(os.environ.get("FREE_POOL_THRESHOLD", "2"))
DAILY_LIMIT = int(os.environ.get("FREE_POOL_DAILY_LIMIT", "100"))
TZ_NAME = os.environ.get("FREE_POOL_TZ", "Asia/Shanghai")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_LOCKED = 3

log = logging.getLogger("free_pool_sync")

DDL = {
    "sqlite": [
        """
        CREATE TABLE IF NOT EXISTS hajimi_free_manual (
            user_id    INTEGER PRIMARY KEY,
            note       TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hajimi_free_suspend (
            user_id    INTEGER NOT NULL,
            date       TEXT NOT NULL,
            cnt        INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, date)
        )
        """,
    ],
    "pg": [
        """
        CREATE TABLE IF NOT EXISTS hajimi_free_manual (
            user_id    BIGINT PRIMARY KEY,
            note       TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS hajimi_free_suspend (
            user_id    BIGINT NOT NULL,
            date       TEXT NOT NULL,
            cnt        BIGINT NOT NULL,
            created_at BIGINT NOT NULL,
            PRIMARY KEY (user_id, date)
        )
        """,
    ],
}


def acquire_lock(lock_path: str):
    """拿不到锁返回 None；拿到返回需保持引用的文件对象。"""
    if fcntl is None:
        log.error("当前平台无 fcntl，无法保证单实例，拒绝运行")
        sys.exit(EXIT_USAGE)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


class DB:
    """sqlite3 / psycopg 薄封装：SQL 内统一用 ? 占位符，PG 时自动换成 %s。"""

    def __init__(self, target: str):
        self.kind = None
        if target.startswith("postgres://") or target.startswith("postgresql://"):
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError:
                log.error("选择了 PostgreSQL 但未安装 psycopg，请先 pip install 'psycopg[binary]'")
                sys.exit(EXIT_USAGE)
            self.kind = "pg"
            self.conn = psycopg.connect(target, row_factory=dict_row)
        else:
            import sqlite3

            self.kind = "sqlite"
            self.conn = sqlite3.connect(target)
            self.conn.row_factory = sqlite3.Row

    def _sql(self, sql: str) -> str:
        # PG(psycopg):字面 % 须先转义为 %%,再把 ? 占位符换成 %s(顺序不可反)
        return sql.replace("%", "%%").replace("?", "%s") if self.kind == "pg" else sql

    def query(self, sql: str, args=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), tuple(args))
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def execute(self, sql: str, args=()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), tuple(args))
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def day_window():
    """返回 (今天日期字符串, 今天 0 点的 Unix 秒)，按 FREE_POOL_TZ。"""
    if ZoneInfo is not None:
        tz = ZoneInfo(TZ_NAME)
        now = datetime.now(tz)
    else:  # pragma: no cover
        now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.strftime("%Y-%m-%d"), int(start.timestamp())


def effective_users(setting_raw):
    """从 users.setting 解析 hajimi_aff.effective_users；解析失败返回 None。"""
    try:
        setting = json.loads(setting_raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(setting, dict):
        return None
    aff = setting.get(STATS_KEY) or {}
    if not isinstance(aff, dict):
        return 0
    eff = aff.get("effective_users")
    return int(eff) if isinstance(eff, (int, float)) else 0


def cmd_manual(db: DB, args) -> int:
    for ddl in DDL[db.kind]:
        db.execute(ddl)
    db.commit()
    if args.action == "list":
        rows = db.query(
            "SELECT m.user_id, m.note, m.created_at, u.username, u.\"group\" AS grp "
            "FROM hajimi_free_manual m LEFT JOIN users u ON u.id = m.user_id "
            "ORDER BY m.user_id"
        )
        if not rows:
            print("手工名单为空")
        for r in rows:
            print(f"user_id={r['user_id']} username={r['username']} "
                  f"group={r['grp']} note={r['note']} added_at={r['created_at']}")
        return EXIT_OK
    if args.action == "add":
        u = db.query("SELECT id, username FROM users WHERE id = ?", (args.user_id,))
        if not u:
            log.error("用户不存在: id=%s", args.user_id)
            return EXIT_ERROR
        db.execute(
            "INSERT INTO hajimi_free_manual (user_id, note, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET note = EXCLUDED.note"
            if db.kind == "pg" else
            "INSERT INTO hajimi_free_manual (user_id, note, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET note = excluded.note",
            (args.user_id, args.note or "", int(time.time())),
        )
        db.commit()
        log.info("手工名单新增/更新: user_id=%s username=%s note=%s",
                 args.user_id, u[0]["username"], args.note or "")
        print("下一趟同步（或立即跑一趟 --apply）后生效")
        return EXIT_OK
    if args.action == "remove":
        cur = db.execute("DELETE FROM hajimi_free_manual WHERE user_id = ?", (args.user_id,))
        db.commit()
        log.info("手工名单移除: user_id=%s 影响行数=%s", args.user_id, cur.rowcount)
        return EXIT_OK
    return EXIT_USAGE


def run_sync(db: DB, apply: bool) -> int:
    today, day_start = day_window()
    for ddl in DDL[db.kind]:
        db.execute(ddl)
    db.commit()

    manual = {r["user_id"] for r in db.query("SELECT user_id FROM hajimi_free_manual")}

    # ---- 保险丝：统计当天免费组消费条数 ----
    counts = {
        r["user_id"]: int(r["c"])
        for r in db.query(
            "SELECT user_id, COUNT(*) AS c FROM logs "
            "WHERE \"group\" = ? AND type = 2 AND created_at >= ? GROUP BY user_id",
            (FREE_GROUP, day_start),
        )
    }
    over = {uid: c for uid, c in counts.items() if c > DAILY_LIMIT}
    suspended_existing = {
        r["user_id"]
        for r in db.query("SELECT user_id FROM hajimi_free_suspend WHERE date = ?", (today,))
    }

    # ---- 候选用户 ----
    users = db.query(
        "SELECT id, username, \"group\" AS grp, setting FROM users "
        "WHERE \"group\" IN (?, ?) OR setting LIKE '%hajimi_aff%' "
        "OR id IN (SELECT user_id FROM hajimi_free_manual) "
        "OR id IN (SELECT user_id FROM hajimi_free_suspend WHERE date = ?)",
        (DEFAULT_GROUP, QUAL_GROUP, today),
    )

    grants, revokes, warns = [], [], []
    auto_hit, manual_hit = 0, 0
    for u in users:
        uid = u["id"]
        eff = effective_users(u["setting"])
        if eff is None:
            warns.append(f"SKIP user_id={uid} username={u['username']}: setting 解析失败，不做判定")
            continue
        is_auto = eff >= THRESHOLD
        is_manual = uid in manual
        if is_auto:
            auto_hit += 1
        if is_manual:
            manual_hit += 1
        suspended = uid in suspended_existing or uid in over
        qualified = (is_auto or is_manual) and not suspended
        if u["grp"] not in (DEFAULT_GROUP, QUAL_GROUP):
            if qualified or is_manual:
                warns.append(
                    f"SKIP user_id={uid} username={u['username']}: 当前组={u['grp']} "
                    "不在脚本管理范围内（只管 default/free2），不自动改组")
            continue
        target = QUAL_GROUP if qualified else DEFAULT_GROUP
        if u["grp"] == target:
            continue
        src = []
        if is_auto:
            src.append(f"auto eff={eff}")
        if is_manual:
            src.append("manual")
        if target == QUAL_GROUP:
            grants.append((uid, u["username"], "+".join(src)))
        else:
            if suspended:
                reason = f"suspended cnt={counts.get(uid, 0)}>{DAILY_LIMIT}"
            elif is_manual or is_auto:
                reason = "state changed"
            else:
                reason = f"below threshold eff={eff}"
            revokes.append((uid, u["username"], reason))

    for w in warns:
        log.warning(w)
    for uid, name, src in grants:
        log.info("GRANT  user_id=%s username=%s -> %s (%s)", uid, name, QUAL_GROUP, src)
    for uid, name, reason in revokes:
        log.info("REVOKE user_id=%s username=%s -> %s (%s)", uid, name, DEFAULT_GROUP, reason)
    for uid, c in sorted(over.items()):
        if uid not in suspended_existing:
            log.info("FUSE   user_id=%s 今日免费组调用 %s 条 > %s，写暂停（%s）",
                     uid, c, DAILY_LIMIT, today)

    if not apply:
        log.info("影子模式：不写库。将新增暂停 %s 人，将 GRANT %s 人、REVOKE %s 人",
                 len([u for u in over if u not in suspended_existing]), len(grants), len(revokes))
        print(f"dry-run: fuse_new={len([u for u in over if u not in suspended_existing])} "
              f"grants={len(grants)} revokes={len(revokes)} "
              f"auto_hit={auto_hit} manual_hit={manual_hit} suspended_today={len(suspended_existing | set(over))}")
        return EXIT_OK

    # ---- 正式执行：单事务 ----
    try:
        now_ts = int(time.time())
        for uid, c in over.items():
            if uid in suspended_existing:
                db.execute("UPDATE hajimi_free_suspend SET cnt = ? WHERE user_id = ? AND date = ?",
                           (c, uid, today))
            else:
                db.execute(
                    "INSERT INTO hajimi_free_suspend (user_id, date, cnt, created_at) VALUES (?, ?, ?, ?)",
                    (uid, today, c, now_ts))
        for uid, _name, _src in grants:
            db.execute("UPDATE users SET \"group\" = ? WHERE id = ?", (QUAL_GROUP, uid))
        for uid, _name, _reason in revokes:
            db.execute("UPDATE users SET \"group\" = ? WHERE id = ?", (DEFAULT_GROUP, uid))
        cur = db.execute("DELETE FROM hajimi_free_suspend WHERE date < ?", (today,))
        cleaned = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        db.commit()
    except Exception:
        db.rollback()
        log.exception("同步失败，整事务已回滚")
        return EXIT_ERROR

    log.info("同步完成：GRANT %s 人、REVOKE %s 人、新暂停 %s 人、清理过期暂停 %s 行"
             "（auto_hit=%s manual_hit=%s 当日暂停共 %s 人）",
             len(grants), len(revokes),
             len([u for u in over if u not in suspended_existing]), cleaned,
             auto_hit, manual_hit, len(suspended_existing | set(over)))
    return EXIT_OK


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="免费池资格同步 + 保险丝")
    parser.add_argument("--db", default=os.environ.get("FREE_POOL_DSN", ""),
                        help="SQLite 路径或 PostgreSQL DSN；省略读 FREE_POOL_DSN")
    parser.add_argument("--lock-file", default=os.environ.get("FREE_POOL_LOCK", ""))
    parser.add_argument("--apply", action="store_true", help="正式写库（缺省为影子模式）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将变更名单（与缺省影子模式相同）")
    sub = parser.add_subparsers(dest="cmd")
    mp = sub.add_parser("manual", help="手工名单维护")
    mp.add_argument("action", choices=["list", "add", "remove"])
    mp.add_argument("user_id", nargs="?", type=int)
    mp.add_argument("--note", default="")
    args = parser.parse_args()

    if not args.db:
        log.error("未指定数据库：用 --db 或环境变量 FREE_POOL_DSN")
        return EXIT_USAGE
    if args.cmd == "manual" and args.action in ("add", "remove") and args.user_id is None:
        log.error("manual %s 需要 user_id", args.action)
        return EXIT_USAGE

    if args.lock_file:
        lock_path = args.lock_file
    elif args.db.startswith("postgres"):
        lock_path = "/tmp/free-pool-sync.lock"
    else:
        lock_path = args.db + ".lock"
    lock_fd = acquire_lock(lock_path)
    if lock_fd is None:
        log.warning("已有实例在运行（锁 %s 被占用），本趟跳过", lock_path)
        return EXIT_LOCKED

    db = DB(args.db)
    try:
        if args.cmd == "manual":
            return cmd_manual(db, args)
        return run_sync(db, apply=args.apply and not args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
