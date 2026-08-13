#!/usr/bin/env bash
# Author: wenhao
# Example:
# sudo bash hardware_check_report.sh

HOST_NAME=$(hostname)
REPORT_DATE=$(date "+%Y-%m-%d %H:%M:%S %Z (%z)")
MACHINE_ID="N/A"
if [ -r /etc/machine-id ]; then
    MACHINE_ID=$(tr -d '[:space:]' < /etc/machine-id)
    MACHINE_ID=${MACHINE_ID:-N/A}
fi
FIO_SIZE="100G"
FIO_MIN_AVAILABLE_BYTES=$((110 * 1024 * 1024 * 1024))
PUBLIC_IP_LOOKUP_URL="https://ifconfig.co/ip"

# 功能：检查报告和压测所需工具，并通过 yum 或 apt 安装缺失的软件包。
# 参数：无。
# 返回值：依赖全部可用时返回 0，否则返回 1。
ensure_dependencies() {
    local -a dependencies=(
        "fio:fio:fio"
        "smartctl:smartmontools:smartmontools"
        "lsblk:util-linux:util-linux"
        "lscpu:util-linux:util-linux"
        "curl:curl:curl"
        "ip:iproute:iproute2"
        "lspci:pciutils:pciutils"
    )
    local -a missing_dependencies=()
    local -a missing_packages=()
    local -a index_updater=()
    local -a installer=()
    local dependency
    local command_name
    local package_name
    local yum_package
    local apt_package
    local package_manager

    for dependency in "${dependencies[@]}"; do
        IFS=':' read -r command_name yum_package apt_package <<< "$dependency"
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing_dependencies+=("$dependency")
        fi
    done

    [ "${#missing_dependencies[@]}" -eq 0 ] && return 0

    if ! command -v sudo >/dev/null 2>&1; then
        echo "Error: sudo is required to install dependencies" >&2
        return 1
    fi

    if command -v yum >/dev/null 2>&1; then
        package_manager="yum"
        installer=(sudo yum install -y)
    elif command -v apt >/dev/null 2>&1; then
        package_manager="apt"
        index_updater=(sudo apt update)
        installer=(sudo apt install -y)
    else
        echo "Error: yum or apt is required to install missing dependencies" >&2
        return 1
    fi

    for dependency in "${missing_dependencies[@]}"; do
        IFS=':' read -r command_name yum_package apt_package <<< "$dependency"
        if [ "$package_manager" = "yum" ]; then
            package_name="$yum_package"
        else
            package_name="$apt_package"
        fi
        missing_packages+=("$package_name")
    done

    if [ "${#index_updater[@]}" -gt 0 ]; then
        echo "Updating package index with $package_manager"
        if ! "${index_updater[@]}"; then
            echo "Error: failed to update package index with $package_manager" >&2
            return 1
        fi
    fi

    echo "Installing required packages with $package_manager: ${missing_packages[*]}"
    if ! "${installer[@]}" "${missing_packages[@]}"; then
        echo "Error: failed to install required packages with $package_manager" >&2
        return 1
    fi

    for dependency in "${dependencies[@]}"; do
        IFS=':' read -r command_name yum_package apt_package <<< "$dependency"
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Error: command is still unavailable after installation: $command_name" >&2
            return 1
        fi
    done

    return 0
}

# 功能：读取当前操作系统名称，优先使用 os-release 的 PRETTY_NAME。
# 参数：无。
# 返回值：通过标准输出返回系统名称；无法识别时回退到 uname 或 N/A。
get_system_name() {
    local PRETTY_NAME=""
    local NAME=""
    local VERSION_ID=""
    local system_name=""

    if [ -r /etc/os-release ]; then
        . /etc/os-release
        system_name="${PRETTY_NAME:-}"
        if [ -z "$system_name" ] && [ -n "${NAME:-}" ]; then
            system_name="$NAME"
            [ -n "${VERSION_ID:-}" ] && system_name="$system_name $VERSION_ID"
        fi
    fi

    if [ -z "$system_name" ]; then
        system_name=$(uname -s 2>/dev/null)
    fi

    printf '%s\n' "${system_name:-N/A}"
}

