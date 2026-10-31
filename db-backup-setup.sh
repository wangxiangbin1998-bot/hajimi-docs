#!/usr/bin/env bash
# 哈基米数据库异地备份·一键安装（2026-10-31）
# 前置：deploy 公钥已加到仓库 hajimi-backups（助手侧完成，他会确认）。
# 本脚本做四件事：装 git（缺时）→ 克隆私有仓库 → 安装推送脚本 → 写 04:45 定时任务，
# 最后手动跑一次推送并打印结果。
set -euo pipefail
KEY=/root/.ssh/hajimi-backup-deploy
REPO=/opt/newapi/hajimi-backups
export GIT_SSH_COMMAND="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
command -v git >/dev/null || apt-get install -y git
if [ ! -d "$REPO/.git" ]; then
  git clone git@github.com:wangxiangbin1998-bot/hajimi-backups.git "$REPO"
fi
curl -s -o /opt/newapi/db-backup-push.sh https://docs.hajimi.men/db-backup-push.sh
chmod +x /opt/newapi/db-backup-push.sh
printf '45 4 * * * root /opt/newapi/db-backup-push.sh >> /opt/newapi/backups/push.log 2>&1\n' > /etc/cron.d/hajimi-backup-push
echo "== setup done, first push run: =="
/opt/newapi/db-backup-push.sh
