#!/usr/bin/env bash
# 服务端部署 bootstrap（幂等，可重复执行）。
# 由本地 deploy.sh 通过 `ssh ... bash -s <APP_HOST> <APP_PORT>` 调用，参数：
#   $1 = web 服务绑定地址（默认 0.0.0.0）
#   $2 = web 服务端口（默认 8765）
# 职责：系统依赖 → venv + pip 依赖 → .env → systemd 单元 → enable/restart → 健康检查。
set -euo pipefail

APP_HOST="${1:-0.0.0.0}"
APP_PORT="${2:-8765}"
APP_DIR="/opt/quant-trading"
VENV="$APP_DIR/.venv"

cd "$APP_DIR"

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# ---------- 1. 系统依赖 ----------
log "系统依赖"
if [ "$(id -u)" != "0" ]; then
    echo "  需要 root 权限（当前 $(id -un)）"; exit 1
fi
# 注意：`python3 -m venv --help` 在缺少 ensurepip 时也会成功，必须直接探 ensurepip
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    echo "  安装 python3-venv …"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null
fi
echo "  python $(python3 --version 2>&1)"

# ---------- 2. venv + pip 依赖 ----------
log "虚拟环境与依赖"
if [ ! -x "$VENV/bin/python" ] || [ ! -x "$VENV/bin/pip" ]; then
    # venv 残缺（如 ensurepip 缺失导致创建失败）先清掉重建
    rm -rf "$VENV"
    echo "  创建 venv …"
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
echo "  安装 requirements.txt（已满足则秒过）…"
"$VENV/bin/pip" install --quiet -r requirements.txt
echo "  依赖 OK"

# ---------- 3. .env ----------
log "环境变量 .env"
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/deploy/deploy.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "  已创建默认 .env（如需 AI/Clash 配置请编辑后重启服务）"
else
    echo "  .env 已存在，跳过"
fi

# ---------- 4. systemd 单元 ----------
log "systemd 服务"
mkdir -p "$APP_DIR/deploy"

cat > /etc/systemd/system/quant-analyzer.service <<EOF
[Unit]
Description=quant-trading analyzer dashboard (A-share, port ${APP_PORT})
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=TZ=Asia/Shanghai
EnvironmentFile=-${APP_DIR}/.env
ExecStart=${VENV}/bin/python -m analyzer serve --host ${APP_HOST} --port ${APP_PORT}
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
SyslogIdentifier=quant-analyzer

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/quant-planwatch.service <<EOF
[Unit]
Description=quant-trading planwatch (trading plan market monitor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=TZ=Asia/Shanghai
EnvironmentFile=-${APP_DIR}/.env
ExecStart=${VENV}/bin/python -m planwatch monitor
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
SyslogIdentifier=quant-planwatch

[Install]
WantedBy=multi-user.target
EOF

# 留一份副本到项目内，方便对照修改
cp /etc/systemd/system/quant-analyzer.service  "$APP_DIR/deploy/quant-analyzer.service"
cp /etc/systemd/system/quant-planwatch.service "$APP_DIR/deploy/quant-planwatch.service"
chmod 644 "$APP_DIR"/deploy/quant-*.service

systemctl daemon-reload
systemctl enable quant-analyzer quant-planwatch >/dev/null
systemctl restart quant-analyzer
systemctl restart quant-planwatch
echo "  服务已启动：quant-analyzer / quant-planwatch"

# ---------- 4b. 盘前定时拉起 planwatch ----------
# monitor 收盘(15:05)会正常退出（CLI 设计），Restart=on-failure 不会拉起它。
# 每个工作日 09:10 CST 由 cron 启动一次：盘中保持运行，收盘再退出，循环往复。
log "盘前定时任务 (cron)"
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled cron >/dev/null 2>&1; then
    cat > /etc/cron.d/quant-planwatch <<'CRON'
# quant-trading: 每个工作日 09:10 CST 拉起盯盘（收盘后 monitor 会自行退出）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
10 9 * * 1-5 root /usr/bin/systemctl start quant-planwatch >> /var/log/quant-planwatch-cron.log 2>&1
CRON
    chmod 644 /etc/cron.d/quant-planwatch
    echo "  已写入 /etc/cron.d/quant-planwatch（工作日 09:10 拉起盯盘）"
else
    echo "  [warn] 未检测到 cron，跳过盘前定时（请自行确保盯盘在交易时段运行）"
fi

# ---------- 5. 健康检查 ----------
log "健康检查"
sleep 3
for i in 1 2 3 4 5; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${APP_PORT}/" || true)
    [ "$code" = "200" ] && break
    sleep 3
done
echo "  HTTP ${APP_PORT}: ${code:-无响应}"

echo
systemctl --no-pager --lines=0 status quant-analyzer  quant-planwatch 2>&1 | grep -E '●|Active:' || true
echo
echo "部署完成。浏览器访问 http://<服务器IP>:${APP_PORT}/ （首次访问注册账户）。"
