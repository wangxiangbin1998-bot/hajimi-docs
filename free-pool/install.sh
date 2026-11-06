#!/bin/bash
# 免费池资格同步 · 生产安装脚本（在东京服务器上以 root 运行）
#
# 做什么：
# 1. 建目录 /opt/free-pool，放 free_pool_sync.py
# 2. 建独立 venv 并安装 psycopg[binary]（不污染系统 Python）
# 3. 生成 free-pool.env（权限 600）：DSN 覆盖位，默认留空
# 4. 生成 dsn.sh（连接串自动解析，与返佣引擎同源）+ run.sh（--apply）
#    + dry.sh（--dry-run 影子跑）+ manual.sh（手工名单维护）
# 5. 配 logrotate（周轮转）
# 6. 最后自动跑一次 dry.sh 影子清单（不写库），供站长核对
#
# 本脚本不开启自动运行。核对清单无误后，另行运行开启命令（见文末提示）：
# /opt/free-pool/enable-cron.sh（cron 每小时 :37 跑 --apply）
#
# 分发方式（一条粘贴命令，文件托管在 hajimi-docs 仓 free-pool/ 下）：
# curl -fsSL https://raw.githubusercontent.com/wangxiangbin1998-bot/hajimi-docs/main/free-pool/install.sh -o /tmp/freepool-install.sh && bash /tmp/freepool-install.sh
# 若脚本同目录已有 free_pool_sync.py / enable-cron.sh（本地拷过去的），优先用本地的。
set -eu

APP_DIR=/opt/free-pool
RAW_BASE=https://raw.githubusercontent.com/wangxiangbin1998-bot/hajimi-docs/main/free-pool
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOGROTATE_FILE=/etc/logrotate.d/free-pool

if [ "$(id -u)" != "0" ]; then
  echo "请用 root 运行（需要写 /opt、/etc/logrotate.d）" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "缺 python3，先装：apt-get install -y python3 python3-venv" >&2; exit 1; }

fetch_or_copy() {
  # $1=文件名 $2=目标路径：同目录有本地副本就拷，没有就从 hajimi-docs 仓拉
  if [ -f "$SRC_DIR/$1" ]; then
    cp "$SRC_DIR/$1" "$2"
    echo "  $1（本地副本）"
  else
    curl -fsSL "$RAW_BASE/$1" -o "$2"
    echo "  $1（从 hajimi-docs 仓下载）"
  fi
}

echo "[1/6] 建目录 $APP_DIR"
mkdir -p "$APP_DIR"

echo "[2/6] 放 free_pool_sync.py 与 enable-cron.sh"
fetch_or_copy free_pool_sync.py "$APP_DIR/free_pool_sync.py"
fetch_or_copy enable-cron.sh "$APP_DIR/enable-cron.sh"
chmod 755 "$APP_DIR/free_pool_sync.py" "$APP_DIR/enable-cron.sh"

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

echo "[4/6] free-pool.env（DSN 覆盖位，已存在则不动）"
if [ ! -f "$APP_DIR/free-pool.env" ]; then
  cat > "$APP_DIR/free-pool.env" <<'EOF'
# 一般留空即可：dsn.sh 会自动从 New API 现行 SQL_DSN 解析连接串，
# 并把主机换成 postgres 容器当前 IP。只有自动解析失效时才手填覆盖。
# 例： postgres://用户名:密码@172.x.x.x:5432/库名
FREE_POOL_DSN=
EOF
  echo "  已生成（DSN 留空 = 自动解析）"
else
  echo "  已存在，保留原内容"
fi
chmod 600 "$APP_DIR/free-pool.env"

echo "[5/6] dsn.sh + run.sh + dry.sh + manual.sh"
cat > "$APP_DIR/dsn.sh" <<'EOF'
# 连接串解析（被 run.sh / dry.sh / manual.sh 引用，与返佣引擎同源逻辑）：
# free-pool.env 里 FREE_POOL_DSN 非空则用它；否则从 New API 现行 SQL_DSN
# 动态换算——密码与库名取现行配置，主机换成 postgres 容器当前 IP。
if [ -f /opt/free-pool/free-pool.env ]; then
  set -a; . /opt/free-pool/free-pool.env; set +a
fi
if [ -z "${FREE_POOL_DSN:-}" ]; then
  RAW=$(grep -rho 'SQL_DSN=[^ "]*' /opt/newapi/ 2>/dev/null | head -1 | cut -d= -f2-)
  PGIP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' newapi-postgres)
  FREE_POOL_DSN=$(printf '%s' "$RAW" | sed -E "s|@[^/]+/|@$PGIP:5432/|")
  export FREE_POOL_DSN
fi
EOF
cat > "$APP_DIR/run.sh" <<'EOF'
#!/bin/sh
. /opt/free-pool/dsn.sh
exec /opt/free-pool/venv/bin/python /opt/free-pool/free_pool_sync.py --apply
EOF
cat > "$APP_DIR/dry.sh" <<'EOF'
#!/bin/sh
. /opt/free-pool/dsn.sh
exec /opt/free-pool/venv/bin/python /opt/free-pool/free_pool_sync.py --dry-run
EOF
cat > "$APP_DIR/manual.sh" <<'EOF'
#!/bin/sh
# 手工名单维护： manual.sh list | manual.sh add <user_id> --note xx | manual.sh remove <user_id>
. /opt/free-pool/dsn.sh
exec /opt/free-pool/venv/bin/python /opt/free-pool/free_pool_sync.py manual "$@"
EOF
chmod 755 "$APP_DIR/run.sh" "$APP_DIR/dry.sh" "$APP_DIR/manual.sh"

echo "[6/6] logrotate"
cat > "$LOGROTATE_FILE" <<'EOF'
/opt/free-pool/sync.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
echo "  已配置：$LOGROTATE_FILE"

echo
echo "================ 安装完成（尚未开启自动运行） ================"
echo "现在跑一次影子清单（--dry-run，不写库），请核对将变更名单："
echo "----------------------------------------------------------------"
"$APP_DIR/dry.sh" || echo "（影子跑未成功：看上面报错，多半是连接串问题，修好后重跑 $APP_DIR/dry.sh）"
echo "----------------------------------------------------------------"
cat <<EOF

注意：按当前全站数据（有效用户共 1 人、邀请人最高 eff=1，未到满 2 人门槛），
首次影子清单 grants=0 / revokes=0 是正常的，不是脚本坏了——先靠手工名单开通。

下一步（验收，全在站长手里）：
  1. 手工给自己开通格： $APP_DIR/manual.sh add <你的站内用户ID> --note 站长验收
  2. 立即生效一趟：     $APP_DIR/run.sh
  3. 后台启用渠道 #37（sharellm），用免费池组建个令牌真调一发：
     应 200、额度分文不扣；未开通的号调应 403。
  4. 验收无误后开启每小时自动同步： $APP_DIR/enable-cron.sh
停自动： rm /etc/cron.d/free-pool
日志：   $APP_DIR/sync.log（开启 cron 后才有）
EOF
