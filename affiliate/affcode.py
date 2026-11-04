#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义邀请码 · 后台改码工具（外挂式，不改 New API 源码）

规矩 v2（站长 2026-11-05 拍板）：
- 格式：5–32 位、仅大写字母 + 数字；禁纯数字联系方式形态（如 11 位手机号样）
- 门槛：有效邀请满 3 人（有效 = 被邀请人累计兑换云猫码满 ¥10，
  口径与 settle.py 完全一致：redemptions 里 status=3、redeemed_time>0、
  批次 name 以「云猫」开头，按 used_user_id 汇总面额 >= 10 * 500000）
- 唯一、先到先得（users.aff_code 本身带 UNIQUE 索引，库层强制）
- 大小写防线（2026-11-05 补）：库层 UNIQUE 区分大小写，Xiaowang 与
  XIAOWANG 能同时入库；但皮肤已定「≥5 位码提交前自动转大写」，小写存的
  码会被串绑到大写码主名下——故本工具一切按大写形处理：站长输入的新码
  先整体转大写再走全部校验，落库与台账新条目只存大写形；撞现存码与
  台账历史码的比对一律大小写不敏感（两侧都转大写），任何大小写变体
  都视为同一个码，严于库层约束
- 用过的码永不回收：换掉 / 封号 / 注销的码永久退役——靠台账拦截，
  台账里出现过的任何码（旧码、新码、seed 存量）都不许再发给任何人
- 90 天内限改 1 次（按台账里该用户最近一次 apply 改码记录算）
- 申请要验注册邮箱：check 会完整打印账号注册邮箱，站长人工核对
  申请人报的邮箱是否一致；账号没留邮箱 = 无法核验，直接拦截
- 保留词（子串命中即拒）：OFFICIAL / ADMIN / ROOT / SUPPORT / SYSTEM /
  GUANFANG / KEFU / HAJIMI（本体及冒充官方组合，如 HAJIMIOFFICIAL）

两段式防误操作：
  check  只查不改，逐项打印校验结果，最后给「可改 / 不可改 + 原因」
  apply  重跑全部校验，全过才 UPDATE 并写台账；任何一项不过拒绝执行
  seed   一次性把当前全站已用 aff_code 导进台账（标「seed存量」），
         让永不回收从第一天就完整；台账已有内容时拒绝重复 seed

台账：affcode-ledger.tsv（默认与脚本同目录，可用 --ledger 或环境变量
AFFCODE_LEDGER 覆盖）。字段（TSV，带表头）：
  时间 | 用户 id | 用户名 | 旧码 | 新码 | 操作备注

数据库连接（与 settle.py / dsn.sh 同一套解析，优先级从高到低）：
  1. --db 参数（SQLite 路径或 postgres:// DSN，实验室用）
  2. 环境变量 AFFILIATE_DSN
  3. 脚本同目录 affiliate.env 里的 AFFILIATE_DSN（非空时）
  4. 自动解析：grep /opt/newapi/ 里的 SQL_DSN（密码/库名照用），
     主机换成 docker 容器 newapi-postgres 的当前 IP:5432
     （目录可用环境变量 AFFCODE_NEWAPI_DIR 覆盖）

用法：
  python3 affcode.py check <用户名> <新码>
  python3 affcode.py apply <用户名> <新码>
  python3 affcode.py seed
  python3 affcode.py --db /path/one-api.db check <用户名> <新码>   # 实验室

退出码：0 = 通过 / 成功；1 = 校验不过 / 拒绝执行；2 = 参数或连接问题
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