# 功能：通过 lscpu 读取 CPU 型号、架构、物理核心数和逻辑线程数。
# 参数：无。
# 返回值：通过标准输出返回“型号|架构|核心数|线程数”；字段不可用时对应值为 N/A。
get_cpu_topology() {
    local cpu_details

    if ! cpu_details=$(LC_ALL=C lscpu 2>/dev/null); then
        printf 'N/A|N/A|N/A|N/A\n'
        return
    fi

    printf '%s\n' "$cpu_details" | awk -F: '
        function trim(value) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            return value
        }
        {
            key = trim($1)
            value = trim($2)
            if (key == "Model name") model_name = value
            else if (key == "Architecture") architecture = value
            else if (key == "CPU(s)") thread_count = value
            else if (key == "Socket(s)") socket_count = value
            else if (key == "Core(s) per socket") cores_per_socket = value
        }
        END {
            if (socket_count ~ /^[1-9][0-9]*$/ \
                && cores_per_socket ~ /^[1-9][0-9]*$/) {
                core_count = socket_count * cores_per_socket
            } else if (thread_count ~ /^[1-9][0-9]*$/) {
                core_count = thread_count
            } else {
                core_count = "N/A"
            }
            if (model_name == "") model_name = "N/A"
            if (architecture == "") architecture = "N/A"
            if (thread_count !~ /^[1-9][0-9]*$/) thread_count = "N/A"
            printf "%s|%s|%s|%s\n", model_name, architecture, \
                core_count, thread_count
        }
    '
}

# 功能：按物理 socket 汇总 CPU 型号、架构、核心数和线程数。
# 参数：无。
# 返回值：逐行输出“socket ID|型号|架构|核心数|线程数”；拓扑缺失时输出一行回退结果。
get_physical_cpu_report() {
    local cpu_topology
    local architecture
    local fallback_core_count
    local fallback_thread_count
    local fallback_model
    local parseable_topology
    local cpu_id
    local core_id
    local socket_id
    local socket_core_key
    local socket_index
    local -a socket_order=()
    local -A socket_seen=()
    local -A socket_threads=()
    local -A socket_cores=()
    local -A socket_core_seen=()

    cpu_topology=$(get_cpu_topology)
    IFS='|' read -r fallback_model architecture fallback_core_count \
        fallback_thread_count <<< "$cpu_topology"

    if parseable_topology=$(LC_ALL=C lscpu -p=CPU,CORE,SOCKET 2>/dev/null); then
        while IFS=, read -r cpu_id core_id socket_id; do
            [[ "$cpu_id" =~ ^[0-9]+$ ]] || continue
            [[ "$core_id" =~ ^[0-9]+$ ]] || continue
            [[ "$socket_id" =~ ^[0-9]+$ ]] || continue

            if [ -z "${socket_seen[$socket_id]:-}" ]; then
                socket_seen["$socket_id"]=1
                socket_order+=("$socket_id")
            fi

            socket_threads["$socket_id"]=$((
                ${socket_threads[$socket_id]:-0} + 1
            ))
            socket_core_key="$socket_id:$core_id"
            if [ -z "${socket_core_seen[$socket_core_key]:-}" ]; then
                socket_core_seen["$socket_core_key"]=1
                socket_cores["$socket_id"]=$((
                    ${socket_cores[$socket_id]:-0} + 1
                ))
            fi
        done <<< "$parseable_topology"
    fi

    if [ "${#socket_order[@]}" -eq 0 ]; then
        printf 'N/A|%s|%s|%s|%s\n' "${fallback_model:-N/A}" \
            "${architecture:-N/A}" "${fallback_core_count:-N/A}" \
            "${fallback_thread_count:-N/A}"
        return
    fi

    for socket_index in "${socket_order[@]}"; do
        printf '%s|%s|%s|%s|%s\n' "$socket_index" \
            "${fallback_model:-N/A}" \
            "${architecture:-N/A}" "${socket_cores[$socket_index]:-N/A}" \
            "${socket_threads[$socket_index]:-N/A}"
    done
}

# 功能：读取主机物理内存总容量并转换为 GiB。
# 参数：无。
# 返回值：通过标准输出返回保留两位小数的 GiB 容量；无法读取时返回 N/A。
get_total_memory() {
    if [ ! -r /proc/meminfo ]; then
        printf 'N/A\n'
        return
    fi

    awk '/^MemTotal:/ {printf "%.2f GiB\n", $2 / 1024 / 1024; found=1; exit}
        END {if (!found) print "N/A"}' /proc/meminfo
}

