#!/usr/bin/env bash
# quant-trading 一键部署（在本地执行，推送到服务器并安装/重启 systemd 服务）。
#
# 用法：
#   ./deploy.sh             日常部署 = code：代码 + deploy.env → 服务端 pip + 重启 + 健康检查
#   ./deploy.sh code        同上（显式）
#   ./deploy.sh full        首次部署：code + data/ 行情数据全量同步
#   ./deploy.sh data        只推行情数据 data/（增量、断点续传）
#   ./deploy.sh pull        从服务器回流运行状态到本地（watchlog/ + data/users/ + 计划文件）
#   ./deploy.sh push-state  显式把本地运行状态推到服务器（本地改动需覆盖服务器时用）
#   ./deploy.sh status      查看服务状态与健康
#   ./deploy.sh logs        跟踪日志（./deploy.sh logs planwatch 看盯盘日志）
#   ./deploy.sh restart     重启服务（不推代码）
#   ./deploy.sh stop        停止服务
#   ./deploy.sh start       启动服务
#
# 运行状态模型（2026-10 起以服务器为准）：
#   - 服务器是运行主环境：盯盘状态(watchlog/)、用户数据(data/users/)、
#     计划文件(plan_*.json，planwatch plan set 会改它)、通知都在服务器侧持续更新；
#   - 因此默认部署**只推代码与配置**，不碰运行状态（避免本地旧快照回滚服务器新状态）；
#   - 需要本地查看/备份 → ./deploy.sh pull；
#   - 本地改了盯盘/计划要覆盖服务器 → ./deploy.sh push-state（慎用，服务器侧会丢失期间的新状态）。
set -euo pipefail

# ================= 部署配置（按需修改） =================
# 默认内网直连；外网/内网路由不通时用环境变量覆盖走网关：
#   SSH_HOST="root@quant.example.com" SSH_PORT="5029" ./deploy.sh
SSH_HOST="${SSH_HOST:-root@10.0.0.66}"
SSH_PORT="${SSH_PORT:-22}"
REMOTE_DIR="/opt/quant-trading"
APP_HOST="0.0.0.0"          # 0.0.0.0 = 公网可访问（靠自带多用户登录鉴权）
APP_PORT="8765"
# ========================================================

cd "$(dirname "$0")"
# 已知主机密钥：仓库内 .deploy/known_hosts（已固定服务器密钥）优先，
# 不存在则退回系统 ~/.ssh/known_hosts（首次连接需接受指纹）。
if [ -f .deploy/known_hosts ]; then
    KH="-o UserKnownHostsFile=$PWD/.deploy/known_hosts"
else
    KH="-o StrictHostKeyChecking=accept-new"
fi
SSH=(ssh -p "$SSH_PORT" -o ConnectTimeout=15 -o ServerAliveInterval=15 $KH "$SSH_HOST")
RSYNC=(rsync -e "ssh -p $SSH_PORT -o ServerAliveInterval=15 $KH" -az)

# 服务器/网络对连续快速建连偶发拒绝（kex reset / connection closed），自动重试
retry() {
    local n=0 max=4
    until "$@"; do
        n=$((n + 1))
        if [ "$n" -ge "$max" ]; then
            echo "!! 重试 $max 次仍失败: $*" >&2
            return 1
        fi
        echo "   （连接被重置，${n}/$((max - 1)) 次重试，5s 后…）" >&2
        sleep 5
    done
}

sync_code() {
    echo "==> 同步代码 → ${SSH_HOST}:${REMOTE_DIR}/"
    retry "${RSYNC[@]}" --stats \
        --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
        --exclude '*.pyc' --exclude '*.egg-info' \
        --exclude 'data' --exclude 'watchlog' \
        --exclude 'analyzer/output' --exclude 'examples/output' \
        --exclude '.ipynb_checkpoints' --exclude '.DS_Store' \
        --exclude 'deploy.env' \
        ./ "${SSH_HOST}:${REMOTE_DIR}/"
}

