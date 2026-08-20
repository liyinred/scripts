#!/usr/bin/env bash
# Author: wenhao
# Example:
# command -v curl >/dev/null 2>&1 || sudo yum install -y --nogpgcheck curl && curl -fSL "https://gitee.com/liyinred/scripts/raw/master/net_traffic_monitor.sh" | sudo bash -s -- true http://<ip>:8000/api/traffic/upload
# command -v curl >/dev/null 2>&1 || sudo yum install -y --nogpgcheck curl && curl -fSL "https://gitee.com/liyinred/scripts/raw/master/net_traffic_monitor.sh" | sudo bash -s -- true http://<ip>:8000/api/traffic/compute-server-upload [ip]

set -e

# ---------- 参数校验 ----------
if [ $# -lt 2 ] || [ $# -gt 3 ]; then
    echo "Usage: $0 <traffic_api_post> <traffic_upload_url> [ip]"
    echo "Example: $0 true http://<ip>:8000/api/traffic/upload"
    echo "Example: $0 true http://<ip>:8000/api/traffic/compute-server-upload [ip]"
    exit 1
fi

TRAFFIC_API_POST="$1"
TRAFFIC_UPLOAD_URL="$2"
COMPUTE_SERVER_IP="${3:-}"

NORMALIZED_UPLOAD_URL="${TRAFFIC_UPLOAD_URL%%\?*}"
NORMALIZED_UPLOAD_URL="${NORMALIZED_UPLOAD_URL%/}"
IS_COMPUTE_SERVER_UPLOAD=false
if [[ "${NORMALIZED_UPLOAD_URL}" == */compute-server-upload ]]; then
    IS_COMPUTE_SERVER_UPLOAD=true
fi

if [ "${IS_COMPUTE_SERVER_UPLOAD}" = true ] && [ -z "${COMPUTE_SERVER_IP}" ]; then
    echo "==> No compute server IP supplied; detecting public IPv4..."
    if ! COMPUTE_SERVER_IP="$(curl -4 -fsS https://api.ip.sb/ip | tr -d '[:space:]')" || [ -z "${COMPUTE_SERVER_IP}" ]; then
        echo "Error: failed to detect public IPv4 from https://api.ip.sb/ip."
        exit 1
    fi
fi

if [ "${IS_COMPUTE_SERVER_UPLOAD}" = false ] && [ -n "${COMPUTE_SERVER_IP}" ]; then
    echo "Error: [ip] is only allowed for compute-server-upload."
    exit 1
fi

PYTHON_ARGS="--traffic_api_post ${TRAFFIC_API_POST} --traffic_upload_url ${TRAFFIC_UPLOAD_URL}"
if [ "${IS_COMPUTE_SERVER_UPLOAD}" = true ]; then
    PYTHON_ARGS="${PYTHON_ARGS} --ip ${COMPUTE_SERVER_IP}"
fi

echo "==> Parameters:"
echo "    traffic_api_post  : ${TRAFFIC_API_POST}"
echo "    traffic_upload_url: ${TRAFFIC_UPLOAD_URL}"
if [ "${IS_COMPUTE_SERVER_UPLOAD}" = true ]; then
    echo "    ip                : ${COMPUTE_SERVER_IP}"
fi
echo ""

# ---------- 1. 安装依赖 ----------
echo "==> [1/8] Checking and installing dependencies..."
command -v curl   >/dev/null 2>&1 || sudo yum install -y --nogpgcheck curl
command -v screen >/dev/null 2>&1 || sudo yum install -y --nogpgcheck screen
command -v chronyc >/dev/null 2>&1 || sudo yum install -y --nogpgcheck chrony

# ---------- 2. 清理旧的 screen 会话 ----------
echo "==> [2/8] Stopping existing traffic screen sessions..."
for s in $(screen -ls | awk '/\.traffic/{print $1}'); do
    screen -S "$s" -X quit
done

# ---------- 3. 清理旧的 systemd 服务 ----------
echo "==> [3/8] Removing existing traffic.service (if any)..."
if systemctl list-units --full --all | grep -q 'traffic.service'; then
    sudo systemctl stop    traffic
    sudo systemctl disable traffic
    sudo rm -f /etc/systemd/system/traffic.service
    sudo systemctl daemon-reload
    sudo systemctl reset-failed
fi

# ---------- 4. 设置北京时间并同步系统时间 ----------
echo "==> [4/8] Setting timezone and synchronizing system time..."
sudo timedatectl set-timezone Asia/Shanghai

# 使用国内公网 NTP，并移除旧时间源以保证重复执行结果一致。
sudo sed -i \
    -e '/^[[:space:]]*server[[:space:]]/d' \
    -e '/^[[:space:]]*pool[[:space:]]/d' \
    /etc/chrony.conf
sudo tee -a /etc/chrony.conf > /dev/null <<'EOF'
server ntp.aliyun.com iburst
server ntp1.aliyun.com iburst
server ntp2.aliyun.com iburst
server ntp3.aliyun.com iburst
EOF

sudo systemctl enable chronyd
sudo systemctl restart chronyd
sudo chronyc -a makestep

# 每秒检查一次，最多检查 5 次，要求剩余时间校正量不超过 0.5 秒。
if sudo chronyc -a waitsync 5 0.5 0 1; then
    echo "==> Time synchronization succeeded: $(date '+%Y-%m-%d %H:%M:%S %Z %z')"
else
    echo "Warning: system time synchronization failed; continuing installation."
    sudo chronyc sources -v || true
fi

# ---------- 5. 下载脚本 ----------
echo "==> [5/8] Downloading net_traffic_monitor.py..."
sudo mkdir -p /python_scripts
curl -L -o /python_scripts/net_traffic_monitor.py \
    https://gitee.com/liyinred/scripts/raw/master/net_traffic_monitor.py

# ---------- 6. 写入新的 systemd 服务文件 ----------
echo "==> [6/8] Writing /etc/systemd/system/traffic.service..."
sudo tee /etc/systemd/system/traffic.service > /dev/null <<EOF
[Unit]
Description=Net Traffic Monitor Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/python_scripts
ExecStart=/bin/bash -c 'exec \$(which python 2>/dev/null || which python3) /python_scripts/net_traffic_monitor.py ${PYTHON_ARGS}'
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ---------- 7. 启用并启动服务 ----------
echo "==> [7/8] Enabling and starting traffic.service..."
sudo systemctl daemon-reload
sudo systemctl enable traffic
sudo systemctl start  traffic

# ---------- 8. 查看服务状态 ----------
echo "==> [8/8] Service status:"
sudo systemctl status traffic --no-pager
