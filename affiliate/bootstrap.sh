#!/bin/sh
# 合伙人返佣结算 · 一键准备（在东京服务器以 root 运行）
# 只做准备：下载 settle.py + install.sh 并以 --no-cron 方式安装（不启用定时结算）。
# 装完后按屏幕提示填 DSN、先跑影子模式核对报表，确认无误再开自动。
set -eu
BASE=https://docs.hajimi.men/affiliate
SRC=/root/affiliate-src
mkdir -p "$SRC"
curl -fsSL "$BASE/settle.py" -o "$SRC/settle.py"
curl -fsSL "$BASE/install.sh" -o "$SRC/install.sh"
chmod +x "$SRC/install.sh"
"$SRC/install.sh" --no-cron
echo ""
echo "== 准备完成（尚未开启自动结算）=="
echo "下一步："
echo "1) 编辑 /opt/affiliate-settle/affiliate.env ，把 AFFILIATE_DSN 填成 New API 生产库连接串"
echo "   （从 /opt/newapi 的 docker-compose / .env 里的 SQL_DSN 取同一个库；宿主机直连要用 127.0.0.1 加映射端口）"
echo "2) 影子跑一轮（只算账不发钱）："
echo "   set -a; . /opt/affiliate-settle/affiliate.env; set +a; /opt/affiliate-settle/venv/bin/python /opt/affiliate-settle/settle.py"
echo "3) 把影子报表发回给小王的助手核对，确认后再开自动 cron"
