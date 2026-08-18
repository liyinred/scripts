#!/usr/bin/env bash
# Author: wenhao
# Description: 在指定时间窗口应用 TBF 出口限速（指定端口除外），并在其他时间解除限速。
# Example:
# curl -fSL "https://gitee.com/liyinred/scripts/raw/master/device_upload_limit.sh" | sudo bash -s -- -s 20 -e 22 -r 50mbit
# curl -fSL "https://gitee.com/liyinred/scripts/raw/master/device_upload_limit.sh" | sudo bash -s -- install-cron -s 20 -e 22 -r 50mbit
# curl -fSL "https://gitee.com/liyinred/scripts/raw/master/device_upload_limit.sh" | sudo bash -s -- remove-cron
# sudo bash device_upload_limit.sh off
# sudo bash device_upload_limit.sh -s 20 -e 22 -r 50mbit
# sudo bash device_upload_limit.sh install-cron -s 20 -e 22 -r 50mbit
# sudo bash device_upload_limit.sh remove-cron

# sudo crontab -l
# sudo tail -n 100 /var/log/device_upload_limit.log

set -uo pipefail

# cron 的默认 PATH 可能不包含 /usr/sbin，需确保能够找到 ip、tc 等系统命令。
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"

START=20
END=22
RATE="50mbit"
ROOT_QDISC_HANDLE="4915:"
EXEMPT_CLASS_ID="4915:1"
LIMIT_CLASS_ID="4915:2"
LIMIT_QDISC_HANDLE="4916:"
EXEMPT_PORTS=(80 443 8080 49155 49156 49159)
EXEMPT_IPV4_ADDRESSES=()
CRON_MARKER="# network-rate-limit-managed"
CRON_LOG_FILE="/var/log/device_upload_limit.log"
SCRIPT_URL="https://gitee.com/liyinred/scripts/raw/master/device_upload_limit.sh"
PERSISTENT_SCRIPT_PATH="/usr/local/sbin/device_upload_limit.sh"

# 功能：检查脚本运行所需权限和命令。
# 参数：无。
# 返回值：检查通过时返回 0，否则返回 1。
check_requirements() {
    local command_name

    if [ "${EUID}" -ne 0 ]; then
        echo "Error: this script must be run as root" >&2
        return 1
    fi

    for command_name in ip tc awk grep date; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Error: required command not found: $command_name" >&2
            return 1
        fi
    done
}

# 功能：校验限速时间窗口和限速速率参数。
# 参数：使用全局变量 START、END 和 RATE。
# 返回值：参数有效时返回 0，否则返回 1。
validate_parameters() {
    if ! [[ "$START" =~ ^[0-9]+$ ]] || ! [[ "$END" =~ ^[0-9]+$ ]]; then
        echo "Error: START and END must be integers from 0 to 23" >&2
        return 1
    fi

    if [ "$START" -gt 23 ] || [ "$END" -gt 23 ]; then
        echo "Error: START and END must be integers from 0 to 23" >&2
        return 1
    fi

    if [ "$START" -eq "$END" ]; then
        echo "Error: START and END cannot be the same" >&2
        return 1
    fi

    if [ -z "$RATE" ]; then
        echo "Error: RATE cannot be empty" >&2
        return 1
    fi
}

# 功能：解析限速时间窗口和限速速率参数。
# 参数：接收 -s/--start、-e/--end 和 -r/--rate 参数。
# 返回值：参数解析成功时返回 0，未知参数或参数缺值时返回 1。
parse_schedule_parameters() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -s|--start)
                if [ "$#" -lt 2 ]; then
                    echo "Error: $1 requires a value" >&2
                    return 1
                fi
                START="$2"
                shift 2
                ;;
            -e|--end)
                if [ "$#" -lt 2 ]; then
                    echo "Error: $1 requires a value" >&2
                    return 1
                fi
                END="$2"
                shift 2
                ;;
            -r|--rate)
                if [ "$#" -lt 2 ]; then
                    echo "Error: $1 requires a value" >&2
                    return 1
                fi
                RATE="$2"
                shift 2
                ;;
            *)
                echo "Error: unknown option: $1" >&2
                return 1
                ;;
        esac
    done
}

