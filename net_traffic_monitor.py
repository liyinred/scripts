#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: wenhao
"""
网卡流量统计程序（Python 2.7 / CentOS 7 兼容）。
解析 /proc/net/dev，返回各网卡的收发字节数。

功能：
1. 每整五分钟统计各网卡窗口平均上下行速率。
2. 将统计结果写入本地 SQLite 数据库。
3. 上报小合云系统流量接口（可选）。
"""

import sqlite3
import time
import sys
import logging
import socket
import struct
import math
import os
import argparse
import json
import random
import ast
import re
import calendar

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import urllib2
except ImportError:
    urllib2 = None
    import urllib.request as urllib_request


# ── 日志配置 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# ── 常量 ─────────────────────────────────────────────────────────────────────
DB_PATH = 'net_traffic.db'          # SQLite 文件名模板，按北京时间自然月生成实际文件
DB_DIR = 'net_traffic_data'         # 月度 SQLite 文件保存目录
API_LOG_DIR = 'api_log'             # API 请求响应日志目录
INTERVAL = 300                      # 统计周期：300 秒（整五分钟）
MIN_STARTUP_WINDOW_REMAINING_SECONDS = 5.0  # 首次窗口允许采集的最短剩余时间
NET_DEV_PATH = '/proc/net/dev'      # Linux 网络统计文件
NET_VLAN_CONFIG_PATH = '/proc/net/vlan/config'  # Linux VLAN 与父接口映射文件
SIOCGIFHWADDR = 0x8927              # ioctl 获取 MAC 地址的请求码
DEFAULT_MAC = '00:00:00:00:00:00'   # MAC 获取失败时的兜底值
BEIJING_UTC_OFFSET = 8 * 60 * 60    # 北京时间固定为 UTC+8
TRAFFIC_UPLOAD_URL = None
TRAFFIC_UPLOAD_API_KEY = 'xiaohe666'
TRAFFIC_UPLOAD_TIMEOUT = 20        # 流量上报接口请求超时时间，单位：秒
TRAFFIC_UPLOAD_MAX_RETRIES = 5     # 流量上报失败后的最大重试次数
TRAFFIC_UPLOAD_RETRY_BASE_DELAY = 1.0  # 流量上报重试基础等待时间，单位：秒
TRAFFIC_UPLOAD_RETRY_MAX_DELAY = 16.0  # 流量上报单次重试最大等待时间，单位：秒
TRAFFIC_UPLOAD_SPREAD_WINDOW = 10.0  # 窗口结束后上报分散时间范围，单位：秒
TRAFFIC_UPLOAD_BUCKET_SECONDS = 0.1  # 上报分散桶大小，单位：秒
TRAFFIC_UPLOAD_IGNORED_IFACES = set(['eth1'])  # API 上报时忽略的接口名称
COMPUTE_SERVER_TRAFFIC_UPLOAD_PATH_SUFFIX = 'compute-server-upload'
COMPUTE_SERVER_TRAFFIC_IFACE = 'eth0'
API_ERROR_LOG_SUFFIX = '_error.log'  # API 异常日志文件后缀
API_LOG_SUFFIX = '.log'              # API 普通日志文件后缀

TABLE_NAME = 'net_speed'
try:
    LONG_TYPE = long
except NameError:
    LONG_TYPE = int

EXPECTED_COLUMNS = [
    ('time_id', 'BIGINT'),
    ('iface', 'TEXT'),
    ('mac', 'TEXT'),
    ('rx_speed', 'BIGINT'),
    ('tx_speed', 'BIGINT')
]

MAX_NET_COUNTER_VALUE = (1 << 64) - 1  # Linux 网络累计计数器的 unsigned 64-bit 上限
MAX_TRAFFIC_SPEED_BPS = 10000 * 1000 * 1000 * 1000  # 入库和上报速率上限：10000 Gbps

INITIALIZED_DB_PATHS = set()


def beijing_log_time_converter(timestamp):
    """将日志时间戳转换为北京时间元组。"""
    return time.gmtime(timestamp + BEIJING_UTC_OFFSET)


# Python 2.7 中普通函数作为类属性会被绑定为实例方法，需显式声明为静态方法。
logging.Formatter.converter = staticmethod(beijing_log_time_converter)


# ── 启动参数 ────────────────────────────────────────────────────────────────

def parse_bool(value):
    """将命令行字符串解析为布尔值。"""
    if isinstance(value, bool):
        return value

    value_text = str(value).strip().lower()
    if value_text in ('true', '1', 'yes', 'y', 'on'):
        return True
    if value_text in ('false', '0', 'no', 'n', 'off'):
        return False

    raise argparse.ArgumentTypeError(
        "traffic_api_post 仅支持 true/false、1/0、yes/no"
    )


def parse_args(argv):
    """解析程序启动参数。

    参数:
        argv: 命令行参数列表，不包含程序名。

    返回:
        argparse.Namespace: 解析后的启动参数对象。
    """
    parser = argparse.ArgumentParser(
        description='按整五分钟窗口统计网卡流量并写入本地数据库。'
    )
    parser.add_argument(
        '--TRAFFIC_UPLOAD_URL',
        '--traffic_upload_url',
        dest='traffic_upload_url',
        required=True,
        help='流量上报接口 URL，必须填写。'
    )
    parser.add_argument(
        '--traffic_api_post',
        type=parse_bool,
        default=False,
        help='是否将每个五分钟窗口的数据 POST 到流量接口，默认 false。'
    )
    parser.add_argument(
        '--ip',
        default=None,
        help='算力服务器流量上报接口使用的当前主机 IP。'
    )
    return parser.parse_args(argv)


def is_compute_server_upload_url(traffic_upload_url):
    """根据上报 URL 判断当前是否使用算力服务器流量上报接口。

    参数:
        traffic_upload_url: 启动参数传入的流量上报接口 URL。

    返回:
        bool: URL 尾部路径为 compute-server-upload 时返回 True，否则返回 False。
    """
    if not traffic_upload_url:
        return False
    normalized_url = str(traffic_upload_url).split('?', 1)[0].rstrip('/')
    return normalized_url.endswith('/' + COMPUTE_SERVER_TRAFFIC_UPLOAD_PATH_SUFFIX)


def validate_upload_api_args(traffic_upload_url, ip_address):
    """校验上报接口相关启动参数。

    参数:
        traffic_upload_url: 启动参数传入的流量上报接口 URL。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        None: 校验通过后不返回业务数据。
    """
    if is_compute_server_upload_url(traffic_upload_url) and not ip_address:
        raise ValueError("使用 compute-server-upload 上报接口时必须传入 --ip 参数。")


# ── 北京时间与月度数据库路径 ────────────────────────────────────────────────

def get_beijing_time_tuple(timestamp):
    """将 Unix 时间戳转换为北京时间的 time.struct_time。"""
    return time.gmtime(timestamp + BEIJING_UTC_OFFSET)


def format_beijing_time(timestamp):
    """格式化 Unix 时间戳对应的北京时间。"""
    return time.strftime('%Y-%m-%d %H:%M:%S', get_beijing_time_tuple(timestamp))


