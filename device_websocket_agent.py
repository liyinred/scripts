#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: wenhao
"""
CentOS7 主机 WebSocket 连接脚本（Python 2.7 兼容）。

功能：
1. 在 CentOS7 主机端生成稳定 device_id。
2. 连接后端 WebSocket 接口并等待指令。
3. 收到 collect_system_info 指令后采集主机系统信息并返回。
4. 收到 collect_acceptance_info 指令后采集设备验收单信息并返回。
5. 收到限速指令后安装或移除设备上行限速 cron。
6. 收到带宽压测指令后执行 iperf3，并解析 receiver 速率与 sender 重传率。
"""

from __future__ import print_function

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import errno
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import time

try:
    from urllib import quote
    from urlparse import urlparse
except ImportError:
    from urllib.parse import quote
    from urllib.parse import urlparse


DEFAULT_RECONNECT_DELAY = 5
DEFAULT_SOCKET_TIMEOUT = 30
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEVICE_ID_SALT = "32d950015e269bf9492d744b8f5512485293394892735c8821f2013f470a0c2e"
DEVICE_ID_VERSION = "v2"
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,127}$")
PHYSICAL_BLOCK_DEVICE_PATTERN = re.compile(
    r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)$"
)
MACHINE_ID_PATH = "/etc/machine-id"
PROC_NET_DEV_PATH = "/proc/net/dev"
SYS_CLASS_NET_PATH = "/sys/class/net"
LSBLK_DISK_KIND_COLUMNS = "NAME,TYPE,ROTA"
LSBLK_DISK_COLUMNS = "NAME,TYPE,SIZE,MOUNTPOINT,FSUSED"
LSBLK_COMPAT_DISK_COLUMNS = "NAME,TYPE,SIZE,MOUNTPOINT"
SSH_CONNECTION_PORT_COMMAND = (
    "ss -tnp | grep \":$(echo $SSH_CONNECTION | cut -d' ' -f4 | cut -d' ' -f1)\" "
    "| head -1 | awk '{print $4}' | cut -d: -f2"
)
IGNORED_PUBLIC_IP_INTERFACE_NAMES = ("lo", "docker0")
PUBLIC_IP_LOOKUP_URLS = {
    4: ("4.itdog.cn",),
    6: ("6.itdog.cn",),
}
PUBLIC_IP_LOOKUP_TIMEOUT_SECONDS = 5
DEFAULT_TERMINAL_COLUMNS = 120
DEFAULT_TERMINAL_ROWS = 32
TERMINAL_OUTPUT_CHUNK_SIZE = 4096
ENABLE_UPLOAD_LIMIT_COMMAND = (
    'curl -fSL "https://gitee.com/liyinred/scripts/raw/master/'
    'device_upload_limit.sh" | sudo bash -s -- install-cron '
    '-s {start} -e {end} -r {rate}mbit'
)
DISABLE_UPLOAD_LIMIT_COMMAND = (
    "sudo /bin/bash /usr/local/sbin/device_upload_limit.sh remove-cron"
)
IPERF3_SUMMARY_PATTERN = re.compile(
    r"^\[\s*(?:\d+|SUM)\]\s+\S+\s+sec\s+"
    r"(?P<transfer>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<transfer_unit>[KMGT]?Bytes)\s+"
    r"(?P<bitrate>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<bitrate_unit>[KMGT]?bits/sec)"
    r"(?:\s+(?P<retransmissions>[0-9]+))?\s+"
    r"(?P<role>sender|receiver)\s*$"
)
IPERF3_BITRATE_MULTIPLIERS = {
    "bits/sec": 1,
    "Kbits/sec": 1000,
    "Mbits/sec": 1000 ** 2,
    "Gbits/sec": 1000 ** 3,
    "Tbits/sec": 1000 ** 4,
}
IPERF3_TRANSFER_MULTIPLIERS = {
    "Bytes": 1,
    "KBytes": 1024,
    "MBytes": 1024 ** 2,
    "GBytes": 1024 ** 3,
    "TBytes": 1024 ** 4,
}
ESTIMATED_PACKET_BYTES = 1500.0
MAX_BANDWIDTH_TEST_DURATION_SECONDS = 3600

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


def read_text_file(path):
    """读取文本文件内容。"""
    try:
        with open(path, "r") as file_obj:
            content = file_obj.read().strip()
            return content or None
    except IOError:
        return None