QUOTA_PER_UNIT = 500000          # 与 settle.py 一致：500000 额度 = ¥1
NAME_PREFIX = "云猫"              # 与 settle.py 一致：云猫批次识别前缀
EFFECTIVE_THRESHOLD_QUOTA = 10 * QUOTA_PER_UNIT  # 有效用户：累计面额 >= ¥10
EFFECTIVE_REQUIRED = 3           # 申请门槛：有效邀请满 3 人
MIN_LEN, MAX_LEN = 5, 32         # 码长（随机码为 4 位，自定义码 5 位起错开空间）
COOLDOWN_DAYS = 90               # 90 天内限改 1 次
RESERVED_WORDS = ("OFFICIAL", "ADMIN", "ROOT", "SUPPORT", "SYSTEM",
                  "GUANFANG", "KEFU", "HAJIMI")
CODE_RE = re.compile(r"^[A-Z0-9]+$")

EXIT_OK = 0
EXIT_REJECT = 1
EXIT_USAGE = 2

LEDGER_HEADER = "时间\t用户id\t用户名\t旧码\t新码\t操作备注"
NOTE_SEED = "seed存量"
NOTE_APPLY = "apply改码"


# ================= 数据库（照 settle.py 的薄封装） =================

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
                print("错误：选择了 PostgreSQL 但未安装 psycopg，请先 pip install 'psycopg[binary]'",
                      file=sys.stderr)
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

    def execute(self, text, params=()):
        return self.cur.execute(self.sql(text), params)

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.rollback()
        except Exception:
            pass
        self.conn.close()


def mask_target(target: str) -> str:
    """展示连接目标：DSN 打码密码部分（同 settle.py）。"""
    if "://" in target:
        scheme, rest = target.split("://", 1)
        if "@" in rest:
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
        return target
    return target


def resolve_target(cli_db: str) -> str:
    """DSN 自动解析：与 install.sh 生成的 dsn.sh 同一套逻辑。"""
    if cli_db:
        return cli_db
    dsn = os.environ.get("AFFILIATE_DSN", "").strip()
    if dsn:
        return dsn
    env_file = Path(__file__).resolve().with_name("affiliate.env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("AFFILIATE_DSN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    # 自动解析：New API 现行 SQL_DSN（密码/库名）+ postgres 容器当前 IP
    newapi_dir = os.environ.get("AFFCODE_NEWAPI_DIR", "/opt/newapi")
    raw = ""
    try:
        out = subprocess.run(
            ["grep", "-rho", r"SQL_DSN=[^ \"]*", newapi_dir],
            capture_output=True, text=True, timeout=15,
        )
        for line in out.stdout.splitlines():
            if line.startswith("SQL_DSN=") and line[len("SQL_DSN="):]:
                raw = line[len("SQL_DSN="):]
                break
    except Exception:
        raw = ""
    pg_ip = ""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             "newapi-postgres"],
            capture_output=True, text=True, timeout=15,
        )
        pg_ip = out.stdout.strip()
    except Exception:
        pg_ip = ""
    if raw and pg_ip:
        return re.sub(r"@[^/]+/", f"@{pg_ip}:5432/", raw, count=1)
    return ""


# ================= 台账 =================

