# Author: wenhao
"""批量通过 SSH 安装并启动外网流量监控脚本。

可用命令:
    python deploy_compute_traffic_monitor.py --commands traffic-monitor
    python deploy_compute_traffic_monitor.py --commands frpc-ssh
    python deploy_compute_traffic_monitor.py --commands iptables-allow
    python deploy_compute_traffic_monitor.py --commands traffic-monitor frpc-ssh iptables-allow
"""

from __future__ import annotations

import argparse
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Sequence

try:
    import paramiko
except ImportError:  # pragma: no cover - 运行环境缺少依赖时走显式错误
    paramiko = None


SSH_USERNAME = "root"
SSH_PASSWORD = "TencentOS2021++"
SSH_PORT = 36000
SSH_TIMEOUT_SECONDS = 30
REMOTE_COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_MAX_WORKERS = 16
REMOTE_SCRIPT_URL = (
    "https://gitee.com/liyinred/scripts/raw/master/"
    "net_traffic_monitor.sh"
)
TRAFFIC_UPLOAD_URL = "http://193.112.177.41:8000/api/traffic/compute-server-upload"
FRPC_INSTALL_SCRIPT_URL = (
    "https://gitee.com/liyinred/scripts/raw/master/"
    "install_frpc_ssh.sh"
)
FRPC_SERVER_ADDR = "175.4.50.59"
IPTABLES_ALLOW_SOURCE_IPS = (
    "175.6.60.199",
    "119.45.6.137",
    "121.4.105.211",
)
SERVER_IPS = (
    "124.88.174.194",
    "124.88.174.195",
    "124.88.174.196",
    "124.88.174.197",
    "124.88.174.198",
    "124.88.174.214",
    "124.88.174.199",
    "124.88.174.200",
    "124.88.174.201",
    "124.88.174.202",
    "124.88.174.203",
    "124.88.174.215",
    "124.88.174.204",
    "124.88.174.205",
    "124.88.174.206",
    "124.88.174.207",
    "124.88.174.208",
    "124.88.174.216",
    "124.88.174.209",
    "124.88.174.210",
    "124.88.174.211",
    "124.88.174.212",
    "124.88.174.213",
)


@dataclass(frozen=True)
class ExecutionResult:
    """保存单台主机远程命令执行结果。

    参数:
        ip: 主机外网 IP。
        exit_status: 远程 shell 命令退出码。
        stdout: 远程命令标准输出。
        stderr: 远程命令标准错误。

    返回:
        ExecutionResult 实例。
    """

    ip: str
    exit_status: int
    stdout: str
    stderr: str


def build_traffic_monitor_command(ip: str) -> str:
    """构造指定主机需要执行的流量监控安装命令。

    参数:
        ip: 写入安装命令最后一个参数的外网 IP。

    返回:
        str: 可直接通过 SSH 执行的 shell 命令。
    """
    if not ip:
        raise ValueError("IP 不能为空")

    return (
        "command -v curl >/dev/null 2>&1 "
        "|| sudo yum install -y --nogpgcheck curl "
        "&& curl -fSL "
        f"{shlex.quote(REMOTE_SCRIPT_URL)} "
        "| sudo bash -s -- true "
        f"{shlex.quote(TRAFFIC_UPLOAD_URL)} {shlex.quote(ip)}"
    )


def build_frpc_ssh_command(ip: str) -> str:
    """构造指定主机需要执行的 frpc SSH 安装命令。

    参数:
        ip: 用于生成 frpc proxy name 的外网 IP。

    返回:
        str: 可直接通过 SSH 执行的 frpc SSH 安装命令。
    """
    if not ip:
        raise ValueError("IP 不能为空")

    proxy_name = f"{ip}-ssh"
    return (
        "curl -fSL "
        f"{shlex.quote(FRPC_INSTALL_SCRIPT_URL)} "
        "| sudo bash -s -- "
        f"-s {shlex.quote(FRPC_SERVER_ADDR)} -n {shlex.quote(proxy_name)}"
    )


def build_iptables_allow_rule_args(source_ip: str) -> str:
    """构造 iptables 放行规则的公共参数。

    参数:
        source_ip: 需要放行的来源 IP。

    返回:
        str: 可追加在 iptables chain 操作后的规则参数。
    """
    if not source_ip:
        raise ValueError("来源 IP 不能为空")

    return f"INPUT -s {shlex.quote(source_ip)} -p all -j ACCEPT"