# 功能：检查管理 cron 任务所需的权限和命令。
# 参数：无。
# 返回值：检查通过时返回 0，否则返回 1。
check_cron_requirements() {
    local command_name

    if [ "${EUID}" -ne 0 ]; then
        echo "Error: cron tasks must be managed as root" >&2
        return 1
    fi

    for command_name in crontab awk grep; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Error: required command not found: $command_name" >&2
            return 1
        fi
    done

    if [ ! -x /bin/bash ]; then
        echo "Error: required executable not found: /bin/bash" >&2
        return 1
    fi
}

# 功能：下载并校验远程脚本，将其安装到 cron 可持续访问的本地路径。
# 参数：使用全局变量 SCRIPT_URL 和 PERSISTENT_SCRIPT_PATH。
# 返回值：安装成功时通过标准输出返回本地脚本路径，否则返回 1。
install_remote_script() {
    local command_name
    local temporary_script
    local staged_script="${PERSISTENT_SCRIPT_PATH}.tmp.$$"

    for command_name in curl mktemp install mv rm; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Error: required command not found: $command_name" >&2
            return 1
        fi
    done

    if ! temporary_script=$(mktemp /tmp/device_upload_limit.XXXXXX); then
        echo "Error: failed to create temporary script file" >&2
        return 1
    fi

    if ! curl -fSL "$SCRIPT_URL" -o "$temporary_script"; then
        echo "Error: failed to download remote script: $SCRIPT_URL" >&2
        rm -f "$temporary_script"
        return 1
    fi

    if ! /bin/bash -n "$temporary_script"; then
        echo "Error: downloaded script failed Bash syntax validation" >&2
        rm -f "$temporary_script"
        return 1
    fi

    if ! install -D -m 0750 "$temporary_script" "$staged_script"; then
        echo "Error: failed to stage script: $staged_script" >&2
        rm -f "$temporary_script" "$staged_script"
        return 1
    fi

    if ! mv -f "$staged_script" "$PERSISTENT_SCRIPT_PATH"; then
        echo "Error: failed to install script: $PERSISTENT_SCRIPT_PATH" >&2
        rm -f "$temporary_script" "$staged_script"
        return 1
    fi

    rm -f "$temporary_script"
    printf '%s\n' "$PERSISTENT_SCRIPT_PATH"
}

# 功能：获取当前本地脚本的绝对路径；远程管道执行时先安装持久副本。
# 参数：无。
# 返回值：通过标准输出返回 cron 后续执行使用的本地脚本绝对路径。
get_script_path() {
    local script_path="${BASH_SOURCE[0]:-}"
    local script_directory
    local script_name

    if [ -z "$script_path" ] || [ ! -f "$script_path" ]; then
        install_remote_script
        return
    fi

    script_directory="${script_path%/*}"
    script_name="${script_path##*/}"
    if [ "$script_directory" = "$script_path" ]; then
        script_directory="."
    fi

    if ! script_directory=$(cd "$script_directory" && pwd -P); then
        echo "Error: failed to resolve script directory" >&2
        return 1
    fi

    printf '%s/%s\n' "$script_directory" "$script_name"
}

# 功能：读取 root 用户当前的 crontab 内容。
# 参数：无。
# 返回值：存在 crontab 时通过标准输出返回内容；未创建时返回空内容和状态 0。
read_current_crontab() {
    local crontab_content
    local crontab_status

    crontab_content=$(crontab -l 2>/dev/null)
    crontab_status=$?
    if [ "$crontab_status" -eq 0 ]; then
        printf '%s\n' "$crontab_content"
        return 0
    fi

    if [ "$crontab_status" -eq 1 ]; then
        return 0
    fi

    echo "Error: failed to read current crontab" >&2
    return "$crontab_status"
}