sync_data() {
    echo "==> 同步行情数据 data/（增量，断点续传）…"
    retry "${RSYNC[@]}" --partial --stats ./data/ "${SSH_HOST}:${REMOTE_DIR}/data/"
}

push_state() {
    echo "==> 推送本地运行状态 → 服务器（watchlog + 计划文件）"
    echo "   注意：服务器侧期间的盯盘/计划改动会被本地版本覆盖"
    retry "${RSYNC[@]}" --partial \
        --exclude 'output' \
        ./watchlog/ "${SSH_HOST}:${REMOTE_DIR}/watchlog/"
    retry "${RSYNC[@]}" --partial \
        plan_300687.json trading_plan_300687.md \
        "${SSH_HOST}:${REMOTE_DIR}/"
}

pull_state() {
    echo "==> 从服务器回流运行状态 → 本地（watchlog + data/users + 计划文件）"
    retry "${RSYNC[@]}" --stats "${SSH_HOST}:${REMOTE_DIR}/watchlog/" ./watchlog/
    retry "${RSYNC[@]}" --stats "${SSH_HOST}:${REMOTE_DIR}/data/users/" ./data/users/
    retry "${RSYNC[@]}" --stats \
        "${SSH_HOST}:${REMOTE_DIR}/plan_300687.json" \
        "${SSH_HOST}:${REMOTE_DIR}/trading_plan_300687.md" \
        ./
}

sync_env() {
    if [ -f deploy.env ]; then
        echo "==> 同步 deploy.env → ${REMOTE_DIR}/.env"
        retry "${RSYNC[@]}" --partial deploy.env "${SSH_HOST}:${REMOTE_DIR}/.env"
    else
        echo "==> 无本地 deploy.env（跳过；服务器首次会自动生成默认 .env）"
    fi
}

bootstrap() {
    echo "==> 服务端安装（幂等）…"
    local n=0
    while true; do
        if "${SSH[@]}" bash -s "$APP_HOST" "$APP_PORT" < deploy/server-bootstrap.sh; then
            return 0
        fi
        n=$((n + 1))
        if [ "$n" -ge 4 ]; then
            echo "!! bootstrap 重试 4 次仍失败" >&2
            return 1
        fi
        echo "   （bootstrap 失败，${n}/3 次重试，5s 后…）" >&2
        sleep 5
    done
}

health() {
    echo "==> 服务状态："
    retry "${SSH[@]}" "systemctl is-active quant-analyzer quant-planwatch; \
        echo \"HTTP 探测: \$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:${APP_PORT}/ 2>/dev/null || echo 无响应)\""
}

code_deploy() {
    sync_code
    sync_env
    bootstrap
    health
}

cmd="${1:-code}"
case "$cmd" in
    code|"")
        code_deploy
        echo
        echo "✅ 完成。访问 http://${SSH_HOST#root@}:${APP_PORT}/"
        ;;
    full)
        code_deploy
        sync_data
        echo
        echo "✅ 完成（含 data/ 行情数据同步）。"
        ;;
    data)
        sync_data
        ;;
    push-state)
        push_state
        ;;
    pull)
        pull_state
        ;;
    status)
        health
        ;;
    logs)
        exec "${SSH[@]}" "journalctl -u quant-${2:-analyzer} -f"
        ;;
    restart) exec "${SSH[@]}" "systemctl restart quant-analyzer quant-planwatch && systemctl is-active quant-analyzer quant-planwatch" ;;
    stop)    exec "${SSH[@]}" "systemctl stop quant-analyzer quant-planwatch && systemctl is-active quant-analyzer quant-planwatch || true" ;;
    start)   exec "${SSH[@]}" "systemctl start quant-analyzer quant-planwatch && systemctl is-active quant-analyzer quant-planwatch" ;;
    *)
        echo "未知命令: $cmd（可选: code/full/data/pull/push-state/status/logs [planwatch]/restart/stop/start）" >&2
        exit 2
        ;;
esac