def build_iptables_allow_command(ip: str) -> str:
    """构造幂等放行指定来源 IP 访问本机的 iptables 命令。

    参数:
        ip: 当前目标主机外网 IP，仅用于与其他 command builder 保持统一签名。

    返回:
        str: 可直接通过 SSH 执行的 iptables 检查、放行、保存和复查命令。
    """
    if not ip:
        raise ValueError("IP 不能为空")

    ensure_commands = []
    check_commands = []
    for source_ip in IPTABLES_ALLOW_SOURCE_IPS:
        rule_args = build_iptables_allow_rule_args(source_ip)
        ensure_commands.append(f"(iptables -C {rule_args} || iptables -A {rule_args})")
        check_commands.append(f"iptables -C {rule_args}")

    return " && ".join(
        [
            *ensure_commands,
            *check_commands,
            "service iptables save",
            "iptables -L --line-numbers",
        ]
    )


REMOTE_COMMAND_BUILDERS = {
    "traffic-monitor": build_traffic_monitor_command,
    "frpc-ssh": build_frpc_ssh_command,
    "iptables-allow": build_iptables_allow_command,
}
DEFAULT_REMOTE_COMMANDS = ("traffic-monitor", "frpc-ssh")


def build_remote_command(ip: str, command_names: Sequence[str]) -> str:
    """构造指定主机需要执行的完整远程 shell 命令。

    参数:
        ip: 写入安装命令和 frpc proxy name 的外网 IP。
        command_names: 需要执行的远程命令名称列表。

    返回:
        str: 可直接通过 SSH 执行的完整 shell 命令。
    """
    command = " && ".join(
        REMOTE_COMMAND_BUILDERS[command_name](ip) for command_name in command_names
    )
    return f"timeout {REMOTE_COMMAND_TIMEOUT_SECONDS}s bash -lc {shlex.quote(command)}"


def create_ssh_client(ip: str) -> "paramiko.SSHClient":
    """创建并连接指定主机的 SSH client。

    参数:
        ip: 需要连接的主机外网 IP。

    返回:
        paramiko.SSHClient: 已建立连接的 SSH client。
    """
    if paramiko is None:
        raise RuntimeError("缺少依赖 paramiko，请先执行：pip install paramiko")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ip,
        port=SSH_PORT,
        username=SSH_USERNAME,
        password=SSH_PASSWORD,
        timeout=SSH_TIMEOUT_SECONDS,
        banner_timeout=SSH_TIMEOUT_SECONDS,
        auth_timeout=SSH_TIMEOUT_SECONDS,
    )
    return client


