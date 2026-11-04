#!/bin/sh
# 合伙人返佣结算 · 生产安装脚本（在东京服务器上以 root 运行）
#
# 做什么：
#   1. 建目录 /opt/affiliate-settle，放 settle.py
#   2. 建独立 venv 并安装 psycopg[binary]（不污染系统 Python）
#   3. 生成 affiliate.env 模板（权限 600，已存在则绝不覆盖——DSN 只存这里）
#   4. 生成 run.sh（读 env 后跑 settle.py --apply）
#   5. 写 cron：每小时整点跑一次；日志追加到 settle.log，并配 logrotate 周轮转
#
# 用法：
#   ./install.sh            # 完整安装（含 cron）——请先按 DEPLOY.md 跑完影子期再执行
#   ./install.sh --no-cron  # 只装文件与依赖，不装 cron（影子期准备阶段用）
# 幂等：可重复执行；affiliate.env 已存在时保留原内容。
set -eu

APP_DIR=/opt/affiliate-settle
CRON_FILE=/etc/cron.d/affiliate-settle
LOGROTATE_FILE=/etc/logrotate.d/affiliate-settle
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WITH_CRON=1
[ "${1:-}" = "--no-cron" ] && WITH_CRON=0

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行（需要写 /opt、/etc/cron.d、/etc/logrotate.d）" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "缺 python3，先装：apt-get install -y python3 python3-venv" >&2; exit 1; }

echo "[1/6] 建目录 $APP_DIR"
mkdir -p "$APP_DIR"

echo "[2/6] 放 settle.py"
cp "$SRC_DIR/settle.py" "$APP_DIR/settle.py"
chmod 755 "$APP_DIR/settle.py"

echo "[3/6] 建 venv 并装 psycopg[binary]（已装则跳过）"
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    echo "  缺 python3-venv，先 apt 安装"
    apt-get update -qq && apt-get install -y python3-venv
fi
if [ ! -x "$APP_DIR/venv/bin/pip" ]; then
    rm -rf "$APP_DIR/venv"
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade "psycopg[binary]"

echo "[4/6] affiliate.env（连接串配置，已存在则不动）"
if [ ! -f "$APP_DIR/affiliate.env" ]; then
    cat > "$APP_DIR/affiliate.env" <<'EOF'
# 一般留空即可：run.sh / shadow.sh 会自动从 New API 现行 SQL_DSN 解析连接串，
# 并把容器名换成 postgres 容器当前 IP。只有自动解析失效时才手填覆盖。
# 例： postgres://用户名:密码@172.x.x.x:5432/库名
AFFILIATE_DSN=
EOF
    echo "  已生成模板，请编辑填入 AFFILIATE_DSN： $APP_DIR/affiliate.env"
else
    echo "  已存在，保留原内容"
fi
chmod 600 "$APP_DIR/affiliate.env"

echo "[5/6] dsn.sh + run.sh + shadow.sh"
cat > "$APP_DIR/dsn.sh" <<'EOF'
# 连接串解析（被 run.sh / shadow.sh 引用）：
# affiliate.env 里 AFFILIATE_DSN 非空则用它；否则从 New API 现行 SQL_DSN 动态换算——
# 密码与库名直接取现行配置，把主机部分换成 postgres 容器当前 IP（容器 IP 会变，每次现查）。
if [ -f /opt/affiliate-settle/affiliate.env ]; then
    set -a; . /opt/affiliate-settle/affiliate.env; set +a
fi
if [ -z "${AFFILIATE_DSN:-}" ]; then
    RAW=$(grep -rho 'SQL_DSN=[^ "]*' /opt/newapi/ 2>/dev/null | head -1 | cut -d= -f2-)
    PGIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' newapi-postgres)
    AFFILIATE_DSN=$(printf '%s' "$RAW" | sed -E "s|@[^/]+/|@$PGIP:5432/|")
    export AFFILIATE_DSN
fi
EOF
cat > "$APP_DIR/run.sh" <<'EOF'
#!/bin/sh
. /opt/affiliate-settle/dsn.sh
exec /opt/affiliate-settle/venv/bin/python /opt/affiliate-settle/settle.py --apply
EOF
cat > "$APP_DIR/shadow.sh" <<'EOF'
#!/bin/sh
. /opt/affiliate-settle/dsn.sh
exec /opt/affiliate-settle/venv/bin/python /opt/affiliate-settle/settle.py
EOF
chmod 755 "$APP_DIR/run.sh" "$APP_DIR/shadow.sh"

echo "[6/6] cron 与日志轮转"
if [ "$WITH_CRON" = "1" ]; then
    printf '0 * * * * root /opt/affiliate-settle/run.sh >> /opt/affiliate-settle/settle.log 2>&1\n' > "$CRON_FILE"
    chmod 644 "$CRON_FILE"
    echo "  cron 已安装：每小时整点结算（$CRON_FILE）"
else
    echo "  --no-cron：跳过 cron 安装"
fi
cat > "$LOGROTATE_FILE" <<'EOF'
/opt/affiliate-settle/settle.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
echo "  logrotate 已配置：$LOGROTATE_FILE"

cat <<EOF

安装完成。手动影子验证（不发钱，连接串自动解析、不用手填）：
  $APP_DIR/shadow.sh          # 影子模式看报表
  $APP_DIR/run.sh             # 确认无误后手动真结一次（与 cron 同一入口）
日志：$APP_DIR/settle.log ；停自动发放： rm $CRON_FILE
EOF