# 功能：读取指定网络接口对应的网卡型号，优先使用 udevadm，失败时回退到 lspci 或驱动名称。
# 参数：$1 为网络接口名称，例如 eth0。
# 返回值：通过标准输出返回“(”左侧的网卡型号；无法识别时返回 N/A。
get_network_model() {
    local interface_name="$1"
    local device_path="/sys/class/net/$interface_name/device"
    local pci_address=""
    local model=""

    if [ -d "/sys/class/net/$interface_name/bonding" ]; then
        printf 'Bonding interface\n'
        return
    fi

    if command -v udevadm >/dev/null 2>&1; then
        model=$(udevadm info --query=property --path="/sys/class/net/$interface_name" \
            2>/dev/null | awk -F= '$1 == "ID_MODEL_FROM_DATABASE" {print $2; exit}')
    fi

    if [ -z "$model" ] && [ -e "$device_path" ]; then
        pci_address=$(basename "$(readlink -f "$device_path" 2>/dev/null)")
        model=$(lspci -s "$pci_address" 2>/dev/null \
            | sed -E 's/^[^[:space:]]+[[:space:]]+[^:]+:[[:space:]]*//' \
            | head -n 1)
    fi

    if [ -z "$model" ] && [ -L "$device_path/driver" ]; then
        model="$(basename "$(readlink -f "$device_path/driver")") driver"
    fi

    if [ -n "$model" ]; then
        model=$(printf '%s\n' "$model" \
            | sed 's/[[:space:]]*(.*$//; s/[[:space:]]*$//')
    fi

    printf '%s\n' "${model:-N/A}"
}

# 功能：读取指定网络接口当前协商的链路速率并转换为 Gbps。
# 参数：$1 为网络接口名称，例如 eth0。
# 返回值：通过标准输出返回以 Gbps 为单位的速率；接口未连接或无法读取时返回 N/A。
get_network_speed() {
    local interface_name="$1"
    local speed=""

    if [ -r "/sys/class/net/$interface_name/speed" ]; then
        speed=$(<"/sys/class/net/$interface_name/speed")
    fi

    if [[ "$speed" =~ ^[0-9]+$ ]] && [ "$speed" -gt 0 ]; then
        awk -v speed_mbps="$speed" 'BEGIN {
            speed_gbps = sprintf("%.3f", speed_mbps / 1000)
            sub(/0+$/, "", speed_gbps)
            sub(/\.$/, "", speed_gbps)
            printf "%s Gbps\n", speed_gbps
        }'
    else
        printf 'N/A\n'
    fi
}