def execute_remote_command(ip: str, command_names: Sequence[str]) -> ExecutionResult:
    """通过 SSH 在指定主机执行流量监控和 frpc SSH 安装命令。

    参数:
        ip: 需要登录并执行命令的主机外网 IP。
        command_names: 需要执行的远程命令名称列表。

    返回:
        ExecutionResult: 远程完整命令执行结果。
    """
    command = build_remote_command(ip, command_names)
    client = create_ssh_client(ip)
    try:
        _, stdout_stream, stderr_stream = client.exec_command(
            command,
            get_pty=True,
            timeout=REMOTE_COMMAND_TIMEOUT_SECONDS + SSH_TIMEOUT_SECONDS,
        )
        exit_status = stdout_stream.channel.recv_exit_status()
        stdout = stdout_stream.read().decode("utf-8", errors="replace")
        stderr = stderr_stream.read().decode("utf-8", errors="replace")
        return ExecutionResult(
            ip=ip,
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        client.close()


def print_execution_result(result: ExecutionResult) -> bool:
    """打印单台主机执行结果并返回是否成功。

    参数:
        result: 单台主机远程命令执行结果。

    返回:
        bool: 远程命令退出码为 0 时返回 True，否则返回 False。
    """
    if result.stdout.strip():
        print(f"[{result.ip}] stdout:\n{result.stdout.rstrip()}", flush=True)
    if result.stderr.strip():
        print(f"[{result.ip}] stderr:\n{result.stderr.rstrip()}", file=sys.stderr, flush=True)

    if result.exit_status == 0:
        print(f"[{result.ip}] 执行成功", flush=True)
        return True

    print(f"[{result.ip}] 执行失败，退出码：{result.exit_status}", file=sys.stderr, flush=True)
    return False


def deploy_to_servers(
    ips: Sequence[str],
    max_workers: int,
    command_names: Sequence[str],
) -> int:
    """并发登录服务器并执行远程安装命令。

    参数:
        ips: 需要处理的主机外网 IP 列表。
        max_workers: SSH 并发执行数量。
        command_names: 需要在每台主机执行的远程命令名称列表。

    返回:
        int: 全部成功返回 0，存在失败返回 1。
    """
    if max_workers < 1:
        raise ValueError("并发数量必须大于 0")

    has_failure = False
    total = len(ips)
    worker_count = min(max_workers, total)

    print(f"准备并发处理 {total} 台主机，并发数：{worker_count}", flush=True)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_ip = {}
        for index, ip in enumerate(ips, start=1):
            print(f"[{index}/{total}] 已提交 {ip}", flush=True)
            future_to_ip[executor.submit(execute_remote_command, ip, command_names)] = ip

        for completed_count, future in enumerate(as_completed(future_to_ip), start=1):
            ip = future_to_ip[future]
            print(f"[{completed_count}/{total}] 收到 {ip} 执行结果", flush=True)
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - 批量运维脚本需汇总单机异常
                has_failure = True
                print(f"[{ip}] SSH 或命令执行异常 {exc}", file=sys.stderr, flush=True)
                continue

            if not print_execution_result(result):
                has_failure = True

    return 1 if has_failure else 0


def parse_max_workers(value: str) -> int:
    """解析并校验 SSH 并发数量。

    参数:
        value: 命令行传入的并发数量字符串。

    返回:
        int: 校验后的并发数量。
    """
    try:
        max_workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("并发数量必须是整数") from exc

    if max_workers < 1:
        raise argparse.ArgumentTypeError("并发数量必须大于 0")
    return max_workers


def parse_command_names(values: Sequence[str]) -> tuple[str, ...]:
    """解析并校验需要执行的远程命令名称。

    参数:
        values: 命令行传入的远程命令名称列表。

    返回:
        tuple[str, ...]: 去重后保留输入顺序的远程命令名称。
    """
    command_names = tuple(dict.fromkeys(values))
    if not command_names:
        raise argparse.ArgumentTypeError("至少需要选择一条命令")

    unsupported_names = [
        command_name
        for command_name in command_names
        if command_name not in REMOTE_COMMAND_BUILDERS
    ]
    if unsupported_names:
        supported_names = ", ".join(REMOTE_COMMAND_BUILDERS)
        raise argparse.ArgumentTypeError(
            f"不支持的命令：{', '.join(unsupported_names)}；可选命令：{supported_names}"
        )
    return command_names


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    参数:
        argv: 待解析的命令行参数；为 None 时使用 sys.argv。

    返回:
        argparse.Namespace: 解析后的命令行参数。
    """
    parser = argparse.ArgumentParser(
        description="并发通过 SSH 登录服务器并安装 compute traffic monitor。",
    )
    parser.add_argument(
        "--max-workers",
        type=parse_max_workers,
        default=DEFAULT_MAX_WORKERS,
        help=f"SSH 并发数量，默认 {DEFAULT_MAX_WORKERS}。",
    )
    parser.add_argument(
        "--commands",
        nargs="+",
        default=DEFAULT_REMOTE_COMMANDS,
        metavar="COMMAND",
        choices=tuple(REMOTE_COMMAND_BUILDERS),
        help=(
            "选择要执行的一条或多条命令，默认执行全部。"
            f"可选：{', '.join(REMOTE_COMMAND_BUILDERS)}。"
        ),
    )
    args = parser.parse_args(argv)
    args.commands = parse_command_names(args.commands)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """执行批量部署入口流程。

    参数:
        argv: 命令行参数；为 None 时使用 sys.argv。

    返回:
        int: 进程退出码，0 表示成功，1 表示至少一台主机失败。
    """
    args = parse_args(argv)
    return deploy_to_servers(SERVER_IPS, args.max_workers, args.commands)


if __name__ == "__main__":
    raise SystemExit(main())