# 功能：创建或更新每分钟运行一次的限速检测 cron 任务。
# 参数：使用全局变量 START、END、RATE、CRON_MARKER 和 CRON_LOG_FILE。
# 返回值：cron 任务安装成功时返回 0，否则返回 1。
install_cron_task() {
    local script_path
    local quoted_script_path
    local quoted_rate
    local current_crontab
    local filtered_crontab
    local cron_entry

    if ! script_path=$(get_script_path); then
        return 1
    fi

    printf -v quoted_script_path '%q' "$script_path"
    printf -v quoted_rate '%q' "$RATE"
    quoted_script_path="${quoted_script_path//%/\\%}"
    quoted_rate="${quoted_rate//%/\\%}"

    if ! current_crontab=$(read_current_crontab); then
        return 1
    fi
    filtered_crontab=$(printf '%s\n' "$current_crontab" \
        | awk -v marker="$CRON_MARKER" 'index($0, marker) == 0')

    cron_entry="* * * * * /bin/bash $quoted_script_path -s $START -e $END -r $quoted_rate >> $CRON_LOG_FILE 2>&1 $CRON_MARKER"
    if ! {
        [ -n "$filtered_crontab" ] && printf '%s\n' "$filtered_crontab"
        printf '%s\n' "$cron_entry"
    } | crontab -; then
        echo "Error: failed to install cron task" >&2
        return 1
    fi

    echo "Cron task installed: $cron_entry"
}

# 功能：删除由本脚本创建的限速检测 cron 任务。
# 参数：使用全局变量 CRON_MARKER 识别任务。
# 返回值：任务删除成功或任务不存在时返回 0，crontab 操作失败时返回 1。
remove_cron_task() {
    local current_crontab
    local filtered_crontab

    if ! current_crontab=$(read_current_crontab); then
        return 1
    fi

    if ! grep -Fq "$CRON_MARKER" <<< "$current_crontab"; then
        echo "Cron task not found"
        return 0
    fi

    filtered_crontab=$(printf '%s\n' "$current_crontab" \
        | awk -v marker="$CRON_MARKER" 'index($0, marker) == 0')
    if ! printf '%s\n' "$filtered_crontab" | crontab -; then
        echo "Error: failed to remove cron task" >&2
        return 1
    fi

    echo "Cron task removed"
}

# 功能：获取需要进行出口限速管理的网络接口。
# 参数：无。
# 返回值：通过标准输出逐行返回接口名称，并排除常见回环、虚拟化和容器接口。
get_interfaces() {
    ip -o link show | awk -F': ' '
        {
            interface_name = $2
            sub(/@.*/, "", interface_name)
            if (interface_name !~ /^(lo|docker0|virbr|br-|veth|cni)/) {
                print interface_name
            }
        }
    '
}

# 功能：删除指定接口上由本脚本创建的出口限速规则，并兼容清理旧版 TBF 根队列。
# 参数：$1 为网络接口名称；$2 为 true 时不输出成功日志，默认 false。
# 返回值：成功或接口未配置本脚本限速规则时返回 0，删除失败时返回 1。
remove_interface_limit() {
    local interface_name="$1"
    local quiet="${2:-false}"
    local qdisc_info

    if ! qdisc_info=$(tc qdisc show dev "$interface_name"); then
        echo "Error: failed to inspect qdisc on $interface_name" >&2
        return 1
    fi

    if ! grep -Eq '^qdisc tbf .* root( |$)' <<< "$qdisc_info" \
        && ! grep -Fq "qdisc prio $ROOT_QDISC_HANDLE root" <<< "$qdisc_info"; then
        if [ "$quiet" != true ]; then
            echo "$(date '+%F %T') $interface_name has no managed limit"
        fi
        return 0
    fi

    if ! tc qdisc del dev "$interface_name" root; then
        echo "Error: failed to remove limit from $interface_name" >&2
        return 1
    fi
    if [ "$quiet" != true ]; then
        echo "$(date '+%F %T') $interface_name limit removed"
    fi
}

