# -*- coding: utf-8 -*-
# Author: wenhao

import argparse
import asyncio
import json
import random
import ssl
import time
from collections import Counter
from dataclasses import dataclass
from statistics import mean
from typing import Any
from urllib.parse import urlparse

API_KEY = "xiaohe666"

MAC_ADDRESSES = [
    "5e:9b:77:f6:58:f8",
    "2a:00:8b:ce:4e:34",
    "3e:06:26:a2:be:fe",
    "42:96:a2:03:60:9d",
    "00:1b:21:bc:3a:02",
    "00:1b:21:bc:3a:03",
    "00:1b:21:bc:3a:05",
    "00:1b:21:bc:3a:07",
    "56:69:80:0b:6b:28",
    "de:4f:00:ed:7e:a6",
    "da:2f:0e:f1:6a:51",
    "26:50:14:3f:e4:b3",
    "6a:59:0b:46:bd:05",
    "d6:12:65:82:20:5c",
    "fe:e8:ef:87:08:94",
    "a6:76:8b:62:5d:44",
    "12:0f:e1:d2:fd:5d",
    "26:eb:bc:02:91:61",
    "b6:cc:c8:5f:ad:e7",
    "62:14:1c:94:d2:7e",
    "ca:b3:1a:37:55:82",
    "fa:20:04:d2:d4:fa",
    "2e:71:1c:3b:be:3a",
    "72:cc:0d:0c:0c:ca",
    "be:08:e4:5e:a6:b6",
    "96:b1:ef:3d:74:5d",
    "fa:c6:56:96:f4:81",
    "9e:97:e8:94:af:7f",
    "8e:b7:3d:83:eb:fe",
    "3e:5d:da:84:67:3b",
    "32:e1:0e:0b:94:af",
    "ae:ae:82:7e:a2:29",
    "8a:b2:73:f4:ab:6e",
    "96:0c:a3:13:df:12",
    "b6:14:82:c4:56:2d",
    "a6:ba:e3:f0:3a:e0",
    "c6:6d:60:57:b1:4f",
    "76:8d:61:5c:19:2a",
    "0e:a3:ed:0e:ab:a8",
    "56:29:64:de:42:a0",
    "ae:c2:a5:24:25:bb",
    "bc:24:11:09:fe:2a",
    "fa:c0:21:d2:69:76",
    "7a:b6:b3:98:68:5d",
    "26:33:4d:9c:3a:c4",
    "fe:80:dc:39:bd:e6",
    "3e:bd:18:e8:1e:a2",
    "52:a7:df:d4:e5:39",
    "ce:8a:50:e2:84:48",
    "26:99:2a:a9:2c:8c",
    "ba:e6:93:84:ae:7d",
    "ce:64:ed:08:74:d1",
    "ce:cb:3f:8e:5c:2d",
    "d2:a5:af:5a:82:07",
    "82:88:13:d3:07:f0",
    "ca:8f:9b:b3:6b:bc",
    "22:0f:e9:f3:86:10",
    "86:95:ab:af:20:9c",
    "3e:02:17:a9:dc:16",
]


@dataclass(frozen=True)
class TargetEndpoint:
    """压测目标接口信息。"""

    scheme: str
    host: str
    port: int
    path: str
    ssl_context: ssl.SSLContext | None


@dataclass(frozen=True)
class RequestResult:
    """单次请求结果。"""

    status_code: int | None
    latency_seconds: float
    error: str | None


def parse_arguments() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="压测 /api/traffic/upload 接口。")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/api/traffic/upload",
        help="目标接口地址。",
    )
    parser.add_argument("--requests", type=int, default=10000, help="总请求数。")
    parser.add_argument("--concurrency", type=int, default=10000, help="最大并发数。")
    parser.add_argument(
        "--launch-window",
        type=float,
        default=1.0,
        help="请求启动窗口秒数，默认 1 秒内调度全部请求。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="单请求超时时间秒数。",
    )
    return parser.parse_args()


def build_target_endpoint(url: str) -> TargetEndpoint:
    """根据 URL 构造压测目标接口信息。"""
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError("仅支持 http 或 https URL。")
    if not parsed_url.hostname:
        raise ValueError("URL 必须包含 host。")

    default_port = 443 if parsed_url.scheme == "https" else 80
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"
    ssl_context = ssl.create_default_context() if parsed_url.scheme == "https" else None
    return TargetEndpoint(
        scheme=parsed_url.scheme,
        host=parsed_url.hostname,
        port=parsed_url.port or default_port,
        path=path,
        ssl_context=ssl_context,
    )


def build_time_id() -> int:
    """生成整 5 分钟 Unix timestamp。"""
    current_timestamp = int(time.time())
    return current_timestamp - current_timestamp % 300


def build_request_time_id(request_index: int) -> int:
    """为单次请求生成不重复的整 5 分钟 time_id。"""
    mac_slot_index = request_index // len(MAC_ADDRESSES)
    return build_time_id() - mac_slot_index * 300


