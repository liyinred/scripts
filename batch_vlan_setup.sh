#!/usr/bin/env bash
# Author: wenhao
# Example:
# curl -fSL https://gitee.com/liyinred/scripts/raw/master/batch_vlan_setup.sh -o /tmp/batch_vlan_setup.sh && sudo bash /tmp/batch_vlan_setup.sh

set -euo pipefail

# ============================================================
# batch_vlan_setup 命令及 systemd 服务安装器
#
# 执行本脚本：
#   1. 删除当前同名 systemd 服务
#   2. 安装 batch_vlan_setup 命令
#   3. 安装 batch_vlan_setup_show 持久化配置查看命令
#   4. 注册并启用新的 systemd 开机服务
#
# 执行 batch_vlan_setup 命令后：
#   1. 自动列出当前主机底层网卡
#   2. 用户选择父接口
#   3. 输入 VLAN 起始 / 结束 ID
#   4. 列出父接口下将被全量替换的现有 VLAN
#   5. 输入默认 IPv4 / IPv6 前缀长度
#   6. 一次性粘贴接口、IPv4、IPv6 映射
#   7. 校验映射完整性
#   8. 显示完整执行计划
#   9. 批量创建 VLAN 并绑定 IP
#  10. 保存配置供 systemd 开机重放
#
# 映射输入格式：
#
# eth0.202    117.156.130.194    2409:8774:122F::B3
# eth0.203    117.156.130.195    2409:8774:122F::B4
#
# 如果 IP 自己带前缀，则优先使用：
#
# eth0.202    117.156.130.194/29    2409:8774:122F::B3/64
#
# ============================================================


# ============================================================
# 基础检查
# ============================================================

SERVICE_NAME="batch_vlan_setup.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}"
COMMAND_PATH="/usr/local/bin/batch_vlan_setup"
SHOW_COMMAND_PATH="/usr/local/bin/batch_vlan_setup_show"
ENGINE_PATH="/usr/local/libexec/batch_vlan_setup"
APPLY_PATH="/usr/local/libexec/batch_vlan_setup_apply"


