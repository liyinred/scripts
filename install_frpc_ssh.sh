#!/usr/bin/env bash
# Author: wenhao
# Example:
# curl -fSL "https://gitee.com/liyinred/scripts/raw/master/install_frpc_ssh.sh" | sudo bash -s -- -s <ip> -n <name>

set -euo pipefail

# ==============================
# 默认配置
# ==============================
SERVER_ADDR=""
PROXY_NAME=""

FRP_VERSION="0.68.1"
FRP_TGZ="frp_${FRP_VERSION}_linux_amd64.tar.gz"
FRP_DIR="frp_${FRP_VERSION}_linux_amd64"
FRP_DOWNLOAD_URL="https://gh-proxy.org/https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${FRP_TGZ}"

USERNAME="xiaohessh"
PASSWORD='R7!qZ2@p'
AUTH_TOKEN='R7!qZ2@p'

SERVICE_NAME="frp_cool"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ==============================
# 参数解析
# ==============================
show_usage() {
    cat << EOF
用法:
  sudo bash $0 --serverAddr <服务器地址> --name <代理名称>

参数:
  -s, --serverAddr    frpc.toml 中的 serverAddr
  -n, --name          frpc.toml 中 [[proxies]] 的 name，必填
  -h, --help          查看帮助

示例:
  sudo bash $0 --serverAddr <ip> --name my-ssh
  sudo bash $0 -s <ip> -n my-ssh
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--serverAddr)
            if [[ -z "${2:-}" ]]; then
                echo "错误: --serverAddr 需要提供参数值"
                exit 1
            fi
            SERVER_ADDR="$2"
            shift 2
            ;;
        -n|--name)
            if [[ -z "${2:-}" ]]; then
                echo "错误: --name 需要提供参数值"
                exit 1
            fi
            PROXY_NAME="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            echo "错误: 未知参数 $1"
            show_usage
            exit 1
            ;;
    esac
done

# ==============================
# 自动识别当前主机 SSH 端口
# ==============================
detect_ssh_port() {
    local ssh_port=""

    # 优先读取 sshd_config 中明确配置的 Port
    if [[ -f /etc/ssh/sshd_config ]]; then
        ssh_port="$(grep -Ei '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config \
            | tail -n 1 \
            | awk '{print $2}' || true)"
    fi

    # 如果 sshd_config 没有配置 Port，则从 sshd 当前监听端口识别
    if [[ -z "$ssh_port" ]] && command -v ss >/dev/null 2>&1; then
        ssh_port="$(ss -ltnp 2>/dev/null \
            | grep -E 'sshd' \
            | awk '{print $4}' \
            | sed 's/.*://' \
            | head -n 1 || true)"
    fi

    # 兼容使用 netstat 识别 sshd 监听端口
    if [[ -z "$ssh_port" ]] && command -v netstat >/dev/null 2>&1; then
        ssh_port="$(netstat -ltnp 2>/dev/null \
            | grep -E 'sshd' \
            | awk '{print $4}' \
            | sed 's/.*://' \
            | head -n 1 || true)"
    fi

    # 未识别到端口时兜底使用默认 SSH 端口
    if [[ -z "$ssh_port" ]]; then
        ssh_port="22"
    fi

    echo "$ssh_port"
}

configure_ssh_password_auth() {
    local config_file="/etc/ssh/sshd_config"
    local backup_file=""
    local sshd_bin=""

    if [[ ! -f "$config_file" ]]; then
        echo "错误: 未找到 SSH 配置文件: $config_file"
        exit 1
    fi

    backup_file="${config_file}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$config_file" "$backup_file"
    echo "已备份 SSH 配置文件: $backup_file"

    if grep -Eq '^[#[:space:]]*PasswordAuthentication[[:space:]]+' "$config_file"; then
        sed -i 's/^[#[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication yes/' "$config_file"
    else
        echo "PasswordAuthentication yes" >> "$config_file"
    fi

    sshd_bin="$(command -v sshd || true)"
    if [[ -z "$sshd_bin" && -x /usr/sbin/sshd ]]; then
        sshd_bin="/usr/sbin/sshd"
    fi

    if [[ -z "$sshd_bin" ]]; then
        echo "错误: 未找到 sshd 命令，无法校验 SSH 配置"
        exit 1
    fi

    "$sshd_bin" -t

    if systemctl restart sshd 2>/dev/null; then
        echo "已重启 sshd 服务。"
    elif systemctl restart ssh 2>/dev/null; then
        echo "已重启 ssh 服务。"
    else
        echo "错误: 无法重启 sshd 或 ssh systemd 服务"
        exit 1
    fi

    echo "已启用 SSH 密码登录并重启 SSH 服务。"
}

configure_hosts_allow() {
    local hosts_allow_file="/etc/hosts.allow"
    local required_rules=(
        "sshd: 127.0.0.1"
        "sshd: localhost"
    )
    local rule=""

    touch "$hosts_allow_file"

    for rule in "${required_rules[@]}"; do
        if ! grep -Fxq "$rule" "$hosts_allow_file"; then
            echo "$rule" >> "$hosts_allow_file"
            echo "已追加 hosts.allow 规则: $rule"
        fi
    done
}

# ==============================
# 基础校验
# ==============================
if [[ -z "$SERVER_ADDR" ]]; then
    echo "错误: serverAddr 不能为空"
    exit 1
