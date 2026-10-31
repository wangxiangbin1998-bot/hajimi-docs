#!/usr/bin/env bash
# 哈基米数据库备份·异地推送（2026-10-31）
# 作用：把 /opt/newapi/backups 里最近 14 份 .sql.gz 同步到 GitHub 私有仓库
#       wangxiangbin1998-bot/hajimi-backups（服务器之外的异地副本）。
# 前置：仓库已用 deploy key 克隆到 /opt/newapi/hajimi-backups（由 db-backup-setup.sh 完成）。
# 定时：/etc/cron.d/hajimi-backup-push 每天 04:45 跑（在本机备份 04:30 之后）。
set -uo pipefail
SRC=/opt/newapi/backups
REPO=/opt/newapi/hajimi-backups
KEY=/root/.ssh/hajimi-backup-deploy
export GIT_SSH_COMMAND="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
cd "$REPO" || { echo "push fail: repo dir missing"; exit 1; }
ls -1t "$SRC"/db-*.sql.gz 2>/dev/null | head -14 | while read -r f; do cp -f "$f" "$REPO"/; done
ls -1t "$REPO"/db-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
git add -A
if git diff --cached --quiet; then
  echo "push skip: no changes $(date +%F\ %T)"
  exit 0
fi
git -c user.name="hajimi-backup" -c user.email="backup@hajimi.men" commit -m "db backup $(date +%F-%H%M)" >/dev/null
if git push origin HEAD >/dev/null 2>&1; then
  echo "push ok: $(date +%F\ %T)"
else
  echo "push FAIL: $(date +%F\ %T)"
  exit 1
fi
