#!/bin/sh
# 免费池资格同步 · 开启自动运行（cron 每小时 :37 跑一次 --apply，
# 与整点返佣结算、:23 的 IP 记录器错开）。停自动：rm /etc/cron.d/free-pool
set -eu

CRON_FILE=/etc/cron.d/free-pool

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行" >&2
    exit 1
fi

printf '37 * * * * root /opt/free-pool/run.sh >> /opt/free-pool/sync.log 2>&1\n' > "$CRON_FILE"
chmod 644 "$CRON_FILE"
echo "已开启：每小时 :37 自动同步（$CRON_FILE）"
echo "日志看：tail -f /opt/free-pool/sync.log"
echo "停自动：rm $CRON_FILE"