def get_monthly_db_path(timestamp):
    """根据北京时间自然月生成 SQLite 数据库文件路径。"""
    beijing_time = get_beijing_time_tuple(timestamp)
    db_name = os.path.basename(DB_PATH)
    db_stem, db_ext = os.path.splitext(db_name)

    if not db_ext:
        db_ext = '.db'

    monthly_name = '{0}_{1:04d}{2:02d}{3}'.format(
        db_stem,
        beijing_time.tm_year,
        beijing_time.tm_mon,
        db_ext
    )

    return os.path.join(DB_DIR, monthly_name)


def ensure_dir(dir_path, dir_desc):
    """确保指定目录存在。"""
    if os.path.isdir(dir_path):
        return

    if os.path.exists(dir_path):
        raise OSError("{0}已存在但不是目录: {1}".format(dir_desc, dir_path))

    os.makedirs(dir_path)


def ensure_monthly_db(timestamp):
    """确保指定时间戳所属北京时间自然月的数据库可用。"""
    db_path = get_monthly_db_path(timestamp)
    if db_path not in INITIALIZED_DB_PATHS:
        ensure_dir(DB_DIR, '数据库保存目录')
        init_db(db_path)
        INITIALIZED_DB_PATHS.add(db_path)

    return db_path


def get_monthly_api_log_path(timestamp, suffix=''):
    """根据北京时间自然月生成 API 日志文件路径。

    参数:
        timestamp: 统计窗口开始 Unix 时间戳。
        suffix: API 日志文件名月份后的可选后缀。

    返回:
        str: API 日志文件路径。
    """
    beijing_time = get_beijing_time_tuple(timestamp)
    log_name = 'traffic_api_{0:04d}{1:02d}{2}.log'.format(
        beijing_time.tm_year,
        beijing_time.tm_mon,
        suffix
    )
    return os.path.join(API_LOG_DIR, log_name)


def get_monthly_api_error_log_path(timestamp):
    """根据北京时间自然月生成 API 异常日志文件路径。

    参数:
        timestamp: 统计窗口开始 Unix 时间戳。

    返回:
        str: API 异常日志文件路径。
    """
    return get_monthly_api_log_path(timestamp, '_error')


def filter_upload_records(records):
    """过滤不参与 API 上报的网卡记录。

    参数:
        records: 当前 time_id 对应的全部入库记录列表。

    返回:
        list: 排除 TRAFFIC_UPLOAD_IGNORED_IFACES 后的记录列表。
    """
    return [
        record
        for record in records
        if record[1] not in TRAFFIC_UPLOAD_IGNORED_IFACES
    ]


def build_traffic_payload(records):
    """将数据库记录转换为流量上报 JSON 数组对应的 Python 数据结构。

    参数:
        records: 已过滤的待上报记录列表，每项包含 time_id、iface、mac、rx_speed、tx_speed。

    返回:
        list: 可序列化为 JSON 数组的流量上报数据。
    """
    payload = []

    for record in records:
        payload.append({
            'time_id': LONG_TYPE(record[0]),
            'mac': record[2],
            'rx_speed': LONG_TYPE(record[3]),
            'tx_speed': LONG_TYPE(record[4])
        })

    return payload


def build_compute_server_traffic_payload(records, ip_address):
    """将 eth0 数据库记录转换为算力服务器流量上报 JSON 数组。

    参数:
        records: 待上报记录列表，每项包含 time_id、iface、mac、rx_speed、tx_speed。
        ip_address: 当前主机 IP，将写入上报数据的 ip 字段。

    返回:
        list: 可序列化为 JSON 数组的算力服务器流量上报数据。
    """
    payload = []
    for record in records:
        payload.append({
            'time_id': LONG_TYPE(record[0]),
            'ip': ip_address,
            'rx_speed': LONG_TYPE(record[3]),
            'tx_speed': LONG_TYPE(record[4])
        })
    return payload


def decode_response_body(response_body):
    """将 HTTP 响应体转换为日志可写入的文本。"""
    if isinstance(response_body, str):
        return response_body

    try:
        return response_body.decode('utf-8')
    except UnicodeDecodeError:
        return response_body.decode('utf-8', 'replace')


def append_api_log(timestamp, status_code, response_body):
    """将一次 API 请求响应内容追加写入北京时间月度日志文件。"""
    ensure_dir(API_LOG_DIR, 'API 日志目录')
    log_path = get_monthly_api_log_path(timestamp)
    log_time = format_beijing_time(time.time())
    window_time = format_beijing_time(timestamp)
    body_text = decode_response_body(response_body)

    with open(log_path, 'a') as file_obj:
        file_obj.write(
            '[{0}] window_time={1} status={2}\n{3}\n\n'.format(
                log_time,
                window_time,
                status_code,
                body_text
            )
        )


def format_records_for_api_error_log(records):
    """将入库记录格式化为 API 异常日志文本。

    参数:
        records: 当前 time_id 对应的全部入库记录列表。

    返回:
        str: 可直接写入日志文件的记录文本，每行一条 tuple。
    """
    if not records:
        return '(无入库数据)'

    return '\n'.join(repr(record) for record in records)


def append_api_error_log(timestamp, error_message, records, response_body=None):
    """将一次 API 上报异常及对应入库数据追加写入月度异常日志文件。

    参数:
        timestamp: 统计窗口开始 Unix 时间戳，也是记录的 time_id。
        error_message: 上报异常摘要。
        records: 当前 time_id 对应的全部入库记录列表。
        response_body: 可选 HTTP 响应体或异常详情。

    返回:
        None: 写入完成后不返回业务数据。
    """
    ensure_dir(API_LOG_DIR, 'API 日志目录')
    log_path = get_monthly_api_error_log_path(timestamp)
    log_time = format_beijing_time(time.time())
    window_time = format_beijing_time(timestamp)
    body_text = ''
    if response_body is not None:
        body_text = decode_response_body(response_body)

    with open(log_path, 'a') as file_obj:
        file_obj.write(
            '[{0}] window_time={1} time_id={2} error={3}\n'.format(
                log_time,
                window_time,
                LONG_TYPE(timestamp),
                error_message
            )
        )
        if body_text:
            file_obj.write('response_body:\n{0}\n'.format(body_text))
        file_obj.write('records:\n{0}\n\n'.format(
            format_records_for_api_error_log(records)
        ))


def parse_beijing_time_text(time_text):
    """将北京时间文本转换为 Unix 时间戳。

    参数:
        time_text: 格式为 YYYY-mm-dd HH:MM:SS 的北京时间字符串。

    返回:
        int: 对应的 Unix 时间戳。
    """
    time_tuple = time.strptime(time_text, '%Y-%m-%d %H:%M:%S')
    return LONG_TYPE(calendar.timegm(time_tuple) - BEIJING_UTC_OFFSET)


def parse_log_header_field(header_line, start_marker, end_markers):
    """从日志头部解析指定字段文本。

    参数:
        header_line: 单条日志记录的第一行文本。
        start_marker: 字段起始标记。
        end_markers: 字段后续可能出现的结束标记列表。

    返回:
        str: 去除首尾空白后的字段值。
    """
    start_index = header_line.find(start_marker)
    if start_index < 0:
        raise ValueError("日志头部缺少字段: {0}".format(start_marker.strip()))

    start_index += len(start_marker)
    end_index = len(header_line)
    for end_marker in end_markers:
        marker_index = header_line.find(end_marker, start_index)
        if marker_index >= 0:
            end_index = min(end_index, marker_index)

    return header_line[start_index:end_index].strip()


