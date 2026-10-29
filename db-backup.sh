#!/usr/bin/env bash
# 哈基米 New API 数据库备份脚本（2026-10-28 备好，待用户在电脑前部署）
# 作用：用容器自身环境变量取 PG 用户/库名做 pg_dump，不需要知道密码；只保留最近 14 份。
# 部署（用户在服务器执行一次）：
#   1) 把本脚本放到 /opt/newapi/db-backup.sh 并 chmod +x
#   2) 先手动跑一次： /opt/newapi/db-backup.sh ，确认 /opt/newapi/backups/ 里生成 .sql.gz
#   3) 加定时任务： (crontab -l 2>/dev/null; echo '30 4 * * * /opt/newapi/db-backup.sh >> /opt/newapi/backups/backup.log 2>&1') | crontab -
set -euo pipefail
DIR=/opt/newapi/backups
mkdir -p "$DIR"
STAMP=$(date +%F-%H%M)
docker exec newapi-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$DIR/db-$STAMP.sql.gz"
ls -1t "$DIR"/db-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "backup ok: $DIR/db-$STAMP.sql.gz ($(du -h "$DIR/db-$STAMP.sql.gz" | cut -f1))"