# 功能：检查指定接口上的出口限速规则是否与当前配置完全一致。
# 参数：$1 为网络接口名称；限速值、豁免 IP 和豁免端口从全局变量读取。
# 返回值：根队列、TBF 速率以及全部 IP 和端口豁免规则一致时返回 0，否则返回 1。
is_interface_limit_current() {
    local interface_name="$1"
    local qdisc_info
    local filter_info
    local ip_address
    local protocol
    local transport_protocol
    local port
    local filter_priority=10

    if ! qdisc_info=$(tc -details qdisc show dev "$interface_name"); then
        return 1
    fi

    if ! grep -Fq "qdisc prio $ROOT_QDISC_HANDLE root" <<< "$qdisc_info" \
        || ! awk -v handle="$LIMIT_QDISC_HANDLE" -v parent="$LIMIT_CLASS_ID" -v rate="$RATE" '
            $1 == "qdisc" && $2 == "tbf" && $3 == handle {
                matched_parent = 0
                matched_rate = 0
                for (field_index = 4; field_index <= NF; field_index++) {
                    if ($field_index == "parent" && $(field_index + 1) == parent) {
                        matched_parent = 1
                    }
                    if ($field_index == "rate" && tolower($(field_index + 1)) == tolower(rate)) {
                        matched_rate = 1
                    }
                }
                if (matched_parent && matched_rate) {
                    found = 1
                }
            }
            END { exit(found ? 0 : 1) }
        ' <<< "$qdisc_info"; then
        return 1
    fi

    for ip_address in "${EXEMPT_IPV4_ADDRESSES[@]+"${EXEMPT_IPV4_ADDRESSES[@]}"}"; do
        if ! filter_info=$(tc filter show dev "$interface_name" parent "$ROOT_QDISC_HANDLE" \
            protocol ip priority "$filter_priority") \
            || ! grep -Eq "dst_ip $ip_address(/32)?([[:space:]]|$)" <<< "$filter_info" \
            || ! grep -Eq "classid $EXEMPT_CLASS_ID([[:space:]]|$)" <<< "$filter_info"; then
            return 1
        fi
        filter_priority=$((filter_priority + 1))
    done

    for protocol in ip ipv6; do
        for transport_protocol in tcp udp; do
            for port in "${EXEMPT_PORTS[@]}"; do
                if ! filter_info=$(tc filter show dev "$interface_name" parent "$ROOT_QDISC_HANDLE" \
                    protocol "$protocol" priority "$filter_priority") \
                    || ! grep -Eq "ip_proto $transport_protocol([[:space:]]|$)" <<< "$filter_info" \
                    || ! grep -Eq "src_port $port([[:space:]]|$)" <<< "$filter_info" \
                    || ! grep -Eq "classid $EXEMPT_CLASS_ID([[:space:]]|$)" <<< "$filter_info"; then
                    return 1
                fi
                filter_priority=$((filter_priority + 1))
            done
        done
    done
}

# 功能：解除所有目标接口上的 TBF 出口限速。
# 参数：无。
# 返回值：全部接口处理成功时返回 0，任一接口失败或接口列表读取失败时返回 1。
limit_off() {
    local -a interfaces=()
    local interface_output
    local interface_name
    local failed=0

    if ! interface_output=$(get_interfaces); then
        echo "Error: failed to get network interfaces" >&2
        return 1
    fi

    if [ -z "$interface_output" ]; then
        echo "Error: no eligible network interfaces found" >&2
        return 1
    fi
    mapfile -t interfaces <<< "$interface_output"

    for interface_name in "${interfaces[@]}"; do
        if ! remove_interface_limit "$interface_name"; then
            failed=1
        fi
    done

    return "$failed"
}

