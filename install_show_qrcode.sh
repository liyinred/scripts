#!/usr/bin/env bash
# Author: wenhao
# Example:
# curl -fSL "https://gitee.com/liyinred/scripts/raw/master/install_show_qrcode.sh" | sudo bash

set -e

SERVICE_NAME="show_qrcode"

echo "[1/5] 安装依赖..."
yum install -y epel-release >/dev/null 2>&1 || true
yum install -y qrencode openssl coreutils

echo "[2/5] 删除旧服务（如果存在）..."

systemctl stop ${SERVICE_NAME}.service >/dev/null 2>&1 || true
systemctl disable ${SERVICE_NAME}.service >/dev/null 2>&1 || true

rm -f /etc/systemd/system/${SERVICE_NAME}.service
rm -f /usr/local/bin/${SERVICE_NAME}

systemctl daemon-reload

echo "[3/5] 创建执行脚本 /usr/local/bin/show_qrcode"

cat >/usr/local/bin/show_qrcode <<'EOF'
#!/usr/bin/env bash
set -e

machine_id="$(tr -d '[:space:]' < /etc/machine-id)"

device_id="$(printf '%s' "$machine_id" \
  | openssl dgst -sha256 -binary \
  | base64 \
  | tr '+/' '-_' \
  | tr -d '=\n')"

url="https://mini.csxhcloud.com/bind?device_id=${device_id}"

echo "Device ID: $device_id"
echo "打开小合云微信小程序扫码绑定，若二维码无法正常显示，则填写 Device ID 值为节点 ID"
echo
qrencode -t UTF8 -m 2 "$url"
echo
EOF

chmod +x /usr/local/bin/show_qrcode

echo "[4/5] 创建 systemd 服务 /etc/systemd/system/show_qrcode.service"

cat >/etc/systemd/system/show_qrcode.service <<EOF
[Unit]
Description=Show QR Code CLI Service
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/show_qrcode
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

echo "[5/5] 启用并设置开机自启..."

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service

echo "安装完成"
echo "使用方式："
echo "  show_qrcode         # 直接执行"
echo "  systemctl start show_qrcode  # 通过 systemd 触发一次执行"