# 功能：读取指定网络接口上的全部 global IPv4 地址。
# 参数：$1 为网络接口名称，例如 eth0。
# 返回值：通过标准输出返回逗号分隔的 Local IPv4；没有地址时返回 N/A。
get_local_ipv4() {
    local interface_name="$1"
    local local_ipv4s

    local_ipv4s=$(ip -o -4 addr show dev "$interface_name" scope global 2>/dev/null \
        | awk '
            {
                split($4, address, "/")
                values = values separator address[1]
                separator = ","
            }
            END {print values}
        ')

    printf '%s\n' "${local_ipv4s:-N/A}"
}

# 功能：通过指定网络接口访问公网查询服务，读取该接口对应的公网 IPv4。
# 参数：$1 为网络接口名称，例如 eth0。
# 返回值：通过标准输出返回合法公网 IPv4；查询失败或响应无效时返回 N/A。
get_public_ipv4() {
    local interface_name="$1"
    local public_ip=""

    public_ip=$(curl -4 --interface "$interface_name" --fail --silent \
        --max-time 5 "$PUBLIC_IP_LOOKUP_URL" 2>/dev/null | tr -d '[:space:]')
    public_ip=$(printf '%s\n' "$public_ip" | awk -F. '
        NF == 4 {
            for (octet_index = 1; octet_index <= 4; octet_index++) {
                if ($octet_index !~ /^[0-9]+$/ \
                    || $octet_index < 0 || $octet_index > 255) exit
            }
            print
        }
    ')

    printf '%s\n' "${public_ip:-N/A}"
}

# 功能：读取指定磁盘的序列号，优先使用 udevadm，缺失时回退到 smartctl。
# 参数：$1 为磁盘设备路径，例如 /dev/sda。
# 返回值：通过标准输出返回序列号；无法识别时返回 N/A。
get_disk_serial() {
    local device="$1"
    local serial=""

    if command -v udevadm >/dev/null 2>&1; then
        serial=$(udevadm info --query=property --name="$device" 2>/dev/null \
            | awk -F= '$1 == "ID_SERIAL" {print $2; exit}')
    fi

    if [ -z "$serial" ] && command -v smartctl >/dev/null 2>&1; then
        serial=$(smartctl -i "$device" 2>/dev/null \
            | awk -F: '/Serial Number/ {gsub(/^[[:space:]]+/, "", $2); print $2; exit}')
    fi

    printf '%s\n' "${serial:-N/A}"
}

# 功能：根据 NVMe SMART 的 Percentage Used 计算指定磁盘的剩余健康度。
# 参数：$1 为磁盘设备路径，例如 /dev/nvme0n1。
# 返回值：通过标准输出返回百分比健康度；无法识别时返回 N/A。
get_disk_health() {
    local device="$1"
    local used=""
    local health

    if command -v smartctl >/dev/null 2>&1; then
        used=$(smartctl -a "$device" 2>/dev/null \
            | awk -F: '/Percentage Used/ {gsub(/[%[:space:]]/, "", $2); print $2; exit}')
    fi

    if [[ "$used" =~ ^[0-9]+$ ]]; then
        health=$((100 - used))
        [ "$health" -lt 0 ] && health=0
        printf '%s%%\n' "$health"
    else
        printf 'N/A\n'
    fi
}

# 功能：从指定磁盘的挂载点中选择文件系统总容量最大的压测路径。
# 参数：$1 为磁盘设备路径，例如 /dev/sda。
# 返回值：通过标准输出返回挂载路径；磁盘没有挂载点时返回空字符串。
select_benchmark_mount() {
    local device="$1"
    local mount_point
    local filesystem_bytes
    local best_mount=""
    local best_capacity=0

    while IFS= read -r mount_point; do
        [ -z "$mount_point" ] && continue
        [ ! -d "$mount_point" ] && continue

        filesystem_bytes=$(df -PB1 -- "$mount_point" 2>/dev/null \
            | awk 'NR == 2 {print $2}')
        [[ "$filesystem_bytes" =~ ^[0-9]+$ ]] || continue

        if [ "$filesystem_bytes" -gt "$best_capacity" ]; then
            best_mount="$mount_point"
            best_capacity="$filesystem_bytes"
        fi
    done < <(lsblk -ln -o MOUNTPOINT "$device" 2>/dev/null)

    printf '%s\n' "$best_mount"
}

# 功能：在指定挂载点串行执行 fio 随机混合读写压测并提取读写 IOPS。
# 参数：$1 为挂载路径，$2 为用于 fio job 名称的磁盘名。
# 返回值：通过标准输出返回“读 IOPS|写 IOPS”；压测失败时返回“N/A|N/A”。
run_fio_benchmark() {
    local mount_point="$1"
    local disk_name="$2"
    local test_dir="${mount_point%/}/fio_test"
    local output_file
    local available_bytes
    local read_iops
    local write_iops

    available_bytes=$(df -PB1 -- "$mount_point" 2>/dev/null \
        | awk 'NR == 2 {print $4}')
    if ! [[ "$available_bytes" =~ ^[0-9]+$ ]] \
        || [ "$available_bytes" -lt "$FIO_MIN_AVAILABLE_BYTES" ]; then
        echo "Warning: $disk_name has less than $FIO_SIZE available at $mount_point" >&2
        printf 'N/A|N/A\n'
        return
    fi

    if ! mkdir -p -- "$test_dir"; then
        echo "Warning: $disk_name cannot create test directory: $test_dir" >&2
        printf 'N/A|N/A\n'
        return
    fi

    output_file=$(mktemp "${TMPDIR:-/tmp}/fio_${disk_name}.XXXXXX") || {
        echo "Warning: $disk_name cannot create fio output file" >&2
        printf 'N/A|N/A\n'
        return
    }

    if ! fio --ioengine=psync --direct=0 --bs=256k --rw=randrw \
        --rwmixread=80 --size="$FIO_SIZE" --numjobs=1 --runtime=120 \
        --group_reporting --unlink=1 --name="${disk_name}_test" \
        --filename="$test_dir/test.file" >"$output_file" 2>&1; then
        echo "Warning: fio benchmark failed for $disk_name at $mount_point" >&2
        rm -f -- "$output_file"
        printf 'N/A|N/A\n'
        return
    fi

    read_iops=$(sed -nE 's/^[[:space:]]*read:[[:space:]]+IOPS=([^,]+),.*/\1/p' \
        "$output_file" | head -n 1)
    write_iops=$(sed -nE 's/^[[:space:]]*write:[[:space:]]+IOPS=([^,]+),.*/\1/p' \
        "$output_file" | head -n 1)
    rm -f -- "$output_file"

    if [ -z "$read_iops" ] || [ -z "$write_iops" ]; then
        echo "Warning: cannot parse fio IOPS for $disk_name" >&2
        printf 'N/A|N/A\n'
        return
    fi

    printf '%s|%s\n' "$read_iops" "$write_iops"
}

if ! ensure_dependencies; then
    exit 1
fi

echo "############################################################"
echo "#                  Hardware Check Report                   #"
echo "############################################################"
echo
echo "System   : $(get_system_name)"
echo "Hostname : $HOST_NAME"
echo "Machine ID: $MACHINE_ID"
echo "Date     : $REPORT_DATE"

echo
echo "======================== CPU =============================="

cpu_index=1

while IFS='|' read -r socket_id cpu_model cpu_architecture \
    cpu_core_count cpu_thread_count; do
    [ -z "$cpu_model" ] && continue
    [ "$cpu_index" -gt 1 ] && echo
    echo "CPU-$cpu_index (Socket $socket_id) : $cpu_model"
    echo "  Architecture: $cpu_architecture"
    echo "  Cores       : $cpu_core_count"
    echo "  Threads     : $cpu_thread_count"
    cpu_index=$((cpu_index + 1))
done < <(get_physical_cpu_report)

echo
echo "======================== MEMORY ==========================="
echo "Total Memory: $(get_total_memory)"

echo
echo "======================== GPU =============================="

gpu_index=1
if command -v nvidia-smi >/dev/null 2>&1; then
    while IFS= read -r gpu; do
        [ -z "$gpu" ] && continue
        echo "GPU-$gpu_index : $gpu"
        gpu_index=$((gpu_index + 1))
    done < <(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)
elif command -v lspci >/dev/null 2>&1; then
    while IFS= read -r gpu; do
        [ -z "$gpu" ] && continue
        echo "GPU-$gpu_index : $gpu"
        gpu_index=$((gpu_index + 1))
    done < <(lspci | grep -Ei "VGA|3D|Display")
else
    echo "GPU information unavailable: nvidia-smi and lspci not installed"
fi

echo
echo "======================== NETWORK =========================="

printf "%-16s %-48s %-14s %-22s %-15s\n" \
    "Interface" "Model" "Link Speed" "Local IPv4" "Public IPv4"
printf "%-16s %-48s %-14s %-22s %-15s\n" \
    "---------" "-----" "----------" "----------" "---------"

for interface_path in /sys/class/net/*; do
    [ -e "$interface_path" ] || continue
    interface_name=$(basename "$interface_path")
    [ "$interface_name" = "lo" ] && continue
    if [ ! -e "$interface_path/device" ] && [ ! -d "$interface_path/bonding" ]; then
        continue
    fi

    network_model=$(get_network_model "$interface_name")
    network_speed=$(get_network_speed "$interface_name")
    local_ipv4=$(get_local_ipv4 "$interface_name")
    public_ip="N/A"
    if ip -o -4 addr show dev "$interface_name" scope global 2>/dev/null \
        | grep -q .; then
        public_ip=$(get_public_ipv4 "$interface_name")
    fi

    printf "%-16s %-48s %-14s %-22s %-15s\n" \
        "$interface_name" "${network_model:0:47}" "$network_speed" \
        "${local_ipv4:0:21}" "$public_ip"
done

echo
echo "======================== SSD =============================="

if ! command -v fio >/dev/null 2>&1; then
    echo "Warning: fio not installed; IOPS values will be N/A" >&2
fi

printf "%-12s %-12s %-25s %-10s %-12s %-12s\n" \
    "Device" "Capacity" "Serial" "Health" "Read IOPS" "Write IOPS"
printf "%-12s %-12s %-25s %-10s %-12s %-12s\n" \
    "------" "--------" "------" "------" "---------" "----------"

while read -r disk disk_type rotational; do
    [ -z "$disk" ] && continue
    [ "$disk_type" != "disk" ] && continue
    [ "$rotational" != "0" ] && continue

    device="/dev/$disk"
    benchmark_mount=$(select_benchmark_mount "$device")

    capacity=$(lsblk -dn -o SIZE "$device" 2>/dev/null | xargs)
    serial=$(get_disk_serial "$device")
    health=$(get_disk_health "$device")
    read_iops="N/A"
    write_iops="N/A"

    if [ -n "$benchmark_mount" ] && command -v fio >/dev/null 2>&1; then
        benchmark_result=$(run_fio_benchmark "$benchmark_mount" "$disk")
        IFS='|' read -r read_iops write_iops <<< "$benchmark_result"
    fi

    printf "%-12s %-12s %-25s %-10s %-12s %-12s\n" \
        "$disk" "${capacity:-N/A}" "${serial:0:24}" "$health" \
        "${read_iops:-N/A}" "${write_iops:-N/A}"
done < <(lsblk -dn -o NAME,TYPE,ROTA 2>/dev/null)

echo
echo "############################################################"
echo "#                     Report Finished                     #"
echo "############################################################"