fi

if [[ -z "$PROXY_NAME" ]]; then
    echo "错误: 必须通过 --name 或 -n 指定代理名称"
    show_usage
    exit 1
fi

if ! command -v wget >/dev/null 2>&1; then
    echo "当前系统未安装 wget，正在尝试自动安装..."

    if command -v yum >/dev/null 2>&1; then
        sudo yum install -y --nogpgcheck wget
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y wget
    else
        echo "错误: 未检测到 yum 或 apt-get，请手动安装 wget"
        exit 1
    fi

    if ! command -v wget >/dev/null 2>&1; then
        echo "错误: wget 安装失败，请手动检查包管理器软件源或网络连接"
        exit 1
    fi
fi

if ! command -v tar >/dev/null 2>&1; then
    echo "错误: 当前系统未安装 tar，请先安装 tar"
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "错误: 当前系统未检测到 systemctl，无法创建 systemd 服务"
    exit 1
fi

LOCAL_SSH_PORT="$(detect_ssh_port)"

if ! [[ "$LOCAL_SSH_PORT" =~ ^[0-9]+$ ]]; then
    echo "错误: 识别到的 SSH 端口非法: $LOCAL_SSH_PORT"
    exit 1
fi

# ==============================
# 切换工作路径到根目录
# ==============================
cd /

# ==============================
# 清理旧文件
# ==============================
echo "正在清理根目录下可能存在的同名文件和文件夹..."

if [[ -e "$FRP_TGZ" || -L "$FRP_TGZ" ]]; then
    rm -f "$FRP_TGZ"
fi

if [[ -d "$FRP_DIR" ]]; then
    rm -rf "$FRP_DIR"
fi

# ==============================
# 下载并解压 frp
# ==============================
echo "开始下载 frp..."
wget "$FRP_DOWNLOAD_URL"

echo "开始解压 frp..."
tar -xzf "$FRP_TGZ"

# ==============================
# 检查并创建登录用户
# ==============================
if id "$USERNAME" &>/dev/null; then
    echo "用户 $USERNAME 已存在，跳过创建和密码设置。"
else
    echo "用户 $USERNAME 不存在，开始创建..."
    useradd -m -s /bin/bash "$USERNAME"
    echo "${USERNAME}:${PASSWORD}" | chpasswd
    echo "用户 $USERNAME 创建成功并已设置密码。"
fi

# ==============================
# 配置用户管理权限
# ==============================
if [[ -f /etc/redhat-release ]] || grep -qi "CentOS\|Red Hat\|Rocky\|AlmaLinux" /etc/os-release 2>/dev/null; then
    usermod -aG wheel "$USERNAME"
    echo "已将用户 $USERNAME 加入 wheel 组。"
else
    if getent group sudo >/dev/null 2>&1; then
        usermod -aG sudo "$USERNAME"
        echo "已将用户 $USERNAME 加入 sudo 组。"
    else
        usermod -aG wheel "$USERNAME"
        echo "已将用户 $USERNAME 加入 wheel 组。"
    fi
fi

# ==============================
# 配置 SSH 访问
# ==============================
configure_hosts_allow
configure_ssh_password_auth

# ==============================
# 进入 frp 目录
# ==============================
cd "/$FRP_DIR"

# ==============================
# 写入 frpc.toml 配置文件
# ==============================
echo "正在配置 frpc.toml..."
echo "检测到当前主机 SSH 端口为: $LOCAL_SSH_PORT"

cat > frpc.toml << EOF
serverAddr = "$SERVER_ADDR"
serverPort = 7000
auth.method = "token"
auth.token = "$AUTH_TOKEN"

[[proxies]]
name = "$PROXY_NAME"
type = "tcp"
localIP = "127.0.0.1"
localPort = $LOCAL_SSH_PORT
remotePort = 0
EOF

# ==============================
# 清理旧 frp_cool 服务
# ==============================
echo "正在检查并清理旧的 ${SERVICE_NAME} 服务..."

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
    echo "检测到已存在 ${SERVICE_NAME} 服务，正在停止并禁用..."
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
fi

if [[ -f "$SERVICE_FILE" ]]; then
    echo "正在删除旧服务文件: $SERVICE_FILE"
    rm -f "$SERVICE_FILE"
fi

systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

# ==============================
# 新建 frp_cool systemd 服务
# ==============================
echo "正在创建新的 ${SERVICE_NAME} 服务..."

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=FRP Cool Client Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/$FRP_DIR
ExecStart=/$FRP_DIR/frpc -c /$FRP_DIR/frpc.toml
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"

# ==============================
# 启用并启动服务
# ==============================
echo "正在启用并启动 ${SERVICE_NAME} 服务..."

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# ==============================
# 完成
# ==============================
echo "=================================================="
echo "脚本执行完毕！"
echo "当前工作目录: $(pwd)"
echo "frpc.toml 配置文件已成功写入。"
echo "${SERVICE_NAME} 服务已创建并设置为开机自启。"
echo
echo "当前配置:"
echo "serverAddr = $SERVER_ADDR"
echo "name       = $PROXY_NAME"
echo "localPort  = $LOCAL_SSH_PORT"
echo
echo "服务状态查看命令:"
echo "systemctl status ${SERVICE_NAME}"
echo
echo "日志查看命令:"
echo "journalctl -u ${SERVICE_NAME} -f"
echo "=================================================="