def parse_api_log_window_start(header_line):
    """从 API 日志头部的 window_time 字段解析窗口开始时间戳。

    参数:
        header_line: 单条 API 日志的第一行文本。

    返回:
        int: window_time 对应的 Unix 时间戳。
    """
    window_time = parse_log_header_field(
        header_line,
        ' window_time=',
        [' time_id=', ' status=', ' error=']
    )
    return parse_beijing_time_text(window_time)


def list_api_log_paths():
    """列出当前目录下全部月度 API 普通日志文件。

    参数:
        无。

    返回:
        list: API 普通日志文件路径列表，按文件名升序排列。
    """
    if not os.path.isdir(API_LOG_DIR):
        return []

    log_paths = []
    for file_name in sorted(os.listdir(API_LOG_DIR)):
        if not file_name.startswith('traffic_api_'):
            continue
        if not file_name.endswith(API_LOG_SUFFIX):
            continue
        if file_name.endswith(API_ERROR_LOG_SUFFIX):
            continue
        log_paths.append(os.path.join(API_LOG_DIR, file_name))

    return log_paths


def list_api_error_log_paths():
    """列出当前目录下全部月度 API 异常日志文件。

    参数:
        无。

    返回:
        list: API 异常日志文件路径列表，按文件名升序排列。
    """
    if not os.path.isdir(API_LOG_DIR):
        return []

    log_paths = []
    for file_name in sorted(os.listdir(API_LOG_DIR)):
        if not file_name.startswith('traffic_api_'):
            continue
        if not file_name.endswith(API_ERROR_LOG_SUFFIX):
            continue
        log_paths.append(os.path.join(API_LOG_DIR, file_name))

    return log_paths


def split_api_error_log_entries(log_text):
    """按异常日志头部切分单条失败上报记录。

    参数:
        log_text: API 异常日志完整文本。

    返回:
        list: 单条异常日志文本列表。
    """
    entries = []
    current_lines = []

    for line in log_text.splitlines():
        is_entry_header = (
            line.startswith('[')
            and ' window_time=' in line
            and ' time_id=' in line
            and ' error=' in line
        )
        if is_entry_header and current_lines:
            entry_text = '\n'.join(current_lines).strip()
            if entry_text:
                entries.append(entry_text)
            current_lines = [line]
            continue

        current_lines.append(line)

    entry_text = '\n'.join(current_lines).strip()
    if entry_text:
        entries.append(entry_text)

    return entries


def split_api_log_entries(log_text):
    """按普通 API 日志头部切分单条请求响应记录。

    参数:
        log_text: API 普通日志完整文本。

    返回:
        list: 单条 API 普通日志文本列表。
    """
    entries = []
    current_lines = []

    for line in log_text.splitlines():
        is_entry_header = (
            line.startswith('[')
            and ' window_time=' in line
            and ' status=' in line
        )
        if is_entry_header and current_lines:
            entry_text = '\n'.join(current_lines).strip()
            if entry_text:
                entries.append(entry_text)
            current_lines = [line]
            continue

        current_lines.append(line)

    entry_text = '\n'.join(current_lines).strip()
    if entry_text:
        entries.append(entry_text)

    return entries


def parse_api_log_status(header_line):
    """从普通 API 日志头部解析 HTTP 状态。

    参数:
        header_line: 单条 API 普通日志的第一行文本。

    返回:
        str: 日志头部记录的 status 字段值。
    """
    return parse_log_header_field(header_line, ' status=', [])


def is_failed_api_log_entry(entry_text):
    """判断普通 API 日志记录是否表示已入库但上报失败。

    参数:
        entry_text: 单条 API 普通日志文本。

    返回:
        bool: status 非 2xx 或为 ERROR 时返回 True，否则返回 False。
    """
    entry_lines = entry_text.splitlines()
    if not entry_lines:
        raise ValueError("API 普通日志记录为空。")

    status = parse_api_log_status(entry_lines[0])
    return not is_success_status(status)


def parse_api_error_log_timestamp(header_line):
    """从异常日志头部解析 time_id。

    参数:
        header_line: 单条异常日志的第一行文本。

    返回:
        int: 异常日志记录对应的 time_id。
    """
    marker = ' time_id='
    start_index = header_line.find(marker)
    if start_index < 0:
        raise ValueError("异常日志头部缺少 time_id。")

    start_index += len(marker)
    end_index = header_line.find(' error=', start_index)
    if end_index < 0:
        end_index = len(header_line)

    return LONG_TYPE(header_line[start_index:end_index])


def parse_api_error_log_records(entry_lines):
    """从单条异常日志中解析已入库记录列表。

    参数:
        entry_lines: 单条异常日志按行切分后的列表。

    返回:
        list: 从 records 段解析出的入库记录列表。
    """
    records_index = None
    for index, line in enumerate(entry_lines):
        if line == 'records:':
            records_index = index

    if records_index is None:
        raise ValueError("异常日志缺少 records 段。")

    records = []
    for line in entry_lines[records_index + 1:]:
        line = line.strip()
        if not line or line == '(无入库数据)':
            continue
        record = ast.literal_eval(re.sub(r'(?<=\d)L\b', '', line))
        records.append(record)

    return records


def parse_api_error_log_entry(entry_text):
    """解析单条 API 异常日志记录。

    参数:
        entry_text: 单条 API 异常日志文本。

    返回:
        tuple: 二元组，依次为 time_id 和 records。
    """
    entry_lines = entry_text.splitlines()
    if not entry_lines:
        raise ValueError("异常日志记录为空。")

    timestamp = parse_api_error_log_timestamp(entry_lines[0])
    records = parse_api_error_log_records(entry_lines)
    return timestamp, records


def rewrite_api_error_log(log_path, entries):
    """用保留的失败记录重写 API 异常日志文件。

    参数:
        log_path: API 异常日志文件路径。
        entries: 仍需保留的异常日志记录文本列表。

    返回:
        None: 写入完成后不返回业务数据。
    """
    content = '\n\n'.join(entries)
    if content:
        content += '\n\n'
    with open(log_path, 'w') as file_obj:
        file_obj.write(content)


def rewrite_api_log_entries(log_path, entries, extra_text=''):
    """用保留记录重写 API 普通日志文件。

    参数:
        log_path: API 普通日志文件路径。
        entries: 仍需保留的普通日志记录文本列表。
        extra_text: 重报过程中追加到文件尾部、需要继续保留的日志文本。

    返回:
        None: 写入完成后不返回业务数据。
    """
    content = '\n\n'.join(entries)
    if content:
        content += '\n\n'
    if extra_text:
        content += extra_text
    with open(log_path, 'w') as file_obj:
        file_obj.write(content)