# 功能：在指定接口上按目标 IP 和源端口分类出口流量，命中豁免规则时不限速。
# 参数：$1 为网络接口名称；限速值、豁免 IP 和豁免端口从全局变量读取。
# 返回值：应用限速成功时返回 0，否则返回 1。
apply_interface_limit() {
    local interface_name="$1"
    local ip_address
    local protocol
    local transport_protocol
    local port
    local filter_priority=10
    local exempt_ipv4_address_count=0

    if ! ip link show "$interface_name" >/dev/null 2>&1; then
        echo "Error: network interface not found: $interface_name" >&2
        return 1
    fi

    if is_interface_limit_current "$interface_name"; then
        echo "$(date '+%F %T') $interface_name limit unchanged: $RATE"
        return 0
    fi

    if ! remove_interface_limit "$interface_name" true; then
        echo "Error: failed to reset managed limit on $interface_name" >&2
        return 1
    fi

    if ! tc qdisc replace dev "$interface_name" root handle "$ROOT_QDISC_HANDLE" prio \
        bands 2 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1; then
        echo "Error: failed to create traffic classes on $interface_name" >&2
        return 1
    fi

    if ! tc qdisc replace dev "$interface_name" parent "$LIMIT_CLASS_ID" \
        handle "$LIMIT_QDISC_HANDLE" tbf \
        rate "$RATE" \
        burst 512kbit \
        latency 50ms; then
        echo "Error: failed to limit $interface_name to $RATE" >&2
        return 1
    fi

    # 上传流量的远端地址是 destination IP，命中后直接进入不限速 band。
    for ip_address in "${EXEMPT_IPV4_ADDRESSES[@]+"${EXEMPT_IPV4_ADDRESSES[@]}"}"; do
        if ! tc filter replace dev "$interface_name" parent "$ROOT_QDISC_HANDLE" \
            protocol ip priority "$filter_priority" flower \
            dst_ip "$ip_address/32" classid "$EXEMPT_CLASS_ID"; then
            echo "Error: failed to exempt destination IPv4 address $ip_address on $interface_name" >&2
            return 1
        fi
        filter_priority=$((filter_priority + 1))
        exempt_ipv4_address_count=$((exempt_ipv4_address_count + 1))
    done

    # 服务响应从服务端口发出，因此出口流量按 TCP/UDP source port 匹配。
    for protocol in ip ipv6; do
        for transport_protocol in tcp udp; do
            for port in "${EXEMPT_PORTS[@]}"; do
                if ! tc filter replace dev "$interface_name" parent "$ROOT_QDISC_HANDLE" \
                    protocol "$protocol" priority "$filter_priority" flower \
                    ip_proto "$transport_protocol" src_port "$port" classid "$EXEMPT_CLASS_ID"; then
                    echo "Error: failed to exempt $transport_protocol source port $port ($protocol) on $interface_name" >&2
                    return 1
                fi
                filter_priority=$((filter_priority + 1))
            done
        done
    done

    echo "$(date '+%F %T') $interface_name limit refreshed: $RATE; $exempt_ipv4_address_count destination IPv4 addresses and TCP/UDP source ports ${EXEMPT_PORTS[*]} exempted"
}

# 功能：为所有目标接口应用 TBF 出口限速。
# 参数：无。
# 返回值：全部接口处理成功时返回 0，任一接口失败或接口列表读取失败时返回 1。
limit_on() {
    local -a interfaces=()
    local interface_output
    local interface_name
    local failed=0

    if ! interface_output=$(get_interfaces); then
        echo "Error: failed to get network interfaces" >&2
        return 1
    fi

    if [ -z "$interface_output" ]; then
        echo "Error: no eligible network interfaces found" >&2
        return 1
    fi
    mapfile -t interfaces <<< "$interface_output"

    for interface_name in "${interfaces[@]}"; do
        if ! apply_interface_limit "$interface_name"; then
            failed=1
        fi
    done

    return "$failed"
}

# 功能：判断给定小时是否处于限速时间窗口，支持跨午夜窗口。
# 参数：$1 为 0-23 的当前小时；时间窗口从全局变量 START、END 读取。
# 返回值：处于限速窗口时返回 0，否则返回 1。
is_limited_hour() {
    local current_hour="$1"

    if [ "$START" -lt "$END" ]; then
        [ "$current_hour" -ge "$START" ] && [ "$current_hour" -lt "$END" ]
    else
        [ "$current_hour" -ge "$START" ] || [ "$current_hour" -lt "$END" ]
    fi
}

# 功能：管理 cron 任务，或根据当前时间开启或解除出口限速。
# 参数：接收脚本的全部命令行参数。
# 返回值：操作成功时返回 0，参数、环境、cron 或 tc 操作失败时返回非 0。
main() {
    local current_hour

    if [ "${1:-}" = "off" ]; then
        if [ "$#" -ne 1 ]; then
            echo "Error: off does not accept additional arguments" >&2
            return 1
        fi
        check_requirements && limit_off
        return
    fi

    if [ "${1:-}" = "remove-cron" ]; then
        if [ "$#" -ne 1 ]; then
            echo "Error: remove-cron does not accept additional arguments" >&2
            return 1
        fi
        check_cron_requirements || return 1
        check_requirements || return 1
        remove_cron_task && limit_off
        return
    fi

    if [ "${1:-}" = "install-cron" ]; then
        shift
        parse_schedule_parameters "$@" || return 1
        validate_parameters || return 1
        check_cron_requirements || return 1
        install_cron_task
        return
    fi

    parse_schedule_parameters "$@" || return 1

    validate_parameters || return 1
    check_requirements || return 1

    current_hour=$((10#$(date +%H)))
    if is_limited_hour "$current_hour"; then
        limit_on
    else
        limit_off
    fi
}

main "$@"
