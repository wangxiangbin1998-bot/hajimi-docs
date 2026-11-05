#!/bin/bash
# 注册 IP 记录器 · 生产安装脚本（在东京服务器上以 root 运行）
#
# 做什么：
# 1. 建目录 /opt/reg-ip-recorder，放 recorder.py
# 2. 建独立 venv 并安装 psycopg[binary]（不污染系统 Python）
# 3. 生成 config（权限 600）：安装时会在终端静默提示粘贴 Bunny 账号
#    API Key（read -s 不回显、不进命令历史）；已存在则绝不覆盖
# 4. 生成 dsn.sh（连接串自动解析，与返佣引擎同源）+ run.sh + report.sh
# 5. 配 logrotate（周轮转）
# 6. 最后自动跑一次 --report 空跑清单，供站长核对
#
# 本脚本不开启自动运行。核对清单无误后，另行运行开启命令（见文末提示或
# DEPLOY.md）：/opt/reg-ip-recorder/enable-cron.sh
#
# 分发方式（一条粘贴命令，文件托管在 hajimi-docs 仓 reg-ip-recorder/ 下）：
# curl -fsSL https://raw.githubusercontent.com/wangxiangbin1998-bot/hajimi-docs/main/reg-ip-recorder/install.sh -o /tmp/regip-install.sh && bash /tmp/regip-install.sh
# 若脚本同目录已有 recorder.py / enable-cron.sh（本地拷过去的），优先用本地的。
set -eu

APP_DIR=/opt/reg-ip-recorder
RAW_BASE=https://raw.githubusercontent.com/wangxiangbin1998-bot/hajimi-docs/main/reg-ip-recorder
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOGROTATE_FILE=/etc/logrotate.d/reg-ip-recorder

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

echo "[2/6] 放 recorder.py 与 enable-cron.sh"
fetch_or_copy recorder.py "$APP_DIR/recorder.py"
fetch_or_copy enable-cron.sh "$APP_DIR/enable-cron.sh"
chmod 755 "$APP_DIR/recorder.py" "$APP_DIR/enable-cron.sh"

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

echo "[4/6] config（Bunny 密钥，已存在则不动）"
if [ ! -f "$APP_DIR/config" ]; then
  BUNNY_KEY=""
  if ( : > /dev/tty ) 2>/dev/null; then
    printf '请粘贴 Bunny 账号 API Key（输入不回显，粘完按回车；没有就直接回车跳过）：' > /dev/tty
    read -r -s BUNNY_KEY < /dev/tty || BUNNY_KEY=""
    printf '\n' > /dev/tty
  fi
  cat > "$APP_DIR/config" <<EOF
# 注册 IP 记录器配置（权限 600，不要外传、不要贴到聊天里）
# Bunny 账号 API Key（Bunny 后台 Account → API 里复制，找不到看 DEPLOY.md）
BUNNY_API_KEY=$BUNNY_KEY
# Pull Zone ID（hajimiapi = 6896217，一般不用改）
BUNNY_PULLZONE_ID=6896217
EOF
  if [ -n "$BUNNY_KEY" ]; then
    echo "  已写入 API Key"
  else
    echo "  未填 API Key：装完后编辑 $APP_DIR/config 补上 BUNNY_API_KEY 再跑"
  fi
else
  echo "  已存在，保留原内容"
fi
chmod 600 "$APP_DIR/config"

echo "[5/6] dsn.sh + run.sh + report.sh + recorder.env"
if [ ! -f "$APP_DIR/recorder.env" ]; then
  cat > "$APP_DIR/recorder.env" <<'EOF'
# 一般留空即可：dsn.sh 会自动从 New API 现行 SQL_DSN 解析连接串，
# 并把主机换成 postgres 容器当前 IP。只有自动解析失效时才手填覆盖。
# 例： postgres://用户名:密码@172.x.x.x:5432/库名
RECORDER_DSN=
EOF
fi
chmod 600 "$APP_DIR/recorder.env"
cat > "$APP_DIR/dsn.sh" <<'EOF'
# 连接串解析（被 run.sh / report.sh 引用，与返佣引擎同一套路）：
# recorder.env 里 RECORDER_DSN 非空则用它；否则从 New API 现行 SQL_DSN
# 动态换算——密码与库名取现行配置，主机换成 postgres 容器当前 IP。
if [ -f /opt/reg-ip-recorder/recorder.env ]; then
  set -a; . /opt/reg-ip-recorder/recorder.env; set +a
fi
if [ -z "${RECORDER_DSN:-}" ]; then
  RAW=$(grep -rho 'SQL_DSN=[^ "]*' /opt/newapi/ 2>/dev/null | head -1 | cut -d= -f2-)
  PGIP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' newapi-postgres)
  RECORDER_DSN=$(printf '%s' "$RAW" | sed -E "s|@[^/]+/|@$PGIP:5432/|")
  export RECORDER_DSN
fi
EOF
cat > "$APP_DIR/run.sh" <<'EOF'
#!/bin/sh
. /opt/reg-ip-recorder/dsn.sh
exec /opt/reg-ip-recorder/venv/bin/python /opt/reg-ip-recorder/recorder.py --apply
EOF
cat > "$APP_DIR/report.sh" <<'EOF'
#!/bin/sh
. /opt/reg-ip-recorder/dsn.sh
exec /opt/reg-ip-recorder/venv/bin/python /opt/reg-ip-recorder/recorder.py --report
EOF
chmod 755 "$APP_DIR/run.sh" "$APP_DIR/report.sh"

echo "[6/6] logrotate"
cat > "$LOGROTATE_FILE" <<'EOF'
/opt/reg-ip-recorder/recorder.log {
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
if grep -q '^BUNNY_API_KEY=.\+' "$APP_DIR/config"; then
  echo "现在跑一次空跑清单（--report，不写库），请核对对人结果："
  echo "----------------------------------------------------------------"
  "$APP_DIR/report.sh" || echo "（空跑未成功：看上面报错，多半是连接串或密钥问题，修好后重跑 $APP_DIR/report.sh）"
  echo "----------------------------------------------------------------"
else
  echo "还没填 Bunny API Key：先编辑 $APP_DIR/config 填 BUNNY_API_KEY，然后跑："
  echo "  $APP_DIR/report.sh"
fi
cat <<EOF

清单核对无误后，开启每小时自动记录（二选一）：
  $APP_DIR/enable-cron.sh
或先手动真写一次再开启：
  $APP_DIR/run.sh          # --apply 真写一次
  $APP_DIR/enable-cron.sh
停自动： rm /etc/cron.d/reg-ip-recorder
日志：   $APP_DIR/recorder.log（开启 cron 后才有）；状态文件： $APP_DIR/state.json
EOF
