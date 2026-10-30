#!/usr/bin/env bash
# hajimi server security self-audit — read-only, prints a compact report.
# Run as root on the server. Changes nothing.
echo "===== SYSTEM ====="
. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME"
uname -r
uptime | sed 's/^ *//'
echo "===== LISTENING PORTS (inside view) ====="
ss -tlnp
echo "===== DOCKER (published ports) ====="
docker ps --format '{{.Names}} | {{.Ports}} | {{.Status}}'
echo "===== FIREWALL ====="
ufw status verbose 2>/dev/null | head -25
echo "===== SSHD EFFECTIVE CONFIG ====="
sshd -T 2>/dev/null | grep -Ei '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|maxauthtries|allowusers) '
echo "===== SSH FAILED LOGINS last 7d (count) ====="
journalctl -u ssh --since "7 days ago" --no-pager 2>/dev/null | grep -c "Failed password"
echo "----- top source IPs -----"
journalctl -u ssh --since "7 days ago" --no-pager 2>/dev/null | grep "Failed password" | grep -oE 'from [0-9.]+' | sort | uniq -c | sort -rn | head -5
echo "===== RECENT SUCCESSFUL LOGINS ====="
last -n 8 | head -8
echo "===== USERS WITH LOGIN SHELLS ====="
getent passwd | awk -F: '$7 ~ /(bash|sh)$/ {print $1}'
echo "===== AUTO SECURITY UPDATES ====="
systemctl is-enabled unattended-upgrades 2>/dev/null
echo "pending upgrades (count):"
apt-get -s upgrade 2>/dev/null | grep -c "^Inst"
echo "===== CRON.D ====="
ls /etc/cron.d/
echo "===== DB BACKUPS ====="
ls -lt /opt/newapi/backups/ 2>/dev/null | head -5
echo "===== DONE ====="