# 功能：替换同名服务并安装配置、查看命令及开机服务。
# 参数：无；读取当前脚本路径和安装路径常量。
# 返回值：安装成功时返回 0，检查或安装失败时返回非 0。
install_batch_vlan_setup_service()
{
    local script_source
    local command_temp
    local show_command_temp
    local apply_temp
    local service_temp
    local command_name

    if [[ $EUID -ne 0 ]]; then
        echo "错误：必须使用 root 权限安装。" >&2
        return 1
    fi

    for command_name in cat install mktemp readlink rm systemctl; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "错误：缺少必要命令：${command_name}" >&2
            return 1
        fi
    done

    script_source=$(readlink -f "${BASH_SOURCE[0]}") || return 1

    if [[ ! -f "$script_source" ]]; then
        echo "错误：无法读取安装脚本，请保存为文件后再执行。" >&2
        return 1
    fi

    echo "停止并移除当前同名服务：${SERVICE_NAME}..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload || return 1
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

    if [[ "$script_source" != "$ENGINE_PATH" ]]; then
        install -D -m 0755 "$script_source" "$ENGINE_PATH" || return 1
    fi

    command_temp=$(mktemp) || return 1
    show_command_temp=$(mktemp) || {
        rm -f "$command_temp"
        return 1
    }
    apply_temp=$(mktemp) || {
        rm -f "$command_temp" "$show_command_temp"
        return 1
    }
    service_temp=$(mktemp) || {
        rm -f "$command_temp" "$show_command_temp" "$apply_temp"
        return 1
    }

    if ! cat > "$command_temp" <<COMMAND_WRAPPER
#!/usr/bin/env bash
exec ${ENGINE_PATH} --configure "\$@"
COMMAND_WRAPPER
    then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! {
        cat <<'SHOW_COMMAND_HEADER'
#!/usr/bin/env bash

set -euo pipefail
SHOW_COMMAND_HEADER
        printf 'APPLY_PATH=%q\n' "$APPLY_PATH"
        cat <<'SHOW_COMMAND_BODY'

if (( $# > 0 )); then
    echo "错误：batch_vlan_setup_show 命令不接受参数。" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "错误：必须使用 root 权限查看持久化配置。" >&2
    echo "例如：sudo batch_vlan_setup_show" >&2
    exit 1
fi

if [[ ! -r "$APPLY_PATH" ]]; then
    echo "错误：无法读取持久化配置：${APPLY_PATH}" >&2
    exit 1
fi

record_count=0

while IFS=$'\t' read -r marker parent_if vlan_id vlan_if mac ipv4 ipv6 extra; do
    [[ "$marker" == "# BATCH_VLAN_CONFIG_V1" ]] || continue

    if [[ -z "$parent_if" || -z "$vlan_id" || -z "$vlan_if" ||
          -z "$mac" || -z "$ipv4" || -z "$ipv6" || -n "$extra" ]]; then
        echo "错误：持久化配置格式损坏。" >&2
        exit 1
    fi

    if (( record_count == 0 )); then
        printf "%-16s %-6s %-16s %-20s %-22s %-42s\n" \
            "父接口" "VLAN" "接口" "MAC" "IPv4" "IPv6"
        printf "%-16s %-6s %-16s %-20s %-22s %-42s\n" \
            "--------------" "----" "--------------" \
            "------------------" "--------------------" \
            "----------------------------------------"
    fi

    printf "%-16s %-6s %-16s %-20s %-22s %-42s\n" \
        "$parent_if" "$vlan_id" "$vlan_if" "$mac" "$ipv4" "$ipv6"
    ((record_count+=1))
done < "$APPLY_PATH"

if (( record_count == 0 )); then
    echo "尚无可查看的持久化 VLAN 配置。"
    echo "请先执行：sudo batch_vlan_setup"
    exit 0
fi

echo
echo "持久化 VLAN 数量：${record_count}"
SHOW_COMMAND_BODY
    } > "$show_command_temp"; then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! cat > "$apply_temp" <<'EMPTY_APPLY'
#!/usr/bin/env bash
echo "尚未配置 VLAN，请先执行：sudo batch_vlan_setup"
EMPTY_APPLY
    then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! cat > "$service_temp" <<SERVICE_UNIT
[Unit]
Description=Batch VLAN Setup Service
Wants=network-online.target
After=network-online.target
ConditionPathExists=${APPLY_PATH}

[Service]
Type=oneshot
ExecStart=${APPLY_PATH}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE_UNIT
    then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! install -D -m 0755 "$command_temp" "$COMMAND_PATH"; then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! install -D -m 0755 "$show_command_temp" "$SHOW_COMMAND_PATH"; then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if [[ ! -f "$APPLY_PATH" ]] &&
       ! install -D -m 0750 "$apply_temp" "$APPLY_PATH"; then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    if ! install -D -m 0644 "$service_temp" "$SERVICE_FILE"; then
        rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"
        return 1
    fi

    rm -f "$command_temp" "$show_command_temp" "$apply_temp" "$service_temp"

    systemctl daemon-reload || return 1
    systemctl enable "$SERVICE_NAME" || return 1
    systemctl restart "$SERVICE_NAME" || return 1

    echo
    echo "安装完成。"
    echo "配置命令：sudo batch_vlan_setup"
    echo "查看命令：sudo batch_vlan_setup_show"
    echo "服务状态：已启动并启用开机自启"
}


if [[ "${1:-}" != "--configure" ]]; then
    if (( $# > 0 )); then
        echo "错误：安装脚本不接受参数。" >&2
        exit 1
    fi

    install_batch_vlan_setup_service
    exit 0
fi

shift

if (( $# > 0 )); then
    echo "错误：batch_vlan_setup 命令不接受参数。" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "错误：必须使用 root 权限执行。"
    echo "例如：sudo batch_vlan_setup"
    exit 1
fi

for CMD in ip awk sed cut paste basename tr sort grep cat head \
           mktemp install rm systemctl; do
    if ! command -v "$CMD" >/dev/null 2>&1; then
        echo "错误：缺少必要命令：$CMD"
        exit 1
    fi
done


# ============================================================
# 获取底层网络接口
# ============================================================

declare -a PHYSICAL_IFACES=()

for NET_PATH in /sys/class/net/*; do
    IFACE=$(basename "$NET_PATH")

    [[ "$IFACE" == "lo" ]] && continue

    # PCI / USB / virtio 等直接网络设备
    if [[ -e "$NET_PATH/device" ]]; then
        PHYSICAL_IFACES+=("$IFACE")
    fi
done

if (( ${#PHYSICAL_IFACES[@]} == 0 )); then
    echo "错误：未检测到可用的底层网络接口。"
    exit 1
fi


# ============================================================
# 显示接口
# ============================================================

echo
echo "======================================================================"
echo "当前主机可用底层网络接口"
echo "======================================================================"
echo

printf "%-5s %-18s %-20s %-10s %-30s\n" \
    "编号" "接口" "MAC" "状态" "IPv4"

printf "%-5s %-18s %-20s %-10s %-30s\n" \
    "----" "----------------" "------------------" "--------" "----------------------------"

for i in "${!PHYSICAL_IFACES[@]}"; do
    IFACE="${PHYSICAL_IFACES[$i]}"

    MAC=$(cat "/sys/class/net/${IFACE}/address" 2>/dev/null || echo "-")
    STATE=$(cat "/sys/class/net/${IFACE}/operstate" 2>/dev/null || echo "unknown")

    CURRENT_IPV4=$(
        ip -4 -o addr show dev "$IFACE" scope global 2>/dev/null |
        awk '{print $4}' |
        paste -sd ',' -
    )

    [[ -z "$CURRENT_IPV4" ]] && CURRENT_IPV4="-"

    printf "%-5s %-18s %-20s %-10s %-30s\n" \
        "$((i + 1))" \
        "$IFACE" \
        "$MAC" \
        "$STATE" \
        "$CURRENT_IPV4"
done


# ============================================================
# 用户选择父接口
# ============================================================

echo

while true; do
    read -r -p \
        "请选择用于创建 VLAN 的父接口 [1-${#PHYSICAL_IFACES[@]}]: " \
        IFACE_INDEX

    if [[ ! "$IFACE_INDEX" =~ ^[0-9]+$ ]]; then
        echo "错误：请输入数字编号。"
        continue
    fi

    if (( IFACE_INDEX < 1 ||
          IFACE_INDEX > ${#PHYSICAL_IFACES[@]} )); then
        echo "错误：编号超出范围。"
        continue
    fi

    break
done

PARENT_IF="${PHYSICAL_IFACES[$((IFACE_INDEX - 1))]}"

BASE_MAC=$(
    tr '[:upper:]' '[:lower:]' \
    < "/sys/class/net/${PARENT_IF}/address"
)

if [[ ! "$BASE_MAC" =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ ]]; then
    echo "错误：无法获取 ${PARENT_IF} 的有效 MAC 地址。"
    exit 1
fi

echo
echo "已选择父接口：${PARENT_IF}"
echo "父接口 MAC   ：${BASE_MAC}"


# ============================================================
# VLAN MAC 派生
#
# 保留父接口 MAC 前四个字节，VLAN ID 编码到最后两个字节
# ============================================================

# 功能：根据父接口 MAC 和 VLAN ID 生成 VLAN 接口 MAC。
# 参数：$1 为父接口 MAC，$2 为 VLAN ID。
# 返回值：输出保留父接口前四个字节的 VLAN MAC。
generate_vlan_mac()
{
    local base_mac="$1"
    local vlan_id="$2"

    local b1 b2 b3 b4 b5 b6

    IFS=':' read -r b1 b2 b3 b4 b5 b6 <<< "$base_mac"

    local vlan_hi
    local vlan_lo

    vlan_hi=$(( (vlan_id >> 8) & 0xFF ))
    vlan_lo=$(( vlan_id & 0xFF ))

    printf "%s:%s:%s:%s:%02x:%02x" \
        "$b1" \
        "$b2" \
        "$b3" \
        "$b4" \
        "$vlan_hi" \
        "$vlan_lo"
}


# 功能：校验并规范化 IPv4 地址。
# 参数：$1 为不含 CIDR 前缀的 IPv4 地址。
# 返回值：成功时输出规范化地址并返回 0，格式无效时返回 1。
normalize_ipv4_address()
{
    local address="$1"
    local -a octets=()
    local octet
    local -a normalized_octets=()

    IFS='.' read -r -a octets <<< "$address"

    (( ${#octets[@]} == 4 )) || return 1

    for octet in "${octets[@]}"; do
        [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
        [[ ${#octet} -eq 1 || "$octet" != 0* ]] || return 1
        (( 10#$octet <= 255 )) || return 1
        normalized_octets+=("$((10#$octet))")
    done

    local IFS='.'
    printf '%s\n' "${normalized_octets[*]}"
}


# 功能：校验 IPv4 地址及 CIDR 前缀，并补充缺省前缀。
# 参数：$1 为 IPv4 地址，$2 为缺省 CIDR 前缀。
# 返回值：成功时输出规范化 CIDR 并返回 0，格式无效时返回 1。
normalize_ipv4_cidr()
{
    local value="$1"
    local default_prefix="$2"
    local address="$value"
    local prefix="$default_prefix"
    local normalized_address

    if [[ "$value" == */* ]]; then
        address="${value%/*}"
        prefix="${value##*/}"
    fi

    [[ "$prefix" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$prefix <= 32 )) || return 1

    normalized_address=$(normalize_ipv4_address "$address") || return 1
    printf '%s/%d\n' "$normalized_address" "$((10#$prefix))"
}


# 功能：校验 IPv6 地址及 CIDR 前缀，展开为统一的 8 组十六进制格式。
# 参数：$1 为 IPv6 地址，$2 为缺省 CIDR 前缀。
# 返回值：成功时输出规范化 CIDR 并返回 0，格式无效时返回 1。
normalize_ipv6_cidr()
{
    local value="$1"
    local default_prefix="$2"
    local address="$value"
    local prefix="$default_prefix"
    local ipv4_suffix normalized_ipv4
    local ipv4_a ipv4_b ipv4_c ipv4_d ipv4_high ipv4_low
    local left right remainder group normalized_group
    local missing_groups
    local -a left_groups=()
    local -a right_groups=()
    local -a address_groups=()
    local -a normalized_groups=()

    if [[ "$value" == */* ]]; then
        address="${value%/*}"
        prefix="${value##*/}"
    fi

    [[ "$prefix" =~ ^[0-9]{1,3}$ ]] || return 1
    (( 10#$prefix <= 128 )) || return 1

    address="${address,,}"
    [[ "$address" == *:* ]] || return 1
    [[ "$address" != *:::* ]] || return 1

    if [[ "$address" == *.* ]]; then
        ipv4_suffix="${address##*:}"
        normalized_ipv4=$(normalize_ipv4_address "$ipv4_suffix") || return 1
        IFS='.' read -r ipv4_a ipv4_b ipv4_c ipv4_d <<< "$normalized_ipv4"
        printf -v ipv4_high '%x' "$((ipv4_a * 256 + ipv4_b))"
        printf -v ipv4_low '%x' "$((ipv4_c * 256 + ipv4_d))"
        address="${address%:*}:${ipv4_high}:${ipv4_low}"
    fi

    [[ "$address" =~ ^[0-9a-f:]+$ ]] || return 1

    if [[ "$address" == *::* ]]; then
        remainder="${address#*::}"
        [[ "$remainder" != *::* ]] || return 1

        left="${address%%::*}"
        right="${address#*::}"

        if [[ -n "$left" ]]; then
            IFS=':' read -r -a left_groups <<< "$left"
        fi

        if [[ -n "$right" ]]; then
            IFS=':' read -r -a right_groups <<< "$right"
        fi

        missing_groups=$((8 - ${#left_groups[@]} - ${#right_groups[@]}))
        (( missing_groups >= 1 )) || return 1

        address_groups=("${left_groups[@]}")
        while (( missing_groups > 0 )); do
            address_groups+=("0")
            ((missing_groups-=1))
        done
        address_groups+=("${right_groups[@]}")
    else
        [[ "$address" != :* && "$address" != *: ]] || return 1
        IFS=':' read -r -a address_groups <<< "$address"
        (( ${#address_groups[@]} == 8 )) || return 1
    fi

    for group in "${address_groups[@]}"; do
        [[ "$group" =~ ^[0-9a-f]{1,4}$ ]] || return 1
        printf -v normalized_group '%04x' "$((16#$group))"
        normalized_groups+=("$normalized_group")
    done

    local IFS=':'
    printf '%s/%d\n' "${normalized_groups[*]}" "$((10#$prefix))"
}


# 功能：根据已确认的 VLAN 计划保存 systemd 开机重放程序。
# 参数：无；读取 PARENT_IF、VLAN 范围及地址映射等全局配置。
# 返回值：保存并验证成功时返回 0，生成或服务重启失败时返回非 0。
save_apply_configuration()
{
    local apply_temp
    local vlan_id vlan_if vlan_mac

    apply_temp=$(mktemp) || return 1

    if ! {
        cat <<'COMMAND_HEADER'
#!/usr/bin/env bash

set -euo pipefail

# 功能：获取现有 VLAN 接口的父接口。
# 参数：$1 为 VLAN 接口名称。
# 返回值：成功时输出父接口名称；无法读取时返回非 0。
get_vlan_parent()
{
    local vlan_if="$1"

    ip -o link show dev "$vlan_if" 2>/dev/null |
    awk -F': ' '{print $2}' |
    cut -d'@' -f2
}


# 功能：获取现有 VLAN 接口的 VLAN ID。
# 参数：$1 为 VLAN 接口名称。
# 返回值：成功时输出 VLAN ID；无法读取时返回非 0。
get_vlan_id()
{
    local vlan_if="$1"

    ip -d link show dev "$vlan_if" 2>/dev/null |
    sed -n 's/.*vlan protocol [^ ]* id \([0-9]\+\).*/\1/p' |
    head -n1
}


declare -a CREATED_INTERFACES=()


# 功能：删除本次执行中新建的 VLAN 接口。
# 参数：无；读取 CREATED_INTERFACES 数组。
# 返回值：始终返回 0。
rollback()
{
    local vlan_if

    for vlan_if in "${CREATED_INTERFACES[@]:-}"; do
        [[ -z "$vlan_if" ]] && continue
        ip link del dev "$vlan_if" 2>/dev/null || true
    done
}


# 功能：幂等创建或更新一个 VLAN 接口及其 IPv4、IPv6 地址。
# 参数：依次为父接口、VLAN 接口、VLAN ID、MAC、IPv4 CIDR、IPv6 CIDR。
# 返回值：配置成功时返回 0，接口冲突或命令失败时返回非 0。
ensure_vlan()
{
    local parent_if="$1"
    local vlan_if="$2"
    local vlan_id="$3"
    local vlan_mac="$4"
    local ipv4_address="$5"
    local ipv6_address="$6"
    local existing_parent existing_vlan_id

    if ip link show dev "$vlan_if" >/dev/null 2>&1; then
        existing_parent=$(get_vlan_parent "$vlan_if")
        existing_vlan_id=$(get_vlan_id "$vlan_if")

        if [[ "$existing_parent" != "$parent_if" ||
              "$existing_vlan_id" != "$vlan_id" ]]; then
            echo "错误：接口 ${vlan_if} 已存在，但不是目标 VLAN。" >&2
            return 1
        fi
    else
        ip link add \
            link "$parent_if" \
            name "$vlan_if" \
            type vlan \
            id "$vlan_id"
        CREATED_INTERFACES+=("$vlan_if")
    fi

    ip link set dev "$vlan_if" address "$vlan_mac"
    ip link set dev "$vlan_if" up
    ip -4 addr replace "$ipv4_address" dev "$vlan_if"
    ip -6 addr replace "$ipv6_address" dev "$vlan_if"

    echo "已配置 ${vlan_if}"
}


if [[ $EUID -ne 0 ]]; then
    echo "错误：必须使用 root 权限执行。" >&2
    exit 1
fi

for command_name in ip awk sed cut head; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：缺少必要命令：${command_name}" >&2
        exit 1
    fi
done

trap 'rollback' ERR
COMMAND_HEADER

        printf 'PARENT_IF=%q\n' "$PARENT_IF"

        cat <<'COMMAND_SETUP'

if ! ip link show dev "$PARENT_IF" >/dev/null 2>&1; then
    echo "错误：父接口 ${PARENT_IF} 不存在。" >&2
    exit 1
fi

ip link set dev "$PARENT_IF" up
COMMAND_SETUP

        for ((vlan_id=VLAN_START; vlan_id<=VLAN_END; vlan_id++)); do
            vlan_if="${PARENT_IF}.${vlan_id}"
            vlan_mac=$(generate_vlan_mac "$BASE_MAC" "$vlan_id")

            printf '# BATCH_VLAN_CONFIG_V1\t%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$PARENT_IF" \
                "$vlan_id" \
                "$vlan_if" \
                "$vlan_mac" \
                "${MAP_IPV4[$vlan_if]}" \
                "${MAP_IPV6[$vlan_if]}"

            printf 'ensure_vlan %q %q %q %q %q %q\n' \
                "$PARENT_IF" \
                "$vlan_if" \
                "$vlan_id" \
                "$vlan_mac" \
                "${MAP_IPV4[$vlan_if]}" \
                "${MAP_IPV6[$vlan_if]}"
        done

        cat <<'COMMAND_FOOTER'

trap - ERR
echo "全部 VLAN 配置完成。"
COMMAND_FOOTER
    } > "$apply_temp"; then
        rm -f "$apply_temp"
        return 1
    fi

    if ! install -D -m 0750 "$apply_temp" "$APPLY_PATH"; then
        rm -f "$apply_temp"
        return 1
    fi

    rm -f "$apply_temp"

    systemctl restart "$SERVICE_NAME" || return 1
}


# ============================================================
# 输入 VLAN 范围
# ============================================================

echo

while true; do
    read -r -p "请输入起始 VLAN ID [1-4094]: " VLAN_START
    read -r -p "请输入结束 VLAN ID [1-4094]: " VLAN_END

    if [[ ! "$VLAN_START" =~ ^[0-9]+$ ]] ||
       [[ ! "$VLAN_END" =~ ^[0-9]+$ ]]; then
        echo "错误：VLAN ID 必须为整数。"
        echo
        continue
    fi

    if (( VLAN_START < 1 ||
          VLAN_START > 4094 ||
          VLAN_END < 1 ||
          VLAN_END > 4094 )); then
        echo "错误：VLAN ID 必须在 1-4094 范围内。"
        echo
        continue
    fi

    if (( VLAN_START > VLAN_END )); then
        echo "错误：起始 VLAN ID 不能大于结束 VLAN ID。"
        echo
        continue
    fi

    break
done

VLAN_COUNT=$((VLAN_END - VLAN_START + 1))

echo
echo "父接口   : ${PARENT_IF}"
echo "VLAN 范围: ${VLAN_START}-${VLAN_END}"
echo "VLAN 数量: ${VLAN_COUNT}"


# ============================================================
# 检查 Linux 接口名长度
#
# IFNAMSIZ = 16，包括 \0
# 可见名称最长 15 个字符
# ============================================================

for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    if (( ${#VLAN_IF} > 15 )); then
        echo
        echo "错误：接口名超过 Linux 15 字符限制："
        echo "  ${VLAN_IF}"
        echo
        echo "请选择名称更短的父接口或修改 VLAN 接口命名方式。"
        exit 1
    fi
done


# ============================================================
# VLAN 信息函数
# ============================================================

# 功能：获取现有 VLAN 接口的父接口。
# 参数：$1 为 VLAN 接口名称。
# 返回值：成功时输出父接口名称；无法读取时返回非 0。
get_vlan_parent()
{
    local vlan_if="$1"

    ip -o link show dev "$vlan_if" 2>/dev/null |
    awk -F': ' '{print $2}' |
    cut -d'@' -f2
}

# 功能：获取现有 VLAN 接口的 VLAN ID。
# 参数：$1 为 VLAN 接口名称。
# 返回值：成功时输出 VLAN ID；无法读取时返回非 0。
get_vlan_id()
{
    local vlan_if="$1"

    ip -d link show dev "$vlan_if" 2>/dev/null |
    sed -n \
        's/.*vlan protocol [^ ]* id \([0-9]\+\).*/\1/p' |
    head -n1
}


# ============================================================
# 预检查 VLAN 冲突
# ============================================================

echo
echo "======================================================================"
echo "检查 VLAN 冲突"
echo "======================================================================"
echo

declare -a REPLACED_VLANS=()
declare -a USED_IFNAMES=()
declare -A REPLACED_VLAN_MAP=()

while IFS= read -r VLAN_IF; do
    [[ -z "$VLAN_IF" ]] && continue

    EXISTING_VLAN_ID=$(get_vlan_id "$VLAN_IF")
    [[ -z "$EXISTING_VLAN_ID" ]] && continue

    VLAN_PARENT=$(get_vlan_parent "$VLAN_IF")

    if [[ "$VLAN_PARENT" == "$PARENT_IF" ]]; then
        REPLACED_VLANS+=("${EXISTING_VLAN_ID}:${VLAN_IF}")
        REPLACED_VLAN_MAP["$VLAN_IF"]=1
    fi

done < <(
    ip -d -o link show type vlan 2>/dev/null |
    awk -F': ' '{print $2}' |
    cut -d'@' -f1
)


for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    if ip link show dev "$VLAN_IF" >/dev/null 2>&1; then
        FOUND=0

        [[ -n "${REPLACED_VLAN_MAP[$VLAN_IF]+x}" ]] && FOUND=1

        if (( FOUND == 0 )); then
            USED_IFNAMES+=("$VLAN_IF")
        fi
    fi
done


HAS_CONFLICT=0

if (( ${#REPLACED_VLANS[@]} > 0 )); then
    echo "以下现有 VLAN 将被本次配置全量替换："
    echo

    for ITEM in "${REPLACED_VLANS[@]}"; do
        VID="${ITEM%%:*}"
        VIF="${ITEM#*:}"

        echo "  VLAN ${VID} -> ${VIF}"
    done

    echo
fi

if (( ${#USED_IFNAMES[@]} > 0 )); then
    HAS_CONFLICT=1

    echo "以下接口名称已经存在："
    echo

    for VIF in "${USED_IFNAMES[@]}"; do
        echo "  ${VIF}"
    done

    echo
fi

if (( HAS_CONFLICT == 1 )); then
    echo "存在冲突。为避免覆盖现有配置，本次操作终止。"
    exit 1
fi

echo "VLAN 检查通过。"


# ============================================================
# 输入默认前缀长度
#
# 映射中如果 IP 自带 /xx，则使用自带值。
# ============================================================

echo
echo "======================================================================"
echo "设置默认 IP 前缀"
echo "======================================================================"
echo
echo "映射表中的 IP 如果不包含 CIDR，则使用这里设置的前缀。"
echo

while true; do
    read -r -p "请输入默认 IPv4 前缀长度，例如 24: " IPV4_PREFIX

    if [[ "$IPV4_PREFIX" =~ ^[0-9]+$ ]] &&
       (( IPV4_PREFIX >= 0 && IPV4_PREFIX <= 32 )); then
        break
    fi

    echo "错误：IPv4 前缀必须为 0-32。"
done

while true; do
    read -r -p "请输入默认 IPv6 前缀长度，例如 64: " IPV6_PREFIX

    if [[ "$IPV6_PREFIX" =~ ^[0-9]+$ ]] &&
       (( IPV6_PREFIX >= 0 && IPV6_PREFIX <= 128 )); then
        break
    fi

    echo "错误：IPv6 前缀必须为 0-128。"
done


# ============================================================
# 输入接口 / IPv4 / IPv6 映射
# ============================================================

echo
echo "======================================================================"
echo "请输入 VLAN IP 映射"
echo "======================================================================"
echo
echo "格式："
echo
echo "接口名    IPv4    IPv6"
echo
echo "例如："
echo
echo "${PARENT_IF}.202    117.156.130.194    2409:8774:122F::B3"
echo "${PARENT_IF}.203    117.156.130.195    2409:8774:122F::B4"
echo
echo "支持空格或 TAB 分隔。"
echo "粘贴全部内容后，再输入一个空行结束。"
echo


declare -A MAP_IPV4
declare -A MAP_IPV6
declare -A SEEN_IFACE

INPUT_COUNT=0


while IFS= read -r LINE; do

    # 空行表示输入结束
    [[ -z "${LINE//[[:space:]]/}" ]] && break

    # 忽略以 # 开头的注释
    [[ "$LINE" =~ ^[[:space:]]*# ]] && continue

    IFACE=""
    IPV4=""
    IPV6=""
    EXTRA=""

    read -r IFACE IPV4 IPV6 EXTRA <<< "$LINE"

    if [[ -z "$IFACE" || -z "$IPV4" || -z "$IPV6" ]]; then
        echo
        echo "错误：格式不正确："
        echo "  $LINE"
        echo
        echo "每行必须包含：接口名 IPv4 IPv6"
        exit 1
    fi

    if [[ -n "$EXTRA" ]]; then
        echo
        echo "错误：每行只能有 3 列："
        echo "  $LINE"
        exit 1
    fi


    # --------------------------------------------------------
    # 接口必须符合 parent.vlan_id
    # --------------------------------------------------------

    EXPECTED_PREFIX="${PARENT_IF}."

    if [[ "$IFACE" != "${EXPECTED_PREFIX}"* ]]; then
        echo
        echo "错误：接口 ${IFACE} 不属于父接口 ${PARENT_IF}。"
        exit 1
    fi

    VLAN_ID="${IFACE#${EXPECTED_PREFIX}}"

    if [[ ! "$VLAN_ID" =~ ^[0-9]+$ ]]; then
        echo
        echo "错误：无法从接口 ${IFACE} 中取得有效 VLAN ID。"
        exit 1
    fi

    if (( VLAN_ID < VLAN_START || VLAN_ID > VLAN_END )); then
        echo
        echo "错误：${IFACE} 的 VLAN ID ${VLAN_ID} 不在目标范围"
        echo "${VLAN_START}-${VLAN_END} 内。"
        exit 1
    fi


    # --------------------------------------------------------
    # 检查重复接口
    # --------------------------------------------------------

    if [[ -n "${SEEN_IFACE[$IFACE]+x}" ]]; then
        echo
        echo "错误：接口重复：${IFACE}"
        exit 1
    fi


    # --------------------------------------------------------
    # IPv4 / IPv6 格式验证并标准化
    # --------------------------------------------------------

    if ! NORMALIZED_IPV4=$(normalize_ipv4_cidr "$IPV4" "$IPV4_PREFIX"); then
        echo
        echo "错误：IPv4 地址格式无效："
        echo "  ${LINE}"
        exit 1
    fi

    if ! NORMALIZED_IPV6=$(normalize_ipv6_cidr "$IPV6" "$IPV6_PREFIX"); then
        echo
        echo "错误：IPv6 地址格式无效："
        echo "  ${LINE}"
        exit 1
    fi


    MAP_IPV4["$IFACE"]="$NORMALIZED_IPV4"
    MAP_IPV6["$IFACE"]="$NORMALIZED_IPV6"
    SEEN_IFACE["$IFACE"]=1

    ((INPUT_COUNT+=1))

done


# ============================================================
# 检查输入数量
# ============================================================

if (( INPUT_COUNT != VLAN_COUNT )); then
    echo
    echo "错误：映射表数量与 VLAN 数量不一致。"
    echo
    echo "VLAN 数量 : ${VLAN_COUNT}"
    echo "映射数量 : ${INPUT_COUNT}"
    echo
fi


# ============================================================
# 检查每个目标 VLAN 是否都有映射
# ============================================================

declare -a MISSING_IFACES=()

for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    if [[ -z "${SEEN_IFACE[$VLAN_IF]+x}" ]]; then
        MISSING_IFACES+=("$VLAN_IF")
    fi
done


if (( ${#MISSING_IFACES[@]} > 0 )); then
    echo "以下 VLAN 接口缺少 IP 映射："
    echo

    for VIF in "${MISSING_IFACES[@]}"; do
        echo "  ${VIF}"
    done

    exit 1
fi


if (( INPUT_COUNT != VLAN_COUNT )); then
    exit 1
fi


# ============================================================
# 检查 IPv4 / IPv6 是否重复
# ============================================================

declare -A USED_IPV4_MAP
declare -A USED_IPV6_MAP

for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    V4="${MAP_IPV4[$VLAN_IF]}"
    V6="${MAP_IPV6[$VLAN_IF]}"

    V4_ONLY="${V4%%/*}"
    V6_ONLY="${V6%%/*}"

    if [[ -n "${USED_IPV4_MAP[$V4_ONLY]+x}" ]]; then
        echo
        echo "错误：IPv4 地址重复：${V4_ONLY}"
        echo "  ${USED_IPV4_MAP[$V4_ONLY]}"
        echo "  ${VLAN_IF}"
        exit 1
    fi

    if [[ -n "${USED_IPV6_MAP[$V6_ONLY]+x}" ]]; then
        echo
        echo "错误：IPv6 地址重复：${V6_ONLY}"
        echo "  ${USED_IPV6_MAP[$V6_ONLY]}"
        echo "  ${VLAN_IF}"
        exit 1
    fi

    USED_IPV4_MAP["$V4_ONLY"]="$VLAN_IF"
    USED_IPV6_MAP["$V6_ONLY"]="$VLAN_IF"
done


# ============================================================
# 检查这些 IP 是否已经绑定在本机
# ============================================================

echo
echo "======================================================================"
echo "检查 IP 地址冲突"
echo "======================================================================"
echo

IP_CONFLICT=0

for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    IPV4_CIDR="${MAP_IPV4[$VLAN_IF]}"
    IPV6_CIDR="${MAP_IPV6[$VLAN_IF]}"

    IPV4_ONLY="${IPV4_CIDR%%/*}"
    IPV6_ONLY="${IPV6_CIDR%%/*}"


    while IFS= read -r EXISTING_V4_IF; do
        [[ -z "$EXISTING_V4_IF" ]] && continue
        [[ -n "${REPLACED_VLAN_MAP[$EXISTING_V4_IF]+x}" ]] && continue

        echo "IPv4 已使用：${IPV4_ONLY} -> ${EXISTING_V4_IF}"
        IP_CONFLICT=1
        break
    done < <(
        ip -4 -o addr show 2>/dev/null |
        awk -v ip="$IPV4_ONLY" '
        {
            split($4, a, "/")
            if (a[1] == ip) {
                print $2
            }
        }'
    )

    while IFS= read -r EXISTING_V6_IF; do
        [[ -z "$EXISTING_V6_IF" ]] && continue
        [[ -n "${REPLACED_VLAN_MAP[$EXISTING_V6_IF]+x}" ]] && continue

        echo "IPv6 已使用：${IPV6_ONLY} -> ${EXISTING_V6_IF}"
        IP_CONFLICT=1
        break
    done < <(
        ip -6 -o addr show 2>/dev/null |
        awk -v ip="$IPV6_ONLY" '
        {
            split($4, a, "/")
            if (tolower(a[1]) == tolower(ip)) {
                print $2
            }
        }'
    )

done


if (( IP_CONFLICT == 1 )); then
    echo
    echo "存在 IP 地址冲突，本次操作终止。"
    exit 1
fi

echo "IP 地址检查通过。"


# ============================================================
# 显示执行计划
# ============================================================

echo
echo "======================================================================"
echo "VLAN 创建及 IP 绑定计划"
echo "======================================================================"
echo

echo "替换范围：${PARENT_IF} 下的全部现有 VLAN"
echo "删除数量：${#REPLACED_VLANS[@]}"
echo

for ITEM in "${REPLACED_VLANS[@]:-}"; do
    [[ -z "$ITEM" ]] && continue
    echo "  删除 ${ITEM#*:}（VLAN ${ITEM%%:*}）"
done

if (( ${#REPLACED_VLANS[@]} > 0 )); then
    echo
fi

printf "%-6s %-16s %-20s %-22s %-42s\n" \
    "VLAN" \
    "接口" \
    "MAC" \
    "IPv4" \
    "IPv6"

printf "%-6s %-16s %-20s %-22s %-42s\n" \
    "----" \
    "--------------" \
    "------------------" \
    "--------------------" \
    "----------------------------------------"


for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do
    VLAN_IF="${PARENT_IF}.${VLAN_ID}"
    VLAN_MAC=$(generate_vlan_mac "$BASE_MAC" "$VLAN_ID")

    printf "%-6s %-16s %-20s %-22s %-42s\n" \
        "$VLAN_ID" \
        "$VLAN_IF" \
        "$VLAN_MAC" \
        "${MAP_IPV4[$VLAN_IF]}" \
        "${MAP_IPV6[$VLAN_IF]}"
done


# ============================================================
# 最终确认
# ============================================================

echo

read -r -p "确认全量替换 ${PARENT_IF} 下的 VLAN 配置？[y/N]: " CONFIRM

if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "操作取消。"
    exit 0
fi


# ============================================================
# 全量替换失败时回滚
# ============================================================

declare -a CREATED_INTERFACES=()
declare -A OLD_VLAN_PROTOCOL=()
declare -A OLD_VLAN_MAC=()
declare -A OLD_VLAN_MTU=()
declare -A OLD_VLAN_ADMIN_UP=()
declare -A OLD_VLAN_IPV4=()
declare -A OLD_VLAN_IPV6=()


# 功能：保存待替换 VLAN 的链路属性和全局 IP 地址。
# 参数：无；读取 REPLACED_VLANS 数组。
# 返回值：快照成功时返回 0，任一接口状态读取失败时返回非 0。
snapshot_replaced_vlans()
{
    local item vlan_if protocol

    for item in "${REPLACED_VLANS[@]:-}"; do
        [[ -z "$item" ]] && continue
        vlan_if="${item#*:}"

        protocol=$(
            ip -d link show dev "$vlan_if" 2>/dev/null |
            sed -n 's/.*vlan protocol \([^ ]*\) id .*/\1/p' |
            head -n1
        )
        [[ -n "$protocol" ]] || return 1

        OLD_VLAN_PROTOCOL["$vlan_if"]="$protocol"
        OLD_VLAN_MAC["$vlan_if"]=$(cat "/sys/class/net/${vlan_if}/address")
        OLD_VLAN_MTU["$vlan_if"]=$(cat "/sys/class/net/${vlan_if}/mtu")
        OLD_VLAN_ADMIN_UP["$vlan_if"]=0

        if ip -o link show dev "$vlan_if" | grep -Eq '(<|,)UP(,|>)'; then
            OLD_VLAN_ADMIN_UP["$vlan_if"]=1
        fi

        OLD_VLAN_IPV4["$vlan_if"]=$(
            ip -4 -o addr show dev "$vlan_if" scope global |
            awk '{print $4}' |
            paste -sd ',' -
        )
        OLD_VLAN_IPV6["$vlan_if"]=$(
            ip -6 -o addr show dev "$vlan_if" scope global |
            awk '{print $4}' |
            paste -sd ',' -
        )
    done
}

# 功能：删除本次执行中新建的 VLAN，并恢复被替换的旧 VLAN。
# 参数：无；读取 CREATED_INTERFACES、REPLACED_VLANS 及旧状态快照。
# 返回值：始终返回 0；恢复不完整时输出错误信息。
rollback()
{
    local vif item vlan_id address
    local restore_failed=0
    local -a addresses=()

    echo
    echo "======================================================================"
    echo "执行失败，开始回滚本次已经创建的 VLAN"
    echo "======================================================================"

    for vif in "${CREATED_INTERFACES[@]:-}"; do
        [[ -z "$vif" ]] && continue

        if ip link show dev "$vif" >/dev/null 2>&1; then
            echo "删除 ${vif}"
            ip link del dev "$vif" 2>/dev/null || true
        fi
    done

    for item in "${REPLACED_VLANS[@]:-}"; do
        [[ -z "$item" ]] && continue
        vlan_id="${item%%:*}"
        vif="${item#*:}"

        if ip link show dev "$vif" >/dev/null 2>&1; then
            continue
        fi

        echo "恢复 ${vif}"

        if ! ip link add \
            link "$PARENT_IF" \
            name "$vif" \
            type vlan \
            protocol "${OLD_VLAN_PROTOCOL[$vif]}" \
            id "$vlan_id"; then
            echo "错误：无法恢复 ${vif}。" >&2
            restore_failed=1
            continue
        fi

        ip link set dev "$vif" mtu "${OLD_VLAN_MTU[$vif]}" || restore_failed=1
        ip link set dev "$vif" address "${OLD_VLAN_MAC[$vif]}" || restore_failed=1

        IFS=',' read -r -a addresses <<< "${OLD_VLAN_IPV4[$vif]}"
        for address in "${addresses[@]:-}"; do
            [[ -z "$address" ]] && continue
            ip -4 addr add "$address" dev "$vif" || restore_failed=1
        done

        IFS=',' read -r -a addresses <<< "${OLD_VLAN_IPV6[$vif]}"
        for address in "${addresses[@]:-}"; do
            [[ -z "$address" ]] && continue
            ip -6 addr add "$address" dev "$vif" || restore_failed=1
        done

        if [[ "${OLD_VLAN_ADMIN_UP[$vif]}" == "1" ]]; then
            ip link set dev "$vif" up || restore_failed=1
        fi
    done

    echo
    if (( restore_failed == 0 )); then
        echo "回滚完成。"
    else
        echo "错误：回滚未完整恢复，请立即检查网络接口。" >&2
    fi
}


trap 'echo; echo "发生错误。"; rollback; exit 1' ERR


# ============================================================
# 创建 VLAN
# ============================================================

echo
echo "======================================================================"
echo "开始创建 VLAN 并绑定 IP"
echo "======================================================================"
echo

snapshot_replaced_vlans

for ITEM in "${REPLACED_VLANS[@]:-}"; do
    [[ -z "$ITEM" ]] && continue
    VIF="${ITEM#*:}"
    echo "删除旧 VLAN：${VIF}"
    ip link del dev "$VIF"
done

ip link set dev "$PARENT_IF" up


for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do

    VLAN_IF="${PARENT_IF}.${VLAN_ID}"
    VLAN_MAC=$(generate_vlan_mac "$BASE_MAC" "$VLAN_ID")

    IPV4_ADDR="${MAP_IPV4[$VLAN_IF]}"
    IPV6_ADDR="${MAP_IPV6[$VLAN_IF]}"


    echo "------------------------------------------------------------"
    echo "VLAN      : ${VLAN_ID}"
    echo "Interface : ${VLAN_IF}"
    echo "MAC       : ${VLAN_MAC}"
    echo "IPv4      : ${IPV4_ADDR}"
    echo "IPv6      : ${IPV6_ADDR}"


    ip link add \
        link "$PARENT_IF" \
        name "$VLAN_IF" \
        type vlan \
        id "$VLAN_ID"


    # 加入回滚列表
    CREATED_INTERFACES+=("$VLAN_IF")


    ip link set \
        dev "$VLAN_IF" \
        address "$VLAN_MAC"


    ip link set \
        dev "$VLAN_IF" \
        up


    ip -4 addr add \
        "$IPV4_ADDR" \
        dev "$VLAN_IF"


    ip -6 addr add \
        "$IPV6_ADDR" \
        dev "$VLAN_IF"


    echo "状态      : OK"
    echo
done


# 创建成功后取消错误回滚 trap
trap - ERR


# ============================================================
# 保存开机重放配置
# ============================================================

echo "======================================================================"
echo "保存 batch_vlan_setup 开机重放配置"
echo "======================================================================"
echo

if ! save_apply_configuration; then
    echo "错误：VLAN 已创建，但开机重放配置保存或验证失败。" >&2
    exit 1
fi

echo "服务名称：${SERVICE_NAME}"
echo "开机配置：已保存并验证"
echo "查看配置：sudo batch_vlan_setup_show"
echo


# ============================================================
# 最终结果
# ============================================================

echo
echo "======================================================================"
echo "全部配置完成"
echo "======================================================================"
echo

printf "%-6s %-16s %-20s %-22s %-42s\n" \
    "VLAN" \
    "接口" \
    "MAC" \
    "IPv4" \
    "IPv6"

printf "%-6s %-16s %-20s %-22s %-42s\n" \
    "----" \
    "--------------" \
    "------------------" \
    "--------------------" \
    "----------------------------------------"


for ((VLAN_ID=VLAN_START; VLAN_ID<=VLAN_END; VLAN_ID++)); do

    VLAN_IF="${PARENT_IF}.${VLAN_ID}"

    VLAN_MAC=$(
        cat "/sys/class/net/${VLAN_IF}/address" \
        2>/dev/null || echo "-"
    )

    IPV4_ACTUAL=$(
        ip -4 -o addr show \
            dev "$VLAN_IF" \
            scope global \
            2>/dev/null |
        awk '{print $4}' |
        paste -sd ',' -
    )

    IPV6_ACTUAL=$(
        ip -6 -o addr show \
            dev "$VLAN_IF" \
            scope global \
            2>/dev/null |
        awk '{print $4}' |
        paste -sd ',' -
    )

    [[ -z "$IPV4_ACTUAL" ]] && IPV4_ACTUAL="-"
    [[ -z "$IPV6_ACTUAL" ]] && IPV6_ACTUAL="-"

    printf "%-6s %-16s %-20s %-22s %-42s\n" \
        "$VLAN_ID" \
        "$VLAN_IF" \
        "$VLAN_MAC" \
        "$IPV4_ACTUAL" \
        "$IPV6_ACTUAL"

done


echo
echo "======================================================================"
echo "VLAN 创建数量：${VLAN_COUNT}"
echo "父接口        ：${PARENT_IF}"
echo "VLAN 范围     ：${VLAN_START}-${VLAN_END}"
echo "======================================================================"