def run_command(command, allow_nonzero=False):
    """执行本机命令并读取标准输出。

    功能：执行指定命令并返回 UTF-8 标准输出，可按需保留非零退出码时的输出。
    参数：command 为 subprocess 命令参数；allow_nonzero 表示是否接受非零退出码。
    返回值：str 或 None，成功且有输出时返回文本，否则返回 None。
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_data, _stderr_data = process.communicate()
        if process.returncode != 0 and not allow_nonzero:
            return None
        if not stdout_data:
            return None
        return stdout_data.decode("utf-8", "ignore").strip()
    except OSError:
        return None


def validate_upload_limit_config(start, end, rate):
    """校验后端下发的设备限速配置。

    功能：确保开始小时、结束小时和速率均为安全的整数参数，并校验业务范围。
    参数：start 为开始小时；end 为结束小时；rate 为限速速率，单位为 mbit。
    返回值：tuple，依次包含校验后的 start、end 和 rate 整数。
    """
    values = (start, end, rate)
    if any(
        isinstance(value, bool) or not isinstance(value, INTEGER_TYPES)
        for value in values
    ):
        raise ValueError("限速配置必须为整数")
    if not 0 <= start <= 23 or not 0 <= end <= 23:
        raise ValueError("限速开始时间和结束时间必须为 0-23")
    if start == end:
        raise ValueError("限速开始时间和结束时间不能相同")
    if rate <= 0:
        raise ValueError("限速速率必须为正整数")
    return start, end, rate


def execute_upload_limit_command(enabled, start=20, end=22, rate=50):
    """执行设备上行限速状态切换命令。

    功能：通过固定的 bash pipeline 安装或移除 device_upload_limit cron，并校验退出码；
        安装时将已校验配置填充到命令模板。
    参数：enabled 为 True 时安装限速 cron，为 False 时移除限速 cron；start 为开始小时；
        end 为结束小时；rate 为限速速率，单位为 mbit。
    返回值：dict，命令成功时返回空数据对象供 WebSocket 响应使用。
    """
    if enabled:
        start, end, rate = validate_upload_limit_config(start, end, rate)
        command = ENABLE_UPLOAD_LIMIT_COMMAND.format(start=start, end=end, rate=rate)
    else:
        command = DISABLE_UPLOAD_LIMIT_COMMAND
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-o", "pipefail", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_data, stderr_data = process.communicate()
    except OSError as exc:
        raise RuntimeError("无法启动限速命令: %s" % exc)
    if process.returncode != 0:
        error_data = stderr_data or stdout_data or b""
        error_text = error_data.decode("utf-8", "ignore").strip()
        raise RuntimeError(error_text or "限速命令执行失败，退出码 %s" % process.returncode)
    return {}


def validate_bandwidth_test_config(server_ip, duration_seconds):
    """校验后端下发的带宽压测配置。

    功能：确保服务器地址为完整 IPv4，压测时间为允许范围内的整数。
    参数：server_ip 为 iperf3 服务端 IPv4；duration_seconds 为压测秒数。
    返回值：tuple，依次包含规范化 IPv4 和压测秒数。
    """
    normalized_server_ip = (server_ip or "").strip()
    ip_segments = normalized_server_ip.split(".")
    if len(ip_segments) != 4 or any(
        not segment.isdigit()
        or str(int(segment)) != segment
        or not 0 <= int(segment) <= 255
        for segment in ip_segments
    ):
        raise ValueError("接收端 IP 必须为完整 IPv4 地址")
    if isinstance(duration_seconds, bool) or not isinstance(
        duration_seconds, INTEGER_TYPES
    ):
        raise ValueError("压测时间必须为整数")
    if not 1 <= duration_seconds <= MAX_BANDWIDTH_TEST_DURATION_SECONDS:
        raise ValueError("压测时间必须为 1-%s 秒" % MAX_BANDWIDTH_TEST_DURATION_SECONDS)
    return normalized_server_ip, duration_seconds


def parse_iperf3_bandwidth_test_output(output):
    """解析 iperf3 文本汇总行并计算带宽压测结果。

    功能：使用 receiver 汇总行计算 bps；使用 sender 的 Retr 与传输字节数，按每包
        1500 bytes 估算总包数并计算重传率百分比。
    参数：output 为 iperf3 标准输出文本。
    返回值：dict，包含整数 bitrateBps 和保留两位小数的 retransmissionRate。
    """
    summaries = {}
    for raw_line in (output or "").splitlines():
        summary_match = IPERF3_SUMMARY_PATTERN.match(raw_line.strip())
        if summary_match:
            summaries[summary_match.group("role")] = summary_match.groupdict()
    sender_summary = summaries.get("sender")
    receiver_summary = summaries.get("receiver")
    if sender_summary is None or receiver_summary is None:
        raise ValueError("iperf3 输出缺少 sender 或 receiver 汇总行")
    try:
        bitrate_bps = int(
            round(
                float(receiver_summary["bitrate"])
                * IPERF3_BITRATE_MULTIPLIERS[receiver_summary["bitrate_unit"]]
            )
        )
        sender_bytes = (
            float(sender_summary["transfer"])
            * IPERF3_TRANSFER_MULTIPLIERS[sender_summary["transfer_unit"]]
        )
        retransmissions = int(sender_summary["retransmissions"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("iperf3 汇总行数值非法")
    if bitrate_bps < 0 or sender_bytes <= 0 or retransmissions < 0:
        raise ValueError("iperf3 汇总行数值非法")
    estimated_packet_count = sender_bytes / ESTIMATED_PACKET_BYTES
    retransmission_rate = round(retransmissions / estimated_packet_count * 100, 2)
    return {
        "bitrateBps": bitrate_bps,
        "retransmissionRate": retransmission_rate,
    }


def execute_bandwidth_test(server_ip, duration_seconds):
    """执行 iperf3 带宽压测并返回解析结果。

    功能：以安全 argv 方式执行 iperf3 -c <ip> -t <seconds>，并解析其汇总输出。
    参数：server_ip 为 iperf3 服务端 IPv4；duration_seconds 为压测秒数。
    返回值：dict，包含 receiver 压测速率 bps 和估算重传率百分比。
    """
    server_ip, duration_seconds = validate_bandwidth_test_config(
        server_ip, duration_seconds
    )
    command_environment = os.environ.copy()
    command_environment["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            ["iperf3", "-c", server_ip, "-t", str(duration_seconds)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=command_environment,
        )
        stdout_data, stderr_data = process.communicate()
    except OSError as exc:
        raise RuntimeError("无法启动 iperf3: %s" % exc)
    if process.returncode != 0:
        error_data = stderr_data or stdout_data or b""
        error_text = error_data.decode("utf-8", "ignore").strip()
        raise RuntimeError(error_text or "iperf3 执行失败，退出码 %s" % process.returncode)
    output_text = (stdout_data or b"").decode("utf-8", "ignore")
    return parse_iperf3_bandwidth_test_output(output_text)


def parse_tcp_port(port_text):
    """解析 TCP 端口文本。

    功能：将命令输出中的端口文本转换为合法 TCP 端口号。
    参数：port_text 为待解析的端口文本。
    返回值：int 或 None，合法时返回端口号，缺失或非法时返回 None。
    """
    try:
        port = int((port_text or "").strip())
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def read_ssh_port():
    """读取当前设备 SSH 端口。

    功能：通过 ss 命令结合 SSH_CONNECTION 环境变量读取当前 SSH 连接的本地端口；
        命令无法读取合法端口时返回 SSH 默认端口 22。
    参数：无。
    返回值：int，当前设备 SSH 端口号。
    """
    current_port = parse_tcp_port(
        run_command(["/bin/sh", "-c", SSH_CONNECTION_PORT_COMMAND])
    )
    if current_port is not None:
        return current_port
    return 22


def normalize_device_id(device_id):
    """规范化并校验 device_id。"""
    normalized = (device_id or "").strip()
    if not DEVICE_ID_PATTERN.match(normalized):
        raise ValueError("invalid device_id")
    return normalized


def read_machine_id():
    """读取 CentOS7 主机的 machine-id。"""
    machine_id = read_text_file(MACHINE_ID_PATH)
    if not machine_id:
        raise RuntimeError("failed to read /etc/machine-id, exit")
    return machine_id


def generate_device_id(machine_id):
    """根据 salt 和 machine_id 生成 v2 device_id。

    功能：使用 SHA256(salt + machine_id) 生成稳定的设备标识。
    参数：machine_id 为当前主机的 machine-id。
    返回值：str，URL-safe Base64 编码且去除 padding 的 v2 device_id。
    """
    digest_source = DEVICE_ID_SALT + machine_id
    digest = hashlib.sha256(digest_source.encode("utf-8")).digest()
    device_id = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return normalize_device_id(device_id)


def parse_lscpu_fields(output):
    """解析 lscpu 输出字段。

    功能：将 lscpu 的 key: value 文本输出解析为字段字典，字段名统一转为小写。
    参数：output 为 lscpu 命令标准输出文本。
    返回值：dict，key 为小写字段名，value 为字段文本；输出为空时返回空字典。
    """
    fields = {}
    for line in (output or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower()
        if normalized_key:
            fields[normalized_key] = value.strip()
    return fields


def parse_os_release_fields(content):
    """解析 /etc/os-release 字段。"""
    fields = {}
    if not content:
        return fields
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def build_os_image_version_from_os_release(fields):
    """根据 os-release 字段组成镜像版本内容。"""
    name = fields.get("NAME", "").strip()
    version_id = fields.get("VERSION_ID", "").strip()
    os_image_version = " ".join(part for part in (name, version_id) if part)
    return os_image_version or "Linux"


def read_os_image_version():
    """读取 /etc/os-release 并组成系统镜像版本。"""
    os_release = read_text_file("/etc/os-release")
    return build_os_image_version_from_os_release(parse_os_release_fields(os_release))


def read_memory_info_kb():
    """读取 /proc/meminfo 的内存字段。

    功能：解析 /proc/meminfo 中以 kB 为单位的内存指标。
    参数：无。
    返回值：dict，key 为内存字段名，value 为 kB 数值；读取失败时返回空字典。
    """
    content = read_text_file("/proc/meminfo")
    if not content:
        return {}
    memory_info = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.split()
        if not parts:
            continue
        try:
            memory_info[key.strip()] = int(parts[0])
        except ValueError:
            continue
    return memory_info


def read_total_memory_bytes():
    """读取主机总内存字节数。

    功能：读取 /proc/meminfo 中的 MemTotal 并转换为 bytes。
    参数：无。
    返回值：int 或 None，成功时返回总内存 bytes，读取失败时返回 None。
    """
    total_memory_kb = read_memory_info_kb().get("MemTotal")
    if total_memory_kb is None:
        return None
    return total_memory_kb * 1024


def read_used_memory_bytes():
    """读取主机占用内存字节数。

    功能：优先按 MemTotal - MemAvailable 计算占用内存；MemAvailable 缺失时使用
        MemFree、Buffers 和 Cached 估算可用内存后再计算。
    参数：无。
    返回值：int 或 None，成功时返回占用内存 bytes，读取失败时返回 None。
    """
    memory_info = read_memory_info_kb()
    total_memory_kb = memory_info.get("MemTotal")
    if total_memory_kb is None:
        return None
    available_memory_kb = memory_info.get("MemAvailable")
    if available_memory_kb is None:
        free_memory_kb = memory_info.get("MemFree")
        buffers_kb = memory_info.get("Buffers", 0)
        cached_kb = memory_info.get("Cached", 0)
        if free_memory_kb is None:
            return None
        available_memory_kb = free_memory_kb + buffers_kb + cached_kb
    used_memory_kb = total_memory_kb - available_memory_kb
    if used_memory_kb < 0:
        used_memory_kb = 0
    return used_memory_kb * 1024


def is_physical_block_device(device_name):
    """判断 block device 是否为需要统计的物理磁盘。"""
    return bool(PHYSICAL_BLOCK_DEVICE_PATTERN.match(device_name or ""))


def parse_lsblk_pairs(line):
    """解析 lsblk -P 输出行。

    功能：将 NAME="sda" TYPE="disk" 形式的 lsblk 文本解析为字段字典。
    参数：line 为 lsblk -P 输出的单行文本。
    返回值：dict，key 为字段名，value 为字段值；无法解析时返回空字典。
    """
    pairs = {}
    for key, value in re.findall(r'([A-Z]+)="([^"]*)"', line or ""):
        pairs[key] = value
    return pairs


def get_partition_parent_device_name(device_name):
    """推导分区所属物理磁盘名称。

    功能：根据 sda1、xvdb2、nvme0n1p1 等分区命名推导父级 block device。
    参数：device_name 为 lsblk 返回的 NAME 字段。
    返回值：str 或 None，成功时返回父级物理磁盘名称，无法推导时返回 None。
    """
    if not device_name:
        return None
    nvme_match = re.match(r"^(nvme\d+n\d+)p\d+$", device_name)
    if nvme_match:
        return nvme_match.group(1)
    disk_match = re.match(r"^((?:sd|hd|vd|xvd)[a-z]+)\d+$", device_name)
    if disk_match:
        return disk_match.group(1)
    return None


def parse_lsblk_disk_kinds(output):
    """解析 lsblk 磁盘介质类型输出。

    功能：解析 ``lsblk -dn -o NAME,TYPE,ROTA`` 输出，仅识别 TYPE=disk 的物理
        block device，并按 ROTA=0/1 分别标记为 SSD/HDD。
    参数：output 为 lsblk 命令标准输出文本。
    返回值：dict，key 为物理磁盘名称，value 为 ssd 或 hdd。
    """
    disk_kinds = {}
    for line in (output or "").splitlines():
        columns = line.split()
        if len(columns) != 3:
            continue
        device_name, device_type, rotational = columns
        if device_type != "disk" or not is_physical_block_device(device_name):
            continue
        if rotational == "0":
            disk_kinds[device_name] = "ssd"
        elif rotational == "1":
            disk_kinds[device_name] = "hdd"
    return disk_kinds


def parse_lsblk_disk_info(
    output,
    disk_kinds,
    disk_used_bytes_overrides=None,
):
    """解析 lsblk 磁盘信息输出。

    功能：读取 lsblk -P -b -n 输出，按预先识别的磁盘介质类型汇总物理磁盘容量、
        数量和基础明细。
    参数：output 为 lsblk 命令标准输出文本；disk_kinds 为按物理磁盘名称记录的
        SSD/HDD 类型；disk_used_bytes_overrides 为按物理磁盘名称提供的占用空间
        覆盖值，通常来源于 df。
    返回值：dict，包含 SSD/HDD 汇总字段和 diskDetails 基础明细；明细健康度默认为 UNKNOWN。
    """
    ssd_total_disk = 0
    hdd_total_disk = 0
    ssd_count = 0
    hdd_count = 0
    disk_rows = []
    disk_used_bytes = {}
    for line in (output or "").splitlines():
        fields = parse_lsblk_pairs(line)
        device_name = fields.get("NAME")
        device_type = fields.get("TYPE")
        if device_type == "disk":
            disk_rows.append(fields)
            disk_used_bytes[device_name] = parse_nonnegative_int(
                fields.get("FSUSED")
            )
            continue
        parent_device_name = get_partition_parent_device_name(device_name)
        partition_used_bytes = parse_nonnegative_int(fields.get("FSUSED"))
        if parent_device_name and partition_used_bytes is not None:
            current_used_bytes = disk_used_bytes.get(parent_device_name) or 0
            disk_used_bytes[parent_device_name] = (
                current_used_bytes + partition_used_bytes
            )

    for device_name, used_bytes in (disk_used_bytes_overrides or {}).items():
        disk_used_bytes[device_name] = used_bytes

    disk_details = []
    for fields in disk_rows:
        device_name = fields.get("NAME")
        if not is_physical_block_device(device_name):
            continue
        try:
            device_size = int(fields.get("SIZE", ""))
        except ValueError:
            continue
        disk_kind = disk_kinds.get(device_name)
        if disk_kind == "ssd":
            ssd_count += 1
            ssd_total_disk += device_size
        elif disk_kind == "hdd":
            hdd_count += 1
            hdd_total_disk += device_size
        else:
            continue
        disk_details.append(
            {
                "name": device_name,
                "type": disk_kind.upper(),
                "usedBytes": min(disk_used_bytes.get(device_name) or 0, device_size),
                "totalBytes": device_size,
                "health": "UNKNOWN",
                "readIops": None,
                "writeIops": None,
            }
        )
    return {
        "ssdTotalDisk": ssd_total_disk,
        "hddTotalDisk": hdd_total_disk,
        "ssdCount": ssd_count,
        "hddCount": hdd_count,
        "diskDetails": disk_details,
    }


def parse_nonnegative_int(value):
    """解析非负整数。

    功能：将 lsblk 容量字段转换为非负整数。
    参数：value 为待解析的数值文本或数值。
    返回值：int 或 None，合法非负整数时返回数值，否则返回 None。
    """
    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return None
    return numeric_value if numeric_value >= 0 else None


def parse_nonnegative_float(value):
    """解析非负浮点数。

    功能：将 iostat 指标字段转换为有限的非负浮点数。
    参数：value 为待解析的数值文本或数值。
    返回值：float 或 None，合法时返回浮点数，否则返回 None。
    """
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if numeric_value < 0 or math.isnan(numeric_value) or math.isinf(numeric_value):
        return None
    return numeric_value


def parse_iostat_disk_iops(output):
    """解析各物理磁盘的读取和写入 IOPS。

    功能：解析 iostat -dx 1 2 文本，按最后一次采样表头中的 r/s 和 w/s 列读取 IOPS。
    参数：output 为 iostat 标准输出文本。
    返回值：dict，key 为物理磁盘名称，value 为包含 readIops 和 writeIops 的字典。
    """
    read_iops_index = None
    write_iops_index = None
    current_sample = {}
    for line in (output or "").splitlines():
        columns = line.split()
        if not columns:
            continue
        if columns[0].rstrip(":") == "Device":
            try:
                read_iops_index = columns.index("r/s")
                write_iops_index = columns.index("w/s")
            except ValueError:
                read_iops_index = None
                write_iops_index = None
            current_sample = {}
            continue
        if (
            read_iops_index is None
            or write_iops_index is None
            or len(columns) <= max(read_iops_index, write_iops_index)
        ):
            continue
        device_name = columns[0]
        if not is_physical_block_device(device_name):
            continue
        read_iops = parse_nonnegative_float(columns[read_iops_index])
        write_iops = parse_nonnegative_float(columns[write_iops_index])
        if read_iops is not None and write_iops is not None:
            current_sample[device_name] = {
                "readIops": read_iops,
                "writeIops": write_iops,
            }
    return current_sample


def collect_disk_iops():
    """采集当前磁盘读取和写入 IOPS。

    功能：执行两次间隔一秒的 iostat 扩展采样，并使用第二次采样的 r/s 和 w/s 指标。
    参数：无。
    返回值：dict，key 为物理磁盘名称，value 为读取和写入 IOPS；命令失败时返回空字典。
    """
    output = run_command(["/bin/sh", "-c", "LC_ALL=C iostat -dx 1 2"])
    return parse_iostat_disk_iops(output)


def parse_smart_health(output):
    """解析 smartctl 健康检查结果。

    功能：从 smartctl -H 输出中识别磁盘整体健康状态。
    参数：output 为 smartctl 标准输出文本。
    返回值：str，返回 PASSED、FAILED 或 UNKNOWN。
    """
    normalized_output = (output or "").upper()
    if re.search(r"(?:RESULT|STATUS)\s*:\s*(?:PASSED|OK)\b", normalized_output):
        return "PASSED"
    if re.search(r"(?:RESULT|STATUS)\s*:\s*(?:FAILED|BAD)\b", normalized_output):
        return "FAILED"
    return "UNKNOWN"


def parse_ssd_health_percentage(output):
    """解析 SSD 剩余健康度百分比。

    功能：读取 smartctl -a 输出中的 Percentage Used，并以 100 减去已用百分比
        计算剩余健康度，结果限制在 0 至 100。
    参数：output 为 smartctl 标准输出文本。
    返回值：int 或 None，成功时返回健康度百分比，字段缺失时返回 None。
    """
    matched = re.search(r"Percentage Used\s*:\s*(\d+)\s*%", output or "", re.I)
    if not matched:
        return None
    return max(0, min(100, 100 - int(matched.group(1))))


def parse_df_used_bytes(output):
    """解析 df 输出中的已占用字节数。

    功能：解析 df -B1 -P 输出最后一行的 Used 字段。
    参数：output 为 df 标准输出文本。
    返回值：int 或 None，成功时返回非负占用字节数，输出非法时返回 None。
    """
    lines = [line for line in (output or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    columns = lines[-1].split()
    if len(columns) < 6:
        return None
    return parse_nonnegative_int(columns[2])


def collect_mountpoint_used_bytes(mountpoint):
    """采集单个挂载点的文件系统占用空间。

    功能：执行 df -B1 -P 获取挂载点的实际文件系统占用字节数。
    参数：mountpoint 为 lsblk 返回的绝对挂载路径。
    返回值：int 或 None，成功时返回占用字节数，命令失败时返回 None。
    """
    if not (mountpoint or "").startswith("/"):
        return None
    return parse_df_used_bytes(run_command(["df", "-B1", "-P", mountpoint]))


def collect_disk_used_bytes(output):
    """按物理磁盘汇总已挂载文件系统占用空间。

    功能：从 lsblk 输出提取磁盘及分区挂载点，通过 df 获取占用空间并按父级
        物理磁盘名称求和。
    参数：output 为 lsblk -P 输出文本。
    返回值：dict，key 为物理磁盘名称，value 为其文件系统占用总字节数。
    """
    disk_used_bytes = {}
    for line in (output or "").splitlines():
        fields = parse_lsblk_pairs(line)
        device_name = fields.get("NAME")
        if fields.get("TYPE") == "disk":
            parent_device_name = device_name
        else:
            parent_device_name = get_partition_parent_device_name(device_name)
        used_bytes = collect_mountpoint_used_bytes(fields.get("MOUNTPOINT"))
        if not parent_device_name or used_bytes is None:
            continue
        disk_used_bytes[parent_device_name] = (
            disk_used_bytes.get(parent_device_name, 0) + used_bytes
        )
    return disk_used_bytes


def collect_disk_health(device_name, disk_type):
    """采集单块磁盘健康度。

    功能：SSD 先执行 smartctl -a，并根据 Percentage Used 计算剩余健康度百分比；
        字段缺失时与 HDD 一样执行 smartctl -H，并归一化整体健康状态。
    参数：device_name 为 lsblk 返回的物理磁盘名称；disk_type 为 SSD 或 HDD。
    返回值：int 或 str，优先返回 SSD 的 0 至 100 健康度百分比，否则返回 PASSED、
        FAILED 或 UNKNOWN；无法解析时返回 UNKNOWN。
    """
    if disk_type == "SSD":
        output = run_command(
            ["smartctl", "-a", "/dev/%s" % device_name],
            allow_nonzero=True,
        )
        health_percentage = parse_ssd_health_percentage(output)
        if health_percentage is not None:
            return health_percentage
    output = run_command(
        ["smartctl", "-H", "/dev/%s" % device_name],
        allow_nonzero=True,
    )
    return parse_smart_health(output)


def collect_disk_info():
    """采集主机 SSD/HDD 磁盘总容量和数量。

    功能：执行 lsblk -dn -o NAME,TYPE,ROTA 识别 SSD/HDD，再读取 block device
        容量和挂载信息；旧版 lsblk 不支持 FSUSED 时降级采集基础字段。随后使用
        iostat 采集读取和写入 IOPS，并使用 smartctl 读取每块已识别磁盘的健康度。
    参数：无。
    返回值：dict，包含 SSD/HDD 汇总字段，以及每块磁盘容量、占用、读取和写入 IOPS
        和健康度明细。
    """
    disk_kind_output = run_command(
        ["lsblk", "-dn", "-o", LSBLK_DISK_KIND_COLUMNS]
    )
    disk_kinds = parse_lsblk_disk_kinds(disk_kind_output)
    lsblk_output = run_command(
        ["lsblk", "-P", "-b", "-n", "-o", LSBLK_DISK_COLUMNS]
    )
    if not lsblk_output:
        lsblk_output = run_command(
            ["lsblk", "-P", "-b", "-n", "-o", LSBLK_COMPAT_DISK_COLUMNS]
        )
    if not disk_kind_output or not lsblk_output:
        return {
            "ssdTotalDisk": None,
            "hddTotalDisk": None,
            "ssdCount": None,
            "hddCount": None,
            "diskDetails": [],
        }
    disk_info = parse_lsblk_disk_info(
        lsblk_output,
        disk_kinds,
        collect_disk_used_bytes(lsblk_output),
    )
    disk_iops = collect_disk_iops()
    for disk_detail in disk_info["diskDetails"]:
        current_disk_iops = disk_iops.get(disk_detail["name"], {})
        disk_detail["readIops"] = current_disk_iops.get("readIops")
        disk_detail["writeIops"] = current_disk_iops.get("writeIops")
        disk_detail["health"] = collect_disk_health(
            disk_detail["name"],
            disk_detail["type"],
        )
    return disk_info


def read_lscpu_fields():
    """读取 lscpu CPU 信息字段。

    功能：执行 lscpu 命令并解析 CPU 架构、逻辑 CPU 数和物理拓扑字段。
    参数：无。
    返回值：dict，key 为 lscpu 小写字段名，value 为字段文本；命令失败时返回空字典。
    """
    return parse_lscpu_fields(run_command(["/bin/sh", "-c", "LC_ALL=C lscpu"]))


def read_lscpu_positive_int(fields, field_name):
    """读取 lscpu 正整数字段。

    功能：从 lscpu 字段字典中读取指定字段并转换为正整数。
    参数：fields 为 parse_lscpu_fields 返回的字段字典；field_name 为小写字段名。
    返回值：int 或 None，字段存在且为正整数时返回该数值，否则返回 None。
    """
    try:
        value = int(fields.get(field_name, ""))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def read_cpu_architecture(lscpu_fields):
    """读取主机 CPU 架构。

    功能：从 lscpu Architecture 字段读取 CPU 架构，并归一化为后端使用的架构名称。
    参数：lscpu_fields 为 read_lscpu_fields 返回的字段字典。
    返回值：str 或 None，成功时返回 amd64、arm64、386、arm 或原始小写架构名。
    """
    machine_arch = lscpu_fields.get("architecture")
    if not machine_arch:
        return None
    normalized_arch = machine_arch.strip().lower()
    arch_aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "i386": "386",
        "i486": "386",
        "i586": "386",
        "i686": "386",
        "armv7l": "arm",
        "armv6l": "arm",
    }
    return arch_aliases.get(normalized_arch, normalized_arch)


def read_cpu_thread_count(lscpu_fields):
    """读取 CPU 线程数。

    功能：从 lscpu CPU(s) 字段读取逻辑 CPU 数，作为 CPU 线程数。
    参数：lscpu_fields 为 read_lscpu_fields 返回的字段字典。
    返回值：int 或 None，字段存在且合法时返回线程数，否则返回 None。
    """
    return read_lscpu_positive_int(lscpu_fields, "cpu(s)")


def read_cpu_core_count(lscpu_fields):
    """读取 CPU 物理核心数。

    功能：优先使用 lscpu Socket(s) 与 Core(s) per socket 计算物理核心数。
    参数：lscpu_fields 为 read_lscpu_fields 返回的字段字典。
    返回值：int 或 None，拓扑字段合法时返回物理核心数；缺失时回退为 CPU 线程数。
    """
    socket_count = read_lscpu_positive_int(lscpu_fields, "socket(s)")
    cores_per_socket = read_lscpu_positive_int(lscpu_fields, "core(s) per socket")
    if socket_count is not None and cores_per_socket is not None:
        return socket_count * cores_per_socket
    return read_cpu_thread_count(lscpu_fields)


def parse_proc_net_dev_interfaces(content):
    """从 /proc/net/dev 内容解析网络接口名称。"""
    interfaces = []
    if not content:
        return interfaces
    for line in content.splitlines():
        if ":" not in line:
            continue
        interface_name = line.split(":", 1)[0].strip()
        if interface_name:
            interfaces.append(interface_name)
    return interfaces


def normalize_mac_address(mac_address):
    """规范化 MAC 地址文本。"""
    normalized = (mac_address or "").strip().lower()
    if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", normalized):
        return None
    if normalized == "00:00:00:00:00:00":
        return None
    return normalized


def read_network_interface_mac(interface_name):
    """读取单个网络接口的 MAC 地址。"""
    address_path = os.path.join(SYS_CLASS_NET_PATH, interface_name, "address")
    return normalize_mac_address(read_text_file(address_path))


def collect_network_interface_mac_addresses():
    """采集可用于在线主机匹配的网络接口 MAC 地址。

    功能：遍历 /proc/net/dev 中的 iface，排除 lo、docker0 和无效 MAC，并对结果去重。
    参数：无。
    返回值：list[str]，按接口出现顺序返回过滤、规范化并去重后的 MAC 地址。
    """
    content = read_text_file(PROC_NET_DEV_PATH)
    mac_addresses = []
    seen_mac_addresses = set()
    for interface_name in parse_proc_net_dev_interfaces(content):
        if interface_name in IGNORED_PUBLIC_IP_INTERFACE_NAMES:
            continue
        mac_address = read_network_interface_mac(interface_name)
        if not mac_address or mac_address in seen_mac_addresses:
            continue
        seen_mac_addresses.add(mac_address)
        mac_addresses.append(mac_address)
    return mac_addresses


def collect_interface_ipv6_addresses(interface_name):
    """读取单个网络接口的 IPv6 地址列表。

    功能：解析 /proc/net/if_inet6，返回指定 iface 的 IPv6 地址文本。
    参数：interface_name 为网络接口名称。
    返回值：list[str]，读取失败或无地址时返回空列表。
    """
    content = read_text_file("/proc/net/if_inet6")
    if not content:
        return []
    addresses = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[-1] != interface_name:
            continue
        raw_address = parts[0]
        if len(raw_address) != 32:
            continue
        chunks = [raw_address[index : index + 4] for index in range(0, 32, 4)]
        try:
            packed_address = socket.inet_pton(socket.AF_INET6, ":".join(chunks))
            addresses.append(socket.inet_ntop(socket.AF_INET6, packed_address))
        except (AttributeError, socket.error):
            addresses.append(":".join(chunks))
    return addresses


def read_public_ip_with_interface(interface_name, ip_version):
    """按指定网络接口读取公网 IP。

    功能：调用公网 IP 查询接口，并通过 curl --interface 绑定 iface 查询公网 IPv4 或 IPv6。
    参数：interface_name 为网络接口名称；ip_version 为 4 或 6。
    返回值：str 或 None，成功时返回公网 IP 文本。
    """
    if interface_name in IGNORED_PUBLIC_IP_INTERFACE_NAMES:
        return None
    curl_flag = "-4" if ip_version == 4 else "-6"
    for lookup_url in PUBLIC_IP_LOOKUP_URLS.get(ip_version, ()):
        public_ip = run_command(
            [
                "curl",
                curl_flag,
                "--interface",
                interface_name,
                "-s",
                "--max-time",
                str(PUBLIC_IP_LOOKUP_TIMEOUT_SECONDS),
                lookup_url,
            ]
        )
        if not public_ip:
            continue
        public_ip = public_ip.strip()
        try:
            socket.inet_pton(
                socket.AF_INET if ip_version == 4 else socket.AF_INET6, public_ip
            )
        except (AttributeError, socket.error):
            continue
        return public_ip
    return None


def collect_network_interfaces():
    """采集主机网络接口明细。

    功能：遍历 /proc/net/dev 中的 iface，排除 lo、docker0 和无有效 MAC 的接口，
        读取接口名称、MAC 和可获取的公网 IP。
    参数：无。
    返回值：list[dict]，每项包含 name、macAddress、ipv4Addresses、ipv6Addresses、
        publicIpv4 和 publicIpv6 字段。
    """
    content = read_text_file(PROC_NET_DEV_PATH)
    interfaces = []
    for interface_name in parse_proc_net_dev_interfaces(content):
        if interface_name in IGNORED_PUBLIC_IP_INTERFACE_NAMES:
            continue
        mac_address = read_network_interface_mac(interface_name)
        if not mac_address:
            continue
        interfaces.append(
            {
                "name": interface_name,
                "macAddress": mac_address,
                "ipv4Addresses": [],
                "ipv6Addresses": collect_interface_ipv6_addresses(interface_name),
                "publicIpv4": read_public_ip_with_interface(interface_name, 4),
                "publicIpv6": read_public_ip_with_interface(interface_name, 6),
            }
        )
    return interfaces


def collect_public_ipv4():
    """采集主机公网 IPv4。

    功能：使用配置的首个 IPv4 查询地址和超时时间执行 curl，查询主机公网 IPv4。
    参数：无。
    返回值：str 或 None，成功时返回公网 IPv4，未获取到时返回 None。
    """
    lookup_urls = PUBLIC_IP_LOOKUP_URLS.get(4, ())
    if not lookup_urls:
        return None
    return run_command(
        [
            "curl",
            "-4",
            "--max-time",
            str(PUBLIC_IP_LOOKUP_TIMEOUT_SECONDS),
            lookup_urls[0],
        ]
    )


def collect_system_info():
    """采集后端要求的 CentOS7 主机系统信息。

    功能：汇总系统镜像、内存、磁盘、CPU、MAC 地址和公网 IPv4，生成后端可持久化的数据。
    参数：无。
    返回值：dict，包含 collect_system_info 指令要求的系统信息字段。
    """
    lscpu_fields = read_lscpu_fields()
    disk_info = collect_disk_info()
    return {
        "osImageVersion": read_os_image_version(),
        "totalMemory": read_total_memory_bytes(),
        "usedMemory": read_used_memory_bytes(),
        "ssdTotalDisk": disk_info.get("ssdTotalDisk"),
        "hddTotalDisk": disk_info.get("hddTotalDisk"),
        "ssdCount": disk_info.get("ssdCount"),
        "hddCount": disk_info.get("hddCount"),
        "diskDetails": disk_info.get("diskDetails", []),
        "cpuArchitecture": read_cpu_architecture(lscpu_fields),
        "cpuThreads": read_cpu_thread_count(lscpu_fields),
        "cpuCores": read_cpu_core_count(lscpu_fields),
        "macAddresses": collect_network_interface_mac_addresses(),
        "publicIpv4": collect_public_ipv4(),
    }


def collect_acceptance_info():
    """采集设备验收单信息。

    功能：采集验收单导出所需的每个网络接口 iface、MAC、内网 IP、公网 IP 和 SSH 端口信息。
    参数：无。
    返回值：dict，包含 networkInterfaces 和 sshPort 字段。
    """
    return {
        "networkInterfaces": collect_network_interfaces(),
        "sshPort": read_ssh_port(),
    }


def build_websocket_url(server_url, device_id, machine_id):
    """根据后端服务地址构造设备 WebSocket URL。

    功能：将服务地址转换为 WebSocket 地址，并附加 machine-id 与 v2 标识查询参数。
    参数：server_url 为后端基础地址，device_id 为设备标识，machine_id 为机器标识。
    返回值：str，可用于建立连接的 WebSocket URL。
    """
    parsed_url = urlparse(server_url)
    if not parsed_url.scheme or not parsed_url.netloc:
        raise ValueError("server_url must include scheme and host")
    if parsed_url.scheme in ("http", "ws"):
        scheme = "ws"
    elif parsed_url.scheme in ("https", "wss"):
        scheme = "wss"
    else:
        raise ValueError("server_url scheme must be http, https, ws or wss")
    base_path = parsed_url.path.rstrip("/")
    if base_path.endswith("/api"):
        api_path = base_path
    else:
        api_path = base_path + "/api"
    return "%s://%s%s/device-connections/%s/ws?machine-id=%s&id-version=%s" % (
        scheme,
        parsed_url.netloc,
        api_path,
        device_id,
        quote(machine_id, safe=""),
        quote(DEVICE_ID_VERSION, safe=""),
    )


def parse_websocket_url(websocket_url):
    """解析 WebSocket URL。"""
    parsed_url = urlparse(websocket_url)
    if parsed_url.scheme not in ("ws", "wss"):
        raise ValueError("websocket url scheme must be ws or wss")
    if not parsed_url.hostname:
        raise ValueError("websocket url missing hostname")
    port = parsed_url.port
    if port is None:
        port = 443 if parsed_url.scheme == "wss" else 80
    path = parsed_url.path or "/"
    if parsed_url.query:
        path += "?" + parsed_url.query
    return parsed_url.scheme, parsed_url.hostname, port, path


def create_socket_connection(scheme, host, port, timeout_seconds):
    """创建 TCP 或 TLS socket 连接。"""
    raw_socket = socket.create_connection((host, port), timeout_seconds)
    raw_socket.settimeout(timeout_seconds)
    if scheme == "wss":
        return ssl.wrap_socket(raw_socket)
    return raw_socket


def read_http_headers(sock):
    """读取 WebSocket 握手响应头。"""
    data = ""
    while "\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("empty websocket handshake response")
        data += chunk
        if len(data) > 65536:
            raise RuntimeError("websocket handshake response header too large")
    return data.split("\r\n\r\n", 1)[0]


def parse_header_map(header_text):
    """解析 HTTP 响应头为大小写无关的字典。"""
    headers = {}
    for line in header_text.split("\r\n")[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return headers


def perform_websocket_handshake(sock, host, port, path):
    """执行 WebSocket 客户端握手。"""
    nonce = base64.b64encode(os.urandom(16)).decode("ascii")
    host_header = host if port in (80, 443) else "%s:%s" % (host, port)
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ) % (path, host_header, nonce)
    if isinstance(request, TEXT_TYPE):
        request = request.encode("ascii")
    sock.sendall(request)
    header_text = read_http_headers(sock)
    status_line = header_text.split("\r\n", 1)[0]
    if " 101 " not in status_line:
        raise RuntimeError("websocket handshake failed: %s" % status_line)
    headers = parse_header_map(header_text)
    expected_accept = base64.b64encode(
        hashlib.sha1((nonce + WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    actual_accept = headers.get("sec-websocket-accept")
    if actual_accept != expected_accept:
        raise RuntimeError("websocket Sec-WebSocket-Accept validation failed")


def recv_exact(sock, length):
    """从 socket 精确读取指定长度字节。"""
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("websocket connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return "".join(chunks)


def mask_payload(payload, mask_key):
    """按 WebSocket 协议 mask 或 unmask payload。"""
    return "".join(
        chr(ord(payload[index]) ^ ord(mask_key[index % 4]))
        for index in range(len(payload))
    )


def send_websocket_frame(sock, opcode, payload):
    """发送 WebSocket 帧。"""
    if isinstance(payload, TEXT_TYPE):
        payload = payload.encode("utf-8")
    payload_length = len(payload)
    first_byte = 0x80 | opcode
    mask_bit = 0x80
    if payload_length < 126:
        header = struct.pack("!BB", first_byte, mask_bit | payload_length)
    elif payload_length <= 0xFFFF:
        header = struct.pack("!BBH", first_byte, mask_bit | 126, payload_length)
    else:
        header = struct.pack("!BBQ", first_byte, mask_bit | 127, payload_length)
    mask_key = os.urandom(4)
    sock.sendall(header + mask_key + mask_payload(payload, mask_key))


def recv_websocket_frame(sock):
    """接收单个 WebSocket 帧。"""
    first_two = recv_exact(sock, 2)
    byte_one, byte_two = struct.unpack("!BB", first_two)
    fin = (byte_one & 0x80) != 0
    opcode = byte_one & 0x0F
    masked = (byte_two & 0x80) != 0
    payload_length = byte_two & 0x7F
    if payload_length == 126:
        payload_length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif payload_length == 127:
        payload_length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask_key = recv_exact(sock, 4) if masked else None
    payload = recv_exact(sock, payload_length) if payload_length else ""
    if mask_key:
        payload = mask_payload(payload, mask_key)
    return fin, opcode, payload


def recv_websocket_message(sock):
    """接收一条完整 WebSocket 文本消息。"""
    fragments = []
    expected_continuation = False
    while True:
        fin, opcode, payload = recv_websocket_frame(sock)
        if opcode == OPCODE_CLOSE:
            return None
        if opcode == OPCODE_PING:
            send_websocket_frame(sock, OPCODE_PONG, payload)
            continue
        if opcode == OPCODE_PONG:
            continue
        if opcode == OPCODE_TEXT:
            fragments.append(payload)
            expected_continuation = not fin
        elif opcode == OPCODE_CONTINUATION and expected_continuation:
            fragments.append(payload)
            expected_continuation = not fin
        else:
            raise RuntimeError("unsupported websocket frame opcode=%s" % opcode)
        if fin:
            return "".join(fragments).decode("utf-8", "ignore")


def send_json_message(sock, payload):
    """通过 WebSocket 发送 JSON 文本消息。"""
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if isinstance(message, TEXT_TYPE):
        message = message.encode("utf-8")
    send_websocket_frame(sock, OPCODE_TEXT, message)


def build_command_response(command_message):
    """根据后端指令构造响应消息。"""
    request_id = command_message.get("requestId")
    command = command_message.get("command")
    response = {
        "type": "command_result",
        "requestId": request_id,
        "success": True,
        "data": {},
    }
    try:
        if command == "collect_system_info":
            response["data"] = collect_system_info()
        elif command == "collect_acceptance_info":
            response["data"] = collect_acceptance_info()
        elif command == "enable_upload_limit":
            response["data"] = execute_upload_limit_command(
                True,
                command_message.get("start"),
                command_message.get("end"),
                command_message.get("rate"),
            )
        elif command == "disable_upload_limit":
            response["data"] = execute_upload_limit_command(False)
        elif command == "bandwidth_test":
            response["data"] = execute_bandwidth_test(
                command_message.get("serverIp"),
                command_message.get("durationSeconds"),
            )
        else:
            response["success"] = False
            response["error"] = "unsupported command: %s" % command
    except Exception as exc:
        response["success"] = False
        response["error"] = str(exc)
        response["data"] = {}
    return response


class TerminalSession(object):
    """设备端交互终端会话。"""

    def __init__(self, terminal_id, cols, rows):
        """初始化终端会话对象。

        功能：保存终端会话标识、初始尺寸和子进程信息。
        参数：terminal_id 为后端分配的终端会话标识；cols 和 rows 为终端列数和行数。
        返回值：None。
        """
        self.terminal_id = terminal_id
        self.cols = cols
        self.rows = rows
        self.pid = None
        self.fd = None

    def start(self):
        """启动本机 pty shell。

        功能：创建伪终端并在子进程中启动当前 SHELL 或 /bin/bash。
        参数：无。
        返回值：None。
        """
        import pty

        pid, fd = pty.fork()
        if pid == 0:
            shell = os.environ.get("SHELL") or "/bin/bash"
            os.environ["TERM"] = "xterm-256color"
            os.execvp(shell, [shell])
        self.pid = pid
        self.fd = fd
        self.resize(self.cols, self.rows)

    def resize(self, cols, rows):
        """调整 pty 窗口尺寸。

        功能：通过 TIOCSWINSZ 修改当前终端会话的行列数。
        参数：cols 为列数；rows 为行数。
        返回值：None。
        """
        if self.fd is None:
            return
        try:
            import fcntl
            import termios

            size = struct.pack("HHHH", int(rows), int(cols), 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)
        except Exception:
            logger.warning("failed to resize terminal session %s", self.terminal_id)

    def write(self, data):
        """写入终端输入。

        功能：把浏览器输入内容写入当前 pty。
        参数：data 为待写入的文本。
        返回值：None。
        """
        if self.fd is None:
            return
        if isinstance(data, TEXT_TYPE):
            data = data.encode("utf-8")
        os.write(self.fd, data)

    def read(self):
        """读取终端输出。

        功能：从当前 pty 读取一段输出并解码为文本。
        参数：无。
        返回值：str，终端输出文本。
        """
        if self.fd is None:
            return ""
        data = os.read(self.fd, TERMINAL_OUTPUT_CHUNK_SIZE)
        if not data:
            return ""
        return data.decode("utf-8", "ignore")

    def close(self):
        """关闭终端会话。

        功能：关闭 pty fd 并终止对应 shell 子进程。
        参数：无。
        返回值：None。
        """
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if self.pid is not None:
            try:
                os.kill(self.pid, 15)
            except OSError:
                pass
            self.pid = None


def parse_terminal_size(value, default_value):
    """解析终端尺寸。

    功能：将后端传入的行列值转换为正整数，非法时使用默认值。
    参数：value 为待解析尺寸；default_value 为默认尺寸。
    返回值：int，解析后的终端尺寸。
    """
    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return default_value
    if numeric_value <= 0:
        return default_value
    return numeric_value


def build_terminal_message(message_type, terminal_id, **kwargs):
    """构造终端上报消息。

    功能：按后端终端协议构造统一 JSON 消息字典。
    参数：message_type 为消息类型；terminal_id 为终端会话标识；kwargs 为附加字段。
    返回值：dict，可直接通过 WebSocket 上报的消息。
    """
    payload = {"type": message_type, "terminalId": terminal_id}
    payload.update(kwargs)
    return payload


def handle_terminal_message(sock, payload, terminal_sessions):
    """处理后端下发的终端控制消息。

    功能：根据 terminal_start/input/resize/close 操作本机 pty 会话并上报结果。
    参数：sock 为设备 WebSocket socket；payload 为后端消息；terminal_sessions 为会话字典。
    返回值：None。
    """
    message_type = payload.get("type")
    terminal_id = payload.get("terminalId")
    if not isinstance(terminal_id, TEXT_TYPE) or not terminal_id:
        return
    if message_type == "terminal_start":
        if terminal_id in terminal_sessions:
            return
        session = TerminalSession(
            terminal_id,
            parse_terminal_size(payload.get("cols"), DEFAULT_TERMINAL_COLUMNS),
            parse_terminal_size(payload.get("rows"), DEFAULT_TERMINAL_ROWS),
        )
        try:
            session.start()
            terminal_sessions[terminal_id] = session
        except Exception as exc:
            send_json_message(
                sock,
                build_terminal_message(
                    "terminal_error", terminal_id, error=str(exc)
                ),
            )
    elif message_type == "terminal_input":
        session = terminal_sessions.get(terminal_id)
        if session is not None:
            session.write(payload.get("data") or "")
    elif message_type == "terminal_resize":
        session = terminal_sessions.get(terminal_id)
        if session is not None:
            session.resize(
                parse_terminal_size(payload.get("cols"), DEFAULT_TERMINAL_COLUMNS),
                parse_terminal_size(payload.get("rows"), DEFAULT_TERMINAL_ROWS),
            )
    elif message_type == "terminal_close":
        session = terminal_sessions.pop(terminal_id, None)
        if session is not None:
            session.close()
            send_json_message(
                sock,
                build_terminal_message("terminal_closed", terminal_id),
            )


def flush_terminal_outputs(sock, terminal_sessions):
    """读取并上报所有可读终端输出。

    功能：遍历当前 pty fd，读取可用输出并通过设备 WebSocket 推送给后端。
    参数：sock 为设备 WebSocket socket；terminal_sessions 为会话字典。
    返回值：None。
    """
    fd_to_session = {
        session.fd: session
        for session in terminal_sessions.values()
        if session.fd is not None
    }
    if not fd_to_session:
        return
    readable_fds, _writable, _errors = select.select(
        list(fd_to_session.keys()), [], [], 0
    )
    for fd in readable_fds:
        session = fd_to_session.get(fd)
        if session is None:
            continue
        try:
            output = session.read()
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            output = ""
        if output:
            send_json_message(
                sock,
                build_terminal_message(
                    "terminal_output", session.terminal_id, data=output
                ),
            )
            continue
        terminal_sessions.pop(session.terminal_id, None)
        session.close()
        send_json_message(
            sock,
            build_terminal_message("terminal_closed", session.terminal_id),
        )


def handle_websocket_session(websocket_url, timeout_seconds):
    """建立并处理一次 WebSocket 会话。"""
    scheme, host, port, path = parse_websocket_url(websocket_url)
    sock = create_socket_connection(scheme, host, port, timeout_seconds)
    terminal_sessions = {}
    try:
        perform_websocket_handshake(sock, host, port, path)
        logger.info("websocket connected")
        while True:
            flush_terminal_outputs(sock, terminal_sessions)
            read_targets = [sock] + [
                session.fd
                for session in terminal_sessions.values()
                if session.fd is not None
            ]
            readable, _writable, _errors = select.select(
                read_targets, [], [], timeout_seconds
            )
            if not readable:
                send_websocket_frame(sock, OPCODE_PING, "")
                continue
            if sock not in readable:
                continue
            message = recv_websocket_message(sock)
            if message is None:
                logger.info("backend closed websocket connection")
                return
            payload = json.loads(message)
            if payload.get("type") == "command":
                send_json_message(sock, build_command_response(payload))
            elif payload.get("type") in (
                "terminal_start",
                "terminal_input",
                "terminal_resize",
                "terminal_close",
            ):
                handle_terminal_message(sock, payload, terminal_sessions)
    finally:
        for session in list(terminal_sessions.values()):
            session.close()
        try:
            send_websocket_frame(sock, OPCODE_CLOSE, "")
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def parse_args(argv):
    """解析命令行参数。

    功能：读取后端地址、重连间隔和 socket 超时配置。
    参数：argv 为不包含脚本名的命令行参数列表。
    返回值：argparse.Namespace，包含解析后的脚本运行配置。
    """
    parser = argparse.ArgumentParser(
        description="CentOS7 device WebSocket agent for pcdn-tx backend."
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Backend base URL, for example http://127.0.0.1:8001.",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=int,
        default=DEFAULT_RECONNECT_DELAY,
        help="Reconnect delay in seconds.",
    )
    parser.add_argument(
        "--socket-timeout",
        type=int,
        default=DEFAULT_SOCKET_TIMEOUT,
        help="Socket timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv):
    """脚本入口函数。

    功能：生成设备标识并按配置循环建立 WebSocket 连接。
    参数：argv 为不包含脚本名的命令行参数列表。
    返回值：int，进程退出码。
    """
    args = parse_args(argv)
    try:
        machine_id = read_machine_id()
        device_id = generate_device_id(machine_id)
        websocket_url = build_websocket_url(
            args.server_url,
            device_id,
            machine_id,
        )
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    logger.info("device_id=%s", device_id)
    logger.info("websocket_url=%s", websocket_url)
    while True:
        try:
            handle_websocket_session(websocket_url, args.socket_timeout)
        except KeyboardInterrupt:
            logger.info("interrupted, exit")
            return 0
        except Exception as exc:
            logger.warning(
                "websocket session error: %s, reconnect after %s seconds",
                exc,
                args.reconnect_delay,
            )
            time.sleep(max(1, args.reconnect_delay))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
