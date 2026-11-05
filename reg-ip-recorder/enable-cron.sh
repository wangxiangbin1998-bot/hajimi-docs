#!/bin/sh
# 注册 IP 记录器 · 开启自动运行（cron 每小时 :23 跑一次 --apply，
# 与返佣引擎的整点错开）。停自动：rm /etc/cron.d/reg-ip-recorder
set -eu

CRON_FILE=/etc/cron.d/reg-ip-recorder

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行" >&2
    exit 1
fi

printf '23 * * * * root /opt/reg-ip-recorder/run.sh >> /opt/reg-ip-recorder/recorder.log 2>&1\n' > "$CRON_FILE"
chmod 644 "$CRON_FILE"
echo "已开启：每小时 :23 自动记录（$CRON_FILE）"
echo "日志看：tail -f /opt/reg-ip-recorder/recorder.log"
echo "停自动：rm $CRON_FILE"