def retry_api_error_log_entry(
    entry_text,
    log_path,
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """重试单条 API 异常日志中的上报记录。

    参数:
        entry_text: 单条 API 异常日志文本。
        log_path: 当前异常日志文件路径，用于输出诊断日志。
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        bool: 重试成功或无需上报时返回 True，仍需保留异常记录时返回 False。
    """
    try:
        window_start, records = parse_api_error_log_entry(entry_text)
        upload_records = filter_upload_records_by_mode(
            records,
            is_compute_server_upload
        )
        if not upload_records:
            logger.info(
                "历史 API 异常记录无需重报，已全部被接口过滤规则排除: %s",
                log_path
            )
            return True

        success, status_code, _response_body, attempts = post_upload_records_with_log(
            upload_records,
            window_start,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )
        if success:
            logger.info(
                "历史 API 异常记录重报成功，time_id=%d，记录数=%d，请求次数=%d",
                window_start,
                len(upload_records),
                attempts
            )
            return True

        logger.warning(
            "历史 API 异常记录重报仍失败，time_id=%d，status=%s，请求次数=%d",
            window_start,
            status_code,
            attempts
        )
        return False
    except Exception as exc:
        logger.warning("历史 API 异常记录重报失败，文件=%s，原因: %s", log_path, exc)
        return False


def retry_api_log_entry_from_db(
    entry_text,
    log_path,
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """根据普通 API 日志中的 window_time 从 SQLite 查询记录并重报。

    参数:
        entry_text: 单条 API 普通日志文本。
        log_path: 当前普通日志文件路径，用于输出诊断日志。
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        bool: 重报成功或无需上报时返回 True，仍需保留普通日志记录时返回 False。
    """
    try:
        entry_lines = entry_text.splitlines()
        if not entry_lines:
            raise ValueError("API 普通日志记录为空。")
        if not is_failed_api_log_entry(entry_text):
            return False

        window_start = parse_api_log_window_start(entry_lines[0])
        records = load_records_by_time_id(window_start)
        if not records:
            logger.warning(
                "普通 API 日志补偿重报未查询到入库记录，文件=%s，time_id=%d",
                log_path,
                window_start
            )
            return False

        upload_records = filter_upload_records_by_mode(
            records,
            is_compute_server_upload
        )
        if not upload_records:
            logger.info(
                "普通 API 日志补偿重报无需上报，已全部被接口过滤规则排除，time_id=%d",
                window_start
            )
            return True

        success, status_code, _response_body, attempts = post_upload_records_with_log(
            upload_records,
            window_start,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )
        if success:
            logger.info(
                "普通 API 日志补偿重报成功，time_id=%d，记录数=%d，请求次数=%d",
                window_start,
                len(upload_records),
                attempts
            )
            return True

        logger.warning(
            "普通 API 日志补偿重报仍失败，time_id=%d，status=%s，请求次数=%d",
            window_start,
            status_code,
            attempts
        )
        return False
    except Exception as exc:
        logger.warning("普通 API 日志补偿重报失败，文件=%s，原因: %s", log_path, exc)
        return False


def retry_failed_upload_records_from_api_logs(
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """扫描普通 API 日志并补偿重报已入库但上报失败的记录。

    参数:
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        None: 重报流程结束后不返回业务数据。
    """
    try:
        log_paths = list_api_log_paths()
    except Exception as exc:
        logger.warning("读取 API 普通日志目录失败，跳过普通日志补偿重报: %s", exc)
        return

    for log_path in log_paths:
        try:
            with open(log_path, 'r') as file_obj:
                log_text = file_obj.read()
        except Exception as exc:
            logger.warning("读取 API 普通日志失败，文件=%s，原因: %s", log_path, exc)
            continue

        entries = split_api_log_entries(log_text)
        if not entries:
            continue

        remaining_entries = []
        success_count = 0
        for entry_text in entries:
            try:
                should_retry = is_failed_api_log_entry(entry_text)
            except Exception as exc:
                logger.warning(
                    "解析 API 普通日志状态失败，保留原记录，文件=%s，原因: %s",
                    log_path,
                    exc
                )
                remaining_entries.append(entry_text)
                continue

            if not should_retry:
                remaining_entries.append(entry_text)
                continue
            if retry_api_log_entry_from_db(
                entry_text,
                log_path,
                traffic_upload_url,
                is_compute_server_upload,
                ip_address
            ):
                success_count += 1
                continue
            remaining_entries.append(entry_text)

        if success_count <= 0:
            continue

        try:
            extra_text = ''
            with open(log_path, 'r') as file_obj:
                current_text = file_obj.read()
            if current_text.startswith(log_text):
                extra_text = current_text[len(log_text):]
            rewrite_api_log_entries(log_path, remaining_entries, extra_text)
            logger.info(
                "已清理 API 普通日志成功补偿重报记录，文件=%s，清理数=%d，保留数=%d",
                log_path,
                success_count,
                len(remaining_entries)
            )
        except Exception as exc:
            logger.error("重写 API 普通日志失败，文件=%s，原因: %s", log_path, exc)


def retry_failed_upload_records_from_error_logs(
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """重报全部月度异常日志中已入库但上报失败的流量记录。

    参数:
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        None: 重报流程结束后不返回业务数据。
    """
    try:
        log_paths = list_api_error_log_paths()
    except Exception as exc:
        logger.warning("读取 API 异常日志目录失败，跳过历史重报: %s", exc)
        return

    if not log_paths:
        logger.info("未找到 API 异常日志，开始扫描普通日志补偿重报。")
        retry_failed_upload_records_from_api_logs(
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )
        return

    for log_path in log_paths:
        try:
            with open(log_path, 'r') as file_obj:
                log_text = file_obj.read()
        except Exception as exc:
            logger.warning("读取 API 异常日志失败，文件=%s，原因: %s", log_path, exc)
            continue

        entries = split_api_error_log_entries(log_text)
        if not entries:
            continue

        remaining_entries = []
        success_count = 0
        for entry_text in entries:
            if retry_api_error_log_entry(
                entry_text,
                log_path,
                traffic_upload_url,
                is_compute_server_upload,
                ip_address
            ):
                success_count += 1
                continue
            remaining_entries.append(entry_text)

        if success_count <= 0:
            continue

        try:
            rewrite_api_error_log(log_path, remaining_entries)
            logger.info(
                "已清理 API 异常日志成功重报记录，文件=%s，清理数=%d，保留数=%d",
                log_path,
                success_count,
                len(remaining_entries)
            )
        except Exception as exc:
            logger.error("重写 API 异常日志失败，文件=%s，原因: %s", log_path, exc)


def calculate_upload_bucket_count(spread_window, bucket_seconds):
    """计算上报分散窗口内可用的 100ms 桶数量。"""
    if bucket_seconds <= 0:
        return 1

    bucket_count = int(spread_window / bucket_seconds)
    if bucket_count <= 0:
        return 1

    return bucket_count


def calculate_random_upload_delay():
    """为当前统计窗口随机选择上报桶并计算延迟。

    参数:
        无。

    返回:
        tuple: 依次为延迟秒数、随机桶编号和可用桶总数。
    """
    bucket_count = calculate_upload_bucket_count(
        TRAFFIC_UPLOAD_SPREAD_WINDOW,
        TRAFFIC_UPLOAD_BUCKET_SECONDS
    )
    bucket_index = random.SystemRandom().randrange(bucket_count)
    delay_seconds = bucket_index * TRAFFIC_UPLOAD_BUCKET_SECONDS
    return delay_seconds, bucket_index, bucket_count


def sleep_until_upload_bucket(window_end, delay_seconds):
    """等待到当前窗口结束后的指定上报桶时间点。"""
    upload_ts = window_end + delay_seconds
    remaining = upload_ts - time.time()
    if remaining <= 0:
        return

    logger.info(
        "等待流量 API 上报分散桶，预计北京时间: %s，剩余 %.3f 秒",
        format_beijing_time(upload_ts),
        remaining
    )
    sleep_until(upload_ts)


def post_json(url, payload, headers, timeout):
    """发送 JSON 格式的 HTTP POST 请求。"""
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    request_headers = {
        'Content-Type': 'application/json',
        'x-api-key': TRAFFIC_UPLOAD_API_KEY
    }
    request_headers.update(headers or {})

    try:
        if urllib2 is not None:
            request = urllib2.Request(url, data=body, headers=request_headers)
            response = urllib2.urlopen(request, timeout=timeout)
        else:
            request = urllib_request.Request(url, data=body, headers=request_headers)
            response = urllib_request.urlopen(request, timeout=timeout)
    except Exception as exc:
        # HTTPError 仍然带有接口响应体，需要作为一次有效请求响应写入日志。
        if hasattr(exc, 'read') and hasattr(exc, 'code'):
            return exc.code, exc.read()
        raise

    try:
        status_code = response.getcode()
        response_body = response.read()
    finally:
        response.close()

    return status_code, response_body


def parse_status_code(status_code):
    """将 HTTP 状态码转换为整数。

    参数:
        status_code: 原始 HTTP 状态码。

    返回:
        int | None: 可转换时返回整数状态码，否则返回 None。
    """
    try:
        return int(status_code)
    except (TypeError, ValueError):
        return None


def is_retryable_status(status_code):
    """判断 HTTP 响应状态码是否适合重试。"""
    status_code_int = parse_status_code(status_code)
    if status_code_int is None:
        return False

    return status_code_int == 429 or status_code_int >= 500


def is_success_status(status_code):
    """判断 HTTP 响应状态码是否表示上报成功。

    参数:
        status_code: HTTP 响应状态码。

    返回:
        bool: 状态码位于 2xx 区间时返回 True，否则返回 False。
    """
    status_code_int = parse_status_code(status_code)
    if status_code_int is None:
        return False

    return 200 <= status_code_int < 300


def calculate_upload_retry_delay(retry_index):
    """计算流量上报重试前的等待时间。"""
    if retry_index <= 0:
        retry_index = 1

    delay_seconds = TRAFFIC_UPLOAD_RETRY_BASE_DELAY * (2 ** (retry_index - 1))
    return min(delay_seconds, TRAFFIC_UPLOAD_RETRY_MAX_DELAY)


def post_json_with_retries(url, payload, headers, timeout, max_retries):
    """带有限重试机制地发送 JSON 格式 HTTP POST 请求。"""
    if max_retries < 0:
        max_retries = 0

    attempts_limit = max_retries + 1
    last_exc = None

    for attempt in range(1, attempts_limit + 1):
        try:
            status_code, response_body = post_json(url, payload, headers, timeout)
            if not is_retryable_status(status_code):
                return status_code, response_body, attempt

            if attempt >= attempts_limit:
                return status_code, response_body, attempt

            delay_seconds = calculate_upload_retry_delay(attempt)
            logger.warning(
                "流量 API 上报返回可重试状态，status=%s，第 %d/%d 次请求失败，"
                "将在 %.1f 秒后重试。",
                status_code,
                attempt,
                attempts_limit,
                delay_seconds
            )
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts_limit:
                raise

            delay_seconds = calculate_upload_retry_delay(attempt)
            logger.warning(
                "流量 API 上报请求异常，第 %d/%d 次请求失败: %s，将在 %.1f 秒后重试。",
                attempt,
                attempts_limit,
                exc,
                delay_seconds
            )

        time.sleep(delay_seconds)

    if last_exc is not None:
        raise last_exc

    raise RuntimeError("流量 API 上报重试流程异常结束。")


def post_upload_records_with_log(
    records,
    window_start,
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """上报流量记录并写入 API 响应日志。

    参数:
        records: 已按接口过滤规则处理后的待上报记录列表。
        window_start: 统计窗口开始 Unix 时间戳，也是记录的 time_id。
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        tuple: 四元组，依次为是否成功、HTTP 状态码、响应体和请求次数。
    """
    if is_compute_server_upload:
        payload = build_compute_server_traffic_payload(records, ip_address)
    else:
        payload = build_traffic_payload(records)
    status_code, response_body, attempts = post_json_with_retries(
        traffic_upload_url,
        payload,
        {},
        TRAFFIC_UPLOAD_TIMEOUT,
        TRAFFIC_UPLOAD_MAX_RETRIES
    )
    append_api_log(window_start, status_code, response_body)
    return is_success_status(status_code), status_code, response_body, attempts


def filter_upload_records_by_mode(records, is_compute_server_upload):
    """根据上报模式过滤待上报记录。

    参数:
        records: 当前 time_id 对应的全部入库记录列表。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。

    返回:
        list: 符合当前接口上报规则的记录列表。
    """
    if is_compute_server_upload:
        return [
            record
            for record in records
            if record[1] == COMPUTE_SERVER_TRAFFIC_IFACE
        ]
    return filter_upload_records(records)


def upload_traffic_records(
    records,
    window_start,
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """将整五分钟窗口内的流量记录上报到流量 API，并记录响应内容。

    参数:
        records: 当前 time_id 对应的全部入库记录列表。
        window_start: 统计窗口开始 Unix 时间戳，也是记录的 time_id。
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        None: 上报流程结束后不返回业务数据。
    """
    retry_failed_upload_records_from_error_logs(
        traffic_upload_url,
        is_compute_server_upload,
        ip_address
    )

    upload_records = filter_upload_records_by_mode(
        records,
        is_compute_server_upload
    )
    ignored_count = len(records) - len(upload_records)
    if ignored_count:
        logger.info(
            "流量 API 上报已过滤接口 %s，过滤记录数=%d",
            ','.join(sorted(TRAFFIC_UPLOAD_IGNORED_IFACES)),
            ignored_count
        )

    try:
        success, status_code, response_body, attempts = post_upload_records_with_log(
            upload_records,
            window_start,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )
        if not success:
            try:
                append_api_error_log(
                    window_start,
                    'HTTP status {0}'.format(status_code),
                    upload_records,
                    response_body
                )
            except Exception as log_exc:
                logger.error("写入 API 异常日志失败: %s", log_exc)
        logger.info(
            "流量 API 上报完成，status=%s，记录数=%d，请求次数=%d",
            status_code,
            len(upload_records),
            attempts
        )
    except Exception as exc:
        logger.error(
            "流量 API 上报失败，已重试 %d 次: %s",
            TRAFFIC_UPLOAD_MAX_RETRIES,
            exc
        )
        try:
            append_api_log(window_start, 'ERROR', str(exc))
        except Exception as log_exc:
            logger.error("写入 API 响应日志失败: %s", log_exc)
        try:
            append_api_error_log(window_start, str(exc), upload_records)
        except Exception as log_exc:
            logger.error("写入 API 异常日志失败: %s", log_exc)


# ── 数据库操作 ────────────────────────────────────────────────────────────────

def create_table(cursor):
    """创建目标数据表。"""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS {table_name} (
            time_id         BIGINT NOT NULL,
            iface           TEXT   NOT NULL,
            mac             TEXT   NOT NULL,
            rx_speed        BIGINT NOT NULL,
            tx_speed        BIGINT NOT NULL,
            PRIMARY KEY (time_id, iface)
        )
    '''.format(table_name=TABLE_NAME))


def get_table_columns(cursor, table_name):
    """读取指定表的列信息。"""
    cursor.execute("PRAGMA table_info({0})".format(table_name))
    rows = cursor.fetchall()
    columns = []

    for row in rows:
        columns.append((row[1], (row[2] or '').upper()))

    return columns


def normalize_columns(columns):
    """将列定义标准化，便于做表结构比对。"""
    normalized = []

    for name, col_type in columns:
        normalized.append((name, (col_type or '').upper()))

    return normalized


def ensure_table_schema(conn, cursor):
    """确保目标表结构符合当前程序要求。"""
    current_columns = normalize_columns(get_table_columns(cursor, TABLE_NAME))
    expected_columns = normalize_columns(EXPECTED_COLUMNS)

    if not current_columns:
        create_table(cursor)
        return

    if current_columns == expected_columns:
        return

    backup_table = '{0}_legacy_{1}'.format(TABLE_NAME, int(time.time()))
    logger.warning("检测到旧表结构不匹配，旧表将重命名为 %s", backup_table)
    cursor.execute(
        'ALTER TABLE {0} RENAME TO {1}'.format(TABLE_NAME, backup_table)
    )
    create_table(cursor)
    conn.commit()


def init_db(db_path):
    """初始化数据库并校验表结构。"""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        ensure_table_schema(conn, cursor)
        conn.commit()
    finally:
        conn.close()

    logger.info("数据库已就绪: %s", db_path)


def save_records(db_path, records):
    """将统计结果批量写入数据库。"""
    if not records:
        return

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.executemany(
            '''
            INSERT OR REPLACE INTO {table_name} (
                time_id, iface, mac, rx_speed, tx_speed
            ) VALUES (?, ?, ?, ?, ?)
            '''.format(table_name=TABLE_NAME),
            records
        )
        conn.commit()
    finally:
        conn.close()


def load_records_by_time_id(window_start):
    """按 time_id 从对应月度 SQLite 文件读取已入库记录。

    参数:
        window_start: 统计窗口开始 Unix 时间戳，也是要查询的 time_id。

    返回:
        list: 数据库中匹配 time_id 的记录列表，每项包含 time_id、iface、mac、rx_speed、tx_speed。
    """
    db_path = get_monthly_db_path(window_start)
    if not os.path.exists(db_path):
        logger.warning(
            "普通 API 日志补偿重报找不到对应数据库，time_id=%d，数据库=%s",
            window_start,
            db_path
        )
        return []

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT time_id, iface, mac, rx_speed, tx_speed
            FROM {table_name}
            WHERE time_id = ?
            ORDER BY iface
            '''.format(table_name=TABLE_NAME),
            (LONG_TYPE(window_start),)
        )
        return cursor.fetchall()
    finally:
        conn.close()


# ── 网络数据读取 ──────────────────────────────────────────────────────────────

def read_net_dev():
    """解析 /proc/net/dev，返回各网卡的收发字节数。"""
    counters = {}
    try:
        with open(NET_DEV_PATH, 'r') as file_obj:
            lines = file_obj.readlines()
    except IOError as exc:
        logger.error("读取 %s 失败: %s", NET_DEV_PATH, exc)
        return counters

    # 前两行是表头，从第三行开始。
    for line in lines[2:]:
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        iface = parts[0].rstrip(':')
        if iface == 'lo':
            continue

        try:
            rx_bytes = LONG_TYPE(parts[1])
            tx_bytes = LONG_TYPE(parts[9])
        except (IndexError, ValueError):
            logger.warning("跳过无法解析的网卡统计行: %s", line)
            continue

        counters[iface] = {
            'rx_bytes': rx_bytes,
            'tx_bytes': tx_bytes
        }

    return counters


def parse_vlan_children_by_parent(lines):
    """解析 VLAN 配置并按父接口归集直属 VLAN 子接口。

    参数:
        lines: /proc/net/vlan/config 的文本行列表。

    返回:
        dict: key 为父接口名称，value 为排序后的直属 VLAN 子接口名称列表。
    """
    children_by_parent = {}
    for line in lines:
        parts = [part.strip() for part in line.split('|')]
        if len(parts) < 3 or parts[0] == 'VLAN Dev name':
            continue

        child_iface = parts[0]
        parent_iface = parts[2]
        if not child_iface or not parent_iface:
            continue

        children_by_parent.setdefault(parent_iface, []).append(child_iface)

    for parent_iface in children_by_parent:
        children_by_parent[parent_iface] = sorted(set(
            children_by_parent[parent_iface]
        ))
    return children_by_parent


def read_vlan_children_by_parent():
    """读取 Linux VLAN 配置并返回父接口与直属 VLAN 子接口映射。

    参数:
        无。

    返回:
        dict: key 为父接口名称，value 为直属 VLAN 子接口名称列表；配置不存在或
        读取失败时返回空字典。
    """
    if not os.path.exists(NET_VLAN_CONFIG_PATH):
        return {}

    try:
        with open(NET_VLAN_CONFIG_PATH, 'r') as file_obj:
            return parse_vlan_children_by_parent(file_obj.readlines())
    except IOError as exc:
        logger.warning("读取 %s 失败，将保留父接口原始流量: %s", NET_VLAN_CONFIG_PATH, exc)
        return {}


def get_mac_address(iface):
    """通过 ioctl(SIOCGIFHWADDR) 获取网卡 MAC 地址。"""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = struct.pack('16sH14s', iface.encode('utf-8')[:15], 0, b'\x00' * 14)
        response = fcntl.ioctl(sock.fileno(), SIOCGIFHWADDR, ifreq)
        mac_bytes = struct.unpack('16sH6B8s', response)[2:8]
        return ':'.join('%02x' % item for item in mac_bytes)
    except Exception as exc:
        logger.warning("获取 %s MAC 失败: %s", iface, exc)
        return DEFAULT_MAC
    finally:
        if sock is not None:
            sock.close()


def get_collectible_mac(iface):
    """获取可用于统计的 MAC 地址。"""
    mac = get_mac_address(iface)
    if mac == DEFAULT_MAC:
        return None

    return mac


def find_counter_window_anomaly(prev_counters, cur_counters):
    """检查窗口前后两次网络计数器采样是否存在异常。

    参数:
        prev_counters: 窗口起点采集到的网卡累计计数器。
        cur_counters: 窗口终点采集到的网卡累计计数器。

    返回:
        str 或 None: 存在异常时返回原因描述，全部正常时返回 None。
    """
    if not prev_counters or not cur_counters:
        return "窗口起点或终点的网络计数器采样为空"

    prev_ifaces = set(prev_counters.keys())
    cur_ifaces = set(cur_counters.keys())
    if prev_ifaces != cur_ifaces:
        missing_ifaces = sorted(prev_ifaces - cur_ifaces)
        added_ifaces = sorted(cur_ifaces - prev_ifaces)
        return "窗口内网卡集合发生变化，缺失={0}，新增={1}".format(
            missing_ifaces,
            added_ifaces
        )

    for iface in sorted(prev_ifaces):
        prev_values = prev_counters.get(iface)
        cur_values = cur_counters.get(iface)
        if not isinstance(prev_values, dict) or not isinstance(cur_values, dict):
            return "网卡 {0} 的计数器结构异常".format(iface)

        for counter_name in ('rx_bytes', 'tx_bytes'):
            if counter_name not in prev_values or counter_name not in cur_values:
                return "网卡 {0} 缺少 {1} 计数器".format(
                    iface,
                    counter_name
                )

            prev_value = prev_values[counter_name]
            cur_value = cur_values[counter_name]
            if (
                isinstance(prev_value, bool)
                or isinstance(cur_value, bool)
                or not isinstance(prev_value, (int, LONG_TYPE))
                or not isinstance(cur_value, (int, LONG_TYPE))
            ):
                return "网卡 {0} 的 {1} 计数器不是整数".format(
                    iface,
                    counter_name
                )

            if (
                prev_value < 0
                or cur_value < 0
                or prev_value > MAX_NET_COUNTER_VALUE
                or cur_value > MAX_NET_COUNTER_VALUE
            ):
                return "网卡 {0} 的 {1} 计数器超出有效范围".format(
                    iface,
                    counter_name
                )

            if cur_value < prev_value:
                return (
                    "网卡 {0} 的 {1} 计数器下降，prev={2}，cur={3}"
                ).format(iface, counter_name, prev_value, cur_value)

    return None


def round_half_up(value):
    """对数值执行传统意义上的四舍五入，并返回整数。"""
    if value >= 0:
        return LONG_TYPE(math.floor(value + 0.5))

    return LONG_TYPE(math.ceil(value - 0.5))


def build_adjusted_counter_deltas(
    prev_counters,
    cur_counters,
    vlan_children_by_parent
):
    """根据已校验的计数器计算增量，并直接扣除 VLAN 子接口增量。

    参数:
        prev_counters: 经窗口异常检查的起点网卡累计计数器。
        cur_counters: 经窗口异常检查的终点网卡累计计数器。
        vlan_children_by_parent: 父接口与直属 VLAN 子接口名称列表的映射。

    返回:
        dict: 按接口返回修正后的收发计数器增量。
    """
    raw_deltas = {}
    for iface in prev_counters:
        prev_values = prev_counters[iface]
        cur_values = cur_counters[iface]
        raw_deltas[iface] = {
            'rx_delta': cur_values['rx_bytes'] - prev_values['rx_bytes'],
            'tx_delta': cur_values['tx_bytes'] - prev_values['tx_bytes']
        }

    adjusted_deltas = dict(
        (iface, dict(values)) for iface, values in raw_deltas.items()
    )
    for parent_iface, child_ifaces in vlan_children_by_parent.items():
        if parent_iface not in raw_deltas:
            continue

        child_rx_delta = sum(
            raw_deltas[child_iface]['rx_delta']
            for child_iface in child_ifaces
            if child_iface in raw_deltas
        )
        child_tx_delta = sum(
            raw_deltas[child_iface]['tx_delta']
            for child_iface in child_ifaces
            if child_iface in raw_deltas
        )
        parent_rx_delta = raw_deltas[parent_iface]['rx_delta']
        parent_tx_delta = raw_deltas[parent_iface]['tx_delta']

        adjusted_deltas[parent_iface] = {
            'rx_delta': parent_rx_delta - child_rx_delta,
            'tx_delta': parent_tx_delta - child_tx_delta
        }

    return adjusted_deltas


def build_record(window_start, iface, counter_deltas, elapsed):
    """根据单张网卡修正后的计数器增量构建一条待入库记录。

    参数:
        window_start: 当前统计窗口开始 Unix 时间戳。
        iface: 当前网卡名称。
        counter_deltas: 当前网卡修正后的 rx_delta 和 tx_delta。
        elapsed: 已校验为正数的两次采样实际耗时，单位为秒。

    返回:
        tuple 或 None: 成功时返回待入库五元组；任一速率超过上限或
        无法取得有效 MAC 时返回 None。
    """
    mac = get_collectible_mac(iface)
    if mac is None:
        logger.info("跳过网卡 %s：无法获取有效 MAC 信息", iface)
        return None

    rx_delta = counter_deltas.get('rx_delta', 0)
    tx_delta = counter_deltas.get('tx_delta', 0)

    rx_speed = (float(rx_delta) * 8.0) / elapsed
    tx_speed = (float(tx_delta) * 8.0) / elapsed
    if (
        rx_speed > MAX_TRAFFIC_SPEED_BPS
        or tx_speed > MAX_TRAFFIC_SPEED_BPS
    ):
        logger.warning(
            "跳过网卡 %s 异常流量记录，不入库且不上报："
            "rx_speed=%.2f bps，tx_speed=%.2f bps，上限=10000 Gbps。",
            iface,
            rx_speed,
            tx_speed
        )
        return None

    rx_speed_rounded = round_half_up(rx_speed)
    tx_speed_rounded = round_half_up(tx_speed)
    return (
        LONG_TYPE(window_start),
        iface,
        mac,
        LONG_TYPE(rx_speed_rounded),
        LONG_TYPE(tx_speed_rounded)
    )


def resolve_window_ifaces(prev_counters, cur_counters, is_compute_server_upload):
    """根据上报模式确定当前窗口需要记录的网卡列表。

    参数:
        prev_counters: 窗口起点采集到的网卡累计计数器。
        cur_counters: 窗口终点采集到的网卡累计计数器。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。

    返回:
        list: 当前窗口需要生成记录的网卡名称列表。
    """
    all_ifaces = set(prev_counters.keys()) | set(cur_counters.keys())
    if is_compute_server_upload:
        if COMPUTE_SERVER_TRAFFIC_IFACE not in all_ifaces:
            logger.warning(
                "算力服务器流量上报模式未找到 %s 网卡，本窗口不会生成记录。",
                COMPUTE_SERVER_TRAFFIC_IFACE
            )
            return []
        return [COMPUTE_SERVER_TRAFFIC_IFACE]
    return sorted(all_ifaces)


# ── 整五分钟对齐 ──────────────────────────────────────────────────────────────

def current_aligned_window_start(timestamp, interval):
    """计算指定时间戳所在的 interval 对齐窗口开始时间。

    参数:
        timestamp: Unix 时间戳。
        interval: 对齐窗口长度，单位为秒。

    返回:
        int: timestamp 所属窗口的开始 Unix 时间戳。
    """
    return int(timestamp // interval) * interval


def should_skip_startup_window(startup_sample_time, window_end):
    """判断程序启动时是否应跳过当前剩余窗口。

    参数:
        startup_sample_time: 程序启动采样的 Unix 时间戳。
        window_end: 当前窗口结束的 Unix 时间戳。

    返回:
        bool: 剩余时间小于 5 秒时返回 True，否则返回 False。
    """
    remaining_seconds = window_end - startup_sample_time
    return remaining_seconds < MIN_STARTUP_WINDOW_REMAINING_SECONDS


def sleep_until(target_ts):
    """阻塞等待直到目标 Unix 时间戳到达。"""
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))


def log_iface_snapshot(counters):
    """输出当前识别到的网卡基础信息。"""
    logger.info("=== 当前主机网卡信息 ===")
    for iface in sorted(counters.keys()):
        mac = get_collectible_mac(iface)
        if mac is None:
            logger.info("  %-12s 已跳过：无法获取有效 MAC 信息", iface)
            continue
        logger.info(
            "  %-12s MAC: %s",
            iface, mac
        )
    logger.info("========================")


# ── 主循环 ───────────────────────────────────────────────────────────────────

def process_window_records(
    window_start,
    prev_counters,
    cur_counters,
    elapsed,
    traffic_api_post,
    window_end,
    traffic_upload_url,
    is_compute_server_upload,
    ip_address
):
    """处理单个统计窗口的记录生成、入库和可选 API 上报。

    参数:
        window_start: 当前统计窗口开始时间戳，也是入库 time_id。
        prev_counters: 窗口起点采集到的网卡累计计数器。
        cur_counters: 窗口终点采集到的网卡累计计数器。
        elapsed: 两次采样之间的实际耗时，单位为秒。
        traffic_api_post: 是否开启 API 上报。
        window_end: 当前统计窗口结束时间戳。
        traffic_upload_url: 流量上报接口 URL。
        is_compute_server_upload: 当前是否为算力服务器流量上报模式。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        list: 已生成并处理的数据库记录列表。
    """
    if elapsed <= 0:
        logger.warning(
            "跳过异常流量窗口，time_id=%d，不入库且不上报："
            "采样耗时 elapsed=%.6f 秒。",
            window_start,
            elapsed
        )
        return []

    anomaly_reason = find_counter_window_anomaly(prev_counters, cur_counters)
    if anomaly_reason is not None:
        logger.warning(
            "跳过异常流量窗口，time_id=%d，不入库且不上报：%s",
            window_start,
            anomaly_reason
        )
        return []

    vlan_children_by_parent = read_vlan_children_by_parent()
    adjusted_counter_deltas = build_adjusted_counter_deltas(
        prev_counters,
        cur_counters,
        vlan_children_by_parent
    )
    records = []
    for iface in resolve_window_ifaces(
        prev_counters,
        cur_counters,
        is_compute_server_upload
    ):
        record = build_record(
            window_start,
            iface,
            adjusted_counter_deltas.get(iface, {}),
            elapsed
        )
        if record is None:
            continue
        records.append(record)

        logger.info(
            "  %-12s MAC: %s  下行速率: %.2f bps  上行速率: %.2f bps",
            record[1], record[2], record[3], record[4]
        )

    monthly_db_path = ensure_monthly_db(window_start)
    save_records(monthly_db_path, records)
    logger.info(
        "已保存 %d 条记录，time_id=%d，数据库: %s",
        len(records),
        window_start,
        monthly_db_path
    )

    if traffic_api_post:
        if not records:
            logger.info(
                "当前窗口无有效流量记录，跳过流量 API 上报，time_id=%d。",
                window_start
            )
            return records

        upload_delay_seconds, upload_bucket_index, upload_bucket_count = (
            calculate_random_upload_delay()
        )
        logger.info(
            "当前窗口流量 API 上报随机分桶：bucket=%d/%d，窗口结束后延迟 %.3f 秒。",
            upload_bucket_index,
            upload_bucket_count,
            upload_delay_seconds
        )
        sleep_until_upload_bucket(window_end, upload_delay_seconds)
        logger.info(
            "准备执行流量 API 上报，time_id=%d，记录数=%d",
            window_start,
            len(records)
        )
        upload_traffic_records(
            records,
            window_start,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )

    return records


def main(traffic_api_post, traffic_upload_url, ip_address):
    """启动主循环并持续按固定窗口采样网卡流量。

    参数:
        traffic_api_post: 是否开启流量 API 上报。
        traffic_upload_url: 流量上报接口 URL。
        ip_address: 算力服务器流量上报使用的当前主机 IP。

    返回:
        None: 主循环持续运行，不返回业务数据。
    """
    logger.info("程序启动，统计周期 %d 秒，数据库模板: %s", INTERVAL, DB_PATH)
    logger.info("启动参数 traffic_api_post=%s", traffic_api_post)
    logger.info("启动参数 TRAFFIC_UPLOAD_URL=%s", traffic_upload_url)
    validate_upload_api_args(traffic_upload_url, ip_address)
    is_compute_server_upload = is_compute_server_upload_url(traffic_upload_url)
    if is_compute_server_upload:
        logger.info(
            "算力服务器流量上报模式已启用，仅记录并上报 %s，ip=%s",
            COMPUTE_SERVER_TRAFFIC_IFACE,
            ip_address
        )
    if traffic_api_post:
        logger.info(
            "已开启流量 API 上报：每个五分钟窗口完成后 POST 到 %s，请求超时时间 %d 秒。",
            traffic_upload_url,
            TRAFFIC_UPLOAD_TIMEOUT
        )
        logger.info("流量 API 上报将在每个统计窗口重新随机选择分散桶。")
    else:
        logger.info("未开启流量 API 上报；如需开启，请使用 --traffic_api_post true。")

    startup_sample_time = time.time()
    window_start = current_aligned_window_start(startup_sample_time, INTERVAL)
    baseline_ts = int(window_start + INTERVAL)
    if should_skip_startup_window(startup_sample_time, baseline_ts):
        logger.info(
            "程序启动时当前窗口剩余时间不足 %.1f 秒，"
            "跳过该窗口的流量记录和上报，等待下一窗口基线点（北京时间）: %s",
            MIN_STARTUP_WINDOW_REMAINING_SECONDS,
            format_beijing_time(baseline_ts)
        )
        sleep_until(baseline_ts)
        prev_counters = read_net_dev()
        prev_sample_time = time.time()
        log_iface_snapshot(prev_counters)
    else:
        prev_counters = read_net_dev()
        prev_sample_time = time.time()
        log_iface_snapshot(prev_counters)

        logger.info(
            "首次启动采样已开始，启动所在窗口 time_id=%d，"
            "等待窗口结束点（北京时间）: %s",
            window_start,
            format_beijing_time(baseline_ts)
        )
        sleep_until(baseline_ts)

        cur_counters = read_net_dev()
        cur_sample_time = time.time()
        elapsed = cur_sample_time - prev_sample_time

        process_window_records(
            window_start,
            prev_counters,
            cur_counters,
            elapsed,
            traffic_api_post,
            baseline_ts,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )

        prev_counters = cur_counters
        prev_sample_time = cur_sample_time
    window_start = baseline_ts

    while True:
        target_ts = window_start + INTERVAL
        logger.info(
            "等待窗口结束点（北京时间）: %s (窗口开始 time_id=%d)",
            format_beijing_time(target_ts),
            window_start
        )
        sleep_until(target_ts)

        cur_counters = read_net_dev()
        cur_sample_time = time.time()
        elapsed = cur_sample_time - prev_sample_time
        process_window_records(
            window_start,
            prev_counters,
            cur_counters,
            elapsed,
            traffic_api_post,
            target_ts,
            traffic_upload_url,
            is_compute_server_upload,
            ip_address
        )

        prev_counters = cur_counters
        prev_sample_time = cur_sample_time
        window_start = target_ts


if __name__ == '__main__':
    try:
        args = parse_args(sys.argv[1:])
        main(
            args.traffic_api_post,
            args.traffic_upload_url,
            args.ip
        )
    except KeyboardInterrupt:
        logger.info("程序已手动终止。")
        sys.exit(0)