def build_payload(request_index: int) -> bytes:
    """构造单次上报请求体。"""
    mac_address = MAC_ADDRESSES[request_index % len(MAC_ADDRESSES)]
    payload = [
        {
            "time_id": build_request_time_id(request_index),
            "mac": mac_address,
            "rx_speed": random.randint(1_000_000, 10_000_000),
            "tx_speed": random.randint(100_000, 2_000_000),
        }
    ]
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def build_http_request(target: TargetEndpoint, body: bytes) -> bytes:
    """构造原始 HTTP POST 请求字节。"""
    headers = [
        f"POST {target.path} HTTP/1.1",
        f"Host: {target.host}:{target.port}",
        "User-Agent: pcdn-tx-traffic-stress-test/1.0",
        "Content-Type: application/json",
        f"x-api-key: {API_KEY}",
        f"Content-Length: {len(body)}",
        "Connection: close",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii") + body


def parse_status_code(status_line: bytes) -> int | None:
    """解析 HTTP 响应状态码。"""
    try:
        return int(status_line.split(maxsplit=2)[1])
    except (IndexError, ValueError):
        return None


async def send_one_request(
    target: TargetEndpoint,
    request_index: int,
    timeout_seconds: float,
    semaphore: asyncio.Semaphore,
) -> RequestResult:
    """发送单次接口请求并记录结果。"""
    async with semaphore:
        start_time = time.perf_counter()
        try:
            return await asyncio.wait_for(
                send_raw_http_request(target, request_index, start_time),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            latency_seconds = time.perf_counter() - start_time
            return RequestResult(None, latency_seconds, "timeout")
        except OSError as exc:
            latency_seconds = time.perf_counter() - start_time
            return RequestResult(None, latency_seconds, exc.__class__.__name__)


async def send_raw_http_request(
    target: TargetEndpoint,
    request_index: int,
    start_time: float,
) -> RequestResult:
    """通过 asyncio TCP 连接发送原始 HTTP 请求。"""
    reader, writer = await asyncio.open_connection(
        target.host,
        target.port,
        ssl=target.ssl_context,
        server_hostname=target.host if target.ssl_context else None,
    )
    body = build_payload(request_index)
    writer.write(build_http_request(target, body))
    await writer.drain()
    status_line = await reader.readline()
    status_code = parse_status_code(status_line)
    writer.close()
    await writer.wait_closed()
    latency_seconds = time.perf_counter() - start_time
    return RequestResult(status_code, latency_seconds, None)


async def run_stress_test(arguments: argparse.Namespace) -> list[RequestResult]:
    """执行压测并返回全部请求结果。"""
    if arguments.requests < 1:
        raise ValueError("requests 必须大于 0。")
    if arguments.concurrency < 1:
        raise ValueError("concurrency 必须大于 0。")
    if arguments.launch_window <= 0:
        raise ValueError("launch-window 必须大于 0。")

    target = build_target_endpoint(arguments.url)
    semaphore = asyncio.Semaphore(arguments.concurrency)
    tasks = []
    interval_seconds = arguments.launch_window / arguments.requests
    start_time = time.perf_counter()

    # 以固定间隔创建任务，保证 1 秒窗口内启动 10000 次请求。
    for request_index in range(arguments.requests):
        tasks.append(
            asyncio.create_task(
                send_one_request(
                    target,
                    request_index,
                    arguments.timeout,
                    semaphore,
                )
            )
        )
        next_launch_time = start_time + interval_seconds * (request_index + 1)
        sleep_seconds = next_launch_time - time.perf_counter()
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)

    return await asyncio.gather(*tasks)


def percentile(sorted_values: list[float], percentile_value: float) -> float:
    """计算延迟分位数。"""
    if not sorted_values:
        return 0.0
    index = round((percentile_value / 100) * (len(sorted_values) - 1))
    return sorted_values[index]


def build_summary(
    results: list[RequestResult],
    elapsed_seconds: float,
    launch_window_seconds: float,
) -> dict[str, Any]:
    """汇总压测结果。"""
    status_counter = Counter(
        str(result.status_code) if result.status_code is not None else "no_response"
        for result in results
    )
    error_counter = Counter(result.error for result in results if result.error)
    success_count = sum(
        1
        for result in results
        if result.status_code and 200 <= result.status_code < 300
    )
    latencies = sorted(result.latency_seconds for result in results)
    return {
        "total": len(results),
        "success": success_count,
        "failed": len(results) - success_count,
        "elapsed_seconds": elapsed_seconds,
        "launch_window_seconds": launch_window_seconds,
        "requests_per_second": (
            len(results) / elapsed_seconds if elapsed_seconds > 0 else 0
        ),
        "status_codes": dict(sorted(status_counter.items())),
        "errors": dict(error_counter),
        "latency_ms": {
            "min": min(latencies) * 1000 if latencies else 0,
            "avg": mean(latencies) * 1000 if latencies else 0,
            "p50": percentile(latencies, 50) * 1000,
            "p90": percentile(latencies, 90) * 1000,
            "p95": percentile(latencies, 95) * 1000,
            "p99": percentile(latencies, 99) * 1000,
            "max": max(latencies) * 1000 if latencies else 0,
        },
    }


def print_summary(summary: dict[str, Any]) -> None:
    """打印压测结果。"""
    print("Traffic upload stress test result")
    print(f"total: {summary['total']}")
    print(f"success: {summary['success']}")
    print(f"failed: {summary['failed']}")
    print(f"elapsed_seconds: {summary['elapsed_seconds']:.3f}")
    print(f"launch_window_seconds: {summary['launch_window_seconds']:.3f}")
    print(f"requests_per_second: {summary['requests_per_second']:.2f}")
    print(f"status_codes: {summary['status_codes']}")
    print(f"errors: {summary['errors']}")
    print("latency_ms:")
    for key, value in summary["latency_ms"].items():
        print(f"  {key}: {value:.2f}")


def main() -> None:
    """执行命令行压测入口。"""
    arguments = parse_arguments()
    start_time = time.perf_counter()
    results = asyncio.run(run_stress_test(arguments))
    elapsed_seconds = time.perf_counter() - start_time
    print_summary(build_summary(results, elapsed_seconds, arguments.launch_window))


if __name__ == "__main__":
    main()