def ledger_path(cli_ledger: str) -> Path:
    if cli_ledger:
        return Path(cli_ledger)
    env = os.environ.get("AFFCODE_LEDGER", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().with_name("affcode-ledger.tsv")


def fmt_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def parse_time(s: str) -> float:
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def load_ledger(path: Path):
    """返回 (rows, exists)。rows: list[dict]，坏行跳过。"""
    if not path.exists():
        return [], False
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if i == 0 and line.startswith("时间\t"):
            continue
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        rows.append({
            "time": parts[0], "user_id": parts[1], "username": parts[2],
            "old_code": parts[3], "new_code": parts[4], "note": parts[5],
        })
    return rows, True


def append_ledger(path: Path, row: dict):
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new_file:
            f.write(LEDGER_HEADER + "\n")
        f.write("\t".join([
            row["time"], str(row["user_id"]), row["username"],
            row["old_code"], row["new_code"], row["note"],
        ]) + "\n")


# ================= 业务校验 =================

def norm_code(code: str) -> str:
    """码的规范形：整体转大写。站长输入的规范化、撞码比对、台账比对、
    落库与写台账，一律用规范形——大小写变体视为同一个码。"""
    return (code or "").upper()


def effective_invitee_count(db: DB, user_id: int) -> int:
    """有效邀请数：与 settle.py 完全同口径——被邀请人累计兑换云猫码
    （status=3、redeemed_time>0、批次名以「云猫」开头）面额 >= ¥10 的人数。"""
    rows = db.execute(
        """
        SELECT used_user_id, SUM(quota) AS face
        FROM redemptions
        WHERE status = 3 AND redeemed_time > 0 AND used_user_id > 0
          AND name LIKE ?
        GROUP BY used_user_id
        """,
        (NAME_PREFIX + "%",),
    ).fetchall()
    face_of = {r["used_user_id"]: int(r["face"]) for r in rows}
    invitees = db.execute(
        "SELECT id FROM users WHERE inviter_id = ?", (user_id,)
    ).fetchall()
    return sum(1 for r in invitees
               if face_of.get(r["id"], 0) >= EFFECTIVE_THRESHOLD_QUOTA)


def run_checks(db: DB, ledger_rows, ledger_exists: bool, username: str, new_code: str,
               raw_code: str = None):
    """逐项校验，返回 (results, user_row)。
    results: list[(ok: bool, label: str, detail: str)]
    new_code 在此先规范化为大写形，后续全部校验与落库都用规范形；
    raw_code 为站长原始输入，仅用于在结果里展示规范化动作。"""
    if raw_code is None:
        raw_code = new_code
    new_code = norm_code(new_code)
    results = []

    def add(ok, label, detail):
        results.append((bool(ok), label, detail))

    # ---- 用户存在 ----
    rows = db.execute(
        "SELECT id, username, email, aff_code, status FROM users WHERE username = ?",
        (username,),
    ).fetchall()
    user = rows[0] if rows else None
    if user is None:
        add(False, "用户存在", f"找不到用户名「{username}」，核对拼写")
        return results, None
    if len(rows) > 1:
        add(False, "用户存在", f"用户名「{username}」查到 {len(rows)} 条记录，异常，先人工处理")
        return results, None
    status_note = "" if user["status"] == 1 else f"（账号状态 status={user['status']}，非正常）"
    add(True, "用户存在", f"id={user['id']}{status_note}")

    # ---- 注册邮箱（核验身份用，完整打印） ----
    email = (user["email"] or "").strip()
    if email:
        add(True, "注册邮箱", f"{email} ← 让申请人报邮箱，逐字对上才办")
    else:
        add(False, "注册邮箱", "该账号没留注册邮箱，无法核验申请人身份，按规矩不办")

    # ---- 当前码 ----
    cur_code = user["aff_code"] or ""
    add(True, "当前邀请码", cur_code or "（空）")

    # ---- 大小写规范化 ----
    if raw_code != new_code:
        add(True, "大小写规范化",
            f"输入「{raw_code}」已整体转大写为「{new_code}」——落库与台账只用大写形，"
            "大小写变体一律视为同一个码")
    else:
        add(True, "大小写规范化", "输入即大写形，无需转换（大小写变体一律视为同一个码）")

    # ---- 门槛：有效邀请满 3 ----
    eff = effective_invitee_count(db, user["id"])
    add(eff >= EFFECTIVE_REQUIRED, "申请门槛",
        f"有效邀请 {eff} 人（要求满 {EFFECTIVE_REQUIRED} 人；有效 = 被邀请人累计兑换云猫码满 ¥10，口径同结算脚本）")

    # ---- 90 天限改 1 次（查台账） ----
    last_change = None
    for r in ledger_rows:
        if r["note"] == NOTE_APPLY and str(r["user_id"]) == str(user["id"]):
            try:
                t = parse_time(r["time"])
            except ValueError:
                continue
            if last_change is None or t > last_change:
                last_change = t
    if last_change is None:
        add(True, "改码间隔", "台账里无改码记录（首次改码，不受 90 天限制）")
    else:
        days = int((time.time() - last_change) // 86400)
        add(days >= COOLDOWN_DAYS, "改码间隔",
            f"距上次改码 {days} 天（{fmt_time(last_change)}），要求满 {COOLDOWN_DAYS} 天")

    # ---- 格式 ----
    fmt_problems = []
    if not (MIN_LEN <= len(new_code) <= MAX_LEN):
        fmt_problems.append(f"长度 {len(new_code)} 位（要求 {MIN_LEN}–{MAX_LEN} 位）")
    if not CODE_RE.match(new_code):
        fmt_problems.append("含非法字符（只许字母 A–Z 和数字 0–9；小写输入已自动转大写，仍不过就是有别的字符）")
    if fmt_problems:
        add(False, "码格式", "；".join(fmt_problems))
    else:
        add(True, "码格式", f"{len(new_code)} 位、大写字母+数字，合规")

    # ---- 联系方式形态 ----
    if new_code.isdigit() and len(new_code) >= 7:
        add(False, "联系方式形态", "纯数字且长度像手机号/QQ 号，按规矩不许用联系方式当码")
    else:
        add(True, "联系方式形态", "不是纯数字联系方式形态")

    # ---- 保留词 ----
    hit_words = [w for w in RESERVED_WORDS if w in new_code]
    if hit_words:
        add(False, "保留词", f"命中保留词：{', '.join(hit_words)}（冒充官方/系统词不许用）")
    else:
        add(True, "保留词", "未命中保留词")

    # ---- 撞现存码（大小写不敏感：库层 UNIQUE 区分大小写，这里严于库层） ----
    hit = db.execute(
        "SELECT id, username, aff_code FROM users WHERE UPPER(aff_code) = ?",
        (new_code,),
    ).fetchall()
    others = [h for h in hit if h["id"] != user["id"]]
    if others:
        desc = "、".join(
            f"用户 {h['username']}（id={h['id']}，其码现存形态「{h['aff_code']}」）"
            for h in others)
        add(False, "撞现存码",
            f"大小写变体已被占用：{desc}——大小写变体视为同码，先到先得")
    elif hit:
        add(False, "撞现存码", "这就是该用户当前的码（或其大小写变体），没必要改")
    else:
        add(True, "撞现存码", "全站现存邀请码（含大小写变体）里没人用")

    # ---- 撞历史码（永不回收，查台账） ----
    if not ledger_exists:
        add(False, "永不回收（台账）",
            "台账文件不存在，无法核查历史码——先跑一次 seed 播种台账再办")
    else:
        ever_used = set()
        for r in ledger_rows:
            if r["old_code"]:
                ever_used.add(norm_code(r["old_code"]))
            if r["new_code"]:
                ever_used.add(norm_code(r["new_code"]))
        if new_code in ever_used:
            add(False, "永不回收（台账）",
                "这个码（含大小写变体）在台账里出现过（曾被使用/已退役），"
                "按规矩永久退役，不再启用")
        else:
            add(True, "永不回收（台账）", "台账历史（含退役码及大小写变体）里没出现过")

    return results, user


def print_results(results):
    for ok, label, detail in results:
        print(f"  [{'✓' if ok else '✗'}] {label}：{detail}")


def verdict(results) -> bool:
    return all(ok for ok, _, _ in results)


# ================= 子命令 =================

def cmd_check(db, lpath, username, new_code) -> int:
    rows, exists = load_ledger(lpath)
    raw_code, new_code = new_code, norm_code(new_code)
    shown = new_code if raw_code == new_code else f"{new_code}（输入「{raw_code}」已规范化为大写）"
    print(f"== 自定义邀请码校验（只查不改）｜用户 {username} ｜新码 {shown} ==")
    results, _ = run_checks(db, rows, exists, username, new_code, raw_code)
    print_results(results)
    if verdict(results):
        print("结论：可改 ✓ —— 确认无误后跑 apply 真正执行")
        return EXIT_OK
    bad = [label for ok, label, _ in results if not ok]
    print(f"结论：不可改 ✗ —— 卡在：{'、'.join(bad)}")
    return EXIT_REJECT


def cmd_apply(db, lpath, username, new_code) -> int:
    rows, exists = load_ledger(lpath)
    raw_code, new_code = new_code, norm_code(new_code)
    shown = new_code if raw_code == new_code else f"{new_code}（输入「{raw_code}」已规范化为大写）"
    print(f"== 自定义邀请码改码（先全量校验，全过才执行）｜用户 {username} ｜新码 {shown} ==")
    results, user = run_checks(db, rows, exists, username, new_code, raw_code)
    print_results(results)
    if not verdict(results):
        bad = [label for ok, label, _ in results if not ok]
        print(f"拒绝执行 ✗ —— 卡在：{'、'.join(bad)}。数据库未做任何改动。")
        return EXIT_REJECT
    old_code = user["aff_code"] or ""
    db.execute("UPDATE users SET aff_code = ? WHERE id = ?", (new_code, user["id"]))
    db.commit()
    back = db.execute("SELECT aff_code FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not back or back["aff_code"] != new_code:
        print("错误：UPDATE 后回读不一致，台账未写，请人工检查数据库", file=sys.stderr)
        return EXIT_USAGE
    append_ledger(lpath, {
        "time": fmt_time(time.time()), "user_id": user["id"],
        "username": user["username"], "old_code": old_code,
        "new_code": new_code, "note": NOTE_APPLY,
    })
    print(f"已改码 ✓ —— {username}（id={user['id']}）：{old_code} → {new_code}，台账已记")
    print("提醒申请人：邀请链接已自动变新码；旧码、旧链接立即作废，让他把物料全换掉。")
    return EXIT_OK


def cmd_seed(db, lpath) -> int:
    rows, exists = load_ledger(lpath)
    if exists and rows:
        print(f"拒绝 seed ✗ —— 台账 {lpath} 已有 {len(rows)} 条记录，重复播种会污染历史。")
        print("如确需重播种，先人工备份并移走旧台账再跑。")
        return EXIT_REJECT
    users = db.execute(
        "SELECT id, username, aff_code FROM users WHERE aff_code IS NOT NULL AND aff_code <> '' ORDER BY id"
    ).fetchall()
    now = fmt_time(time.time())
    for u in users:
        append_ledger(lpath, {
            "time": now, "user_id": u["id"], "username": u["username"],
            "old_code": "", "new_code": u["aff_code"], "note": NOTE_SEED,
        })
    print(f"seed 完成 ✓ —— 已把全站 {len(users)} 个现存邀请码写入台账 {lpath}（标「{NOTE_SEED}」）")
    print("从现在起，这些码连同日后一切退役码都受「永不回收」拦截保护。")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="自定义邀请码后台改码工具（规矩 v2）")
    ap.add_argument("--db", default=None,
                    help="SQLite 文件路径或 postgres:// DSN；省略则按 dsn.sh 同一套逻辑自动解析")
    ap.add_argument("--ledger", default=None,
                    help="台账文件路径；默认脚本同目录 affcode-ledger.tsv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "apply"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("new_code")
    sub.add_parser("seed")
    args = ap.parse_args()

    target = resolve_target(args.db)
    if not target:
        print("错误：没解析出数据库连接（--db / AFFILIATE_DSN / affiliate.env / /opt/newapi 自动解析全落空）",
              file=sys.stderr)
        return EXIT_USAGE
    print(f"数据库：{mask_target(target)}", file=sys.stderr)
    lpath = ledger_path(args.ledger)
    db = DB(target)
    try:
        if args.cmd == "check":
            return cmd_check(db, lpath, args.username, args.new_code)
        if args.cmd == "apply":
            return cmd_apply(db, lpath, args.username, args.new_code)
        return cmd_seed(db, lpath)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
