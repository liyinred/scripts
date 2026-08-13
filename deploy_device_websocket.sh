#!/usr/bin/env bash
# Author: wenhao
# Example:
# curl -fSL https://gitee.com/liyinred/scripts/raw/master/deploy_device_websocket.sh | sudo bash -s -- <ip>

set -euo pipefail

SERVER_URL=""

usage() {
    echo "Usage: $0 <server-url>  或  $0 --server-url <url>"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server-url)
            SERVER_URL="$2"
            shift 2
            ;;
        --server-url=*)
            SERVER_URL="${1#*=}"
            shift
            ;;
        -*)
            echo "Unknown argument: $1"
            usage
            ;;
        *)
            SERVER_URL="$1"
            shift
            ;;
    esac
done

if [[ -z "$SERVER_URL" ]]; then
    echo "Error: --server-url is required"
    usage
fi

echo "[1/6] 检查并安装依赖 (curl, screen, smartmontools, sysstat, iperf3)..."
command -v curl >/dev/null 2>&1 || sudo yum install -y --nogpgcheck curl
command -v screen >/dev/null 2>&1 || sudo yum install -y --nogpgcheck screen
command -v smartctl >/dev/null 2>&1 || sudo yum install -y --nogpgcheck smartmontools
command -v iostat >/dev/null 2>&1 || sudo yum install -y --nogpgcheck sysstat
command -v iperf3 >/dev/null 2>&1 || sudo yum install -y --nogpgcheck iperf3
echo "[1/6] 依赖检查完成"

echo "[2/6] 下载 device_websocket_agent.py 到 /python_scripts ..."
sudo mkdir -p /python_scripts
sudo curl -L -o /python_scripts/device_websocket_agent.py \
    https://gitee.com/liyinred/scripts/raw/master/device_websocket_agent.py
echo "[2/6] 下载完成"

echo "[3/6] 清理已存在的 screen 会话..."
FOUND_SESSIONS=$(screen -ls 2>/dev/null | awk '/\.websocket/{print $1}' || true)
if [[ -n "$FOUND_SESSIONS" ]]; then
    for s in $FOUND_SESSIONS; do
        echo "  -> 终止会话: $s"
        screen -S "$s" -X quit
    done
else
    echo "  -> 未发现相关 screen 会话"
fi
echo "[3/6] screen 会话清理完成"

echo "[4/6] 检查已存在的 systemd 服务..."
if systemctl list-unit-files | grep -q 'device-websocket.service'; then
    echo "  -> 发现旧服务，正在停止并移除"
    sudo systemctl stop device-websocket
    sudo systemctl disable device-websocket
    sudo rm -f /etc/systemd/system/device-websocket.service
    sudo systemctl daemon-reload
    echo "  -> 旧服务已移除"
else
    echo "  -> 未发现已存在的服务"
fi
echo "[4/6] systemd 服务检查完成"

echo "[5/6] 生成新的 systemd 服务文件 (server-url=${SERVER_URL})..."
sudo tee /etc/systemd/system/device-websocket.service > /dev/null <<EOF
[Unit]
Description=Device WebSocket Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=/python_scripts
ExecStart=/bin/bash -c "exec \$(which python || which python3) /python_scripts/device_websocket_agent.py --server-url ${SERVER_URL}"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "[5/6] 服务文件已写入 /etc/systemd/system/device-websocket.service"

echo "[6/6] 重载并启动 device-websocket 服务..."
sudo systemctl daemon-reload
sudo systemctl enable device-websocket
sudo systemctl start device-websocket
echo "[6/6] 服务启动完成，当前状态："
sudo systemctl status device-websocket --no-pager
