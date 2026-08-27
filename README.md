# scripts

本项目是 `pcdn-tx` 项目的运维与辅助脚本集合，覆盖前端构建产物打包、边缘节点流量采集、设备 WebSocket 通道、SSH 内网穿透安装以及流量上报接口压测等场景。脚本既可作为单机工具运行，也与 `pcdn-tx` 后端的 `/api/traffic/upload` 接口、CentOS7 边缘主机配套使用。

## 项目结构

```
scripts/
├── package_dist.py                 # 打包 frontend/dist 为 dist.zip
├── net_traffic_monitor.py          # 边缘主机整 5 分钟窗口流量统计与上报
├── device_websocket_agent.py       # CentOS7 主机 WebSocket 通道客户端
├── install_frpc_ssh.sh             # frpc + SSH 内网穿透一键安装脚本
├── dist.zip                        # 前端构建产物压缩包（由 package_dist.py 生成）
├── dist_docs.zip                   # VitePress 文档站压缩包
├── stress_test/
│   ├── traffic_upload_stress_test.py  # asyncio 实现的流量上报压测
│   └── traffic_upload_wrk.lua         # wrk 配套的 Lua 压测脚本
└── vitepress_project/              # 小合云帮助中心（VitePress 站点源码）
    ├── docs/
    │   ├── .vitepress/config.mts
    │   ├── index.md
    │   ├── en/index.md
    │   ├── agreements/
    │   └── settlements/
    └── package.json
```

## 脚本一览

| 脚本                                          | 类型             | 用途                                       | 主要依赖                                         |
| ------------------------------------------- | -------------- | ---------------------------------------- | -------------------------------------------- |
| `package_dist.py`                           | Python 3       | 将 `frontend/dist` 压缩为 `dist.zip` 供后端统一挂载 | Python 3.x 标准库                               |
| `net_traffic_monitor.py`                    | Python 2.7/3.x | 解析 `/proc/net/dev`，按整 5 分钟窗口统计上下行速率并可选上报 | `urllib2`/`urllib.request`、`fcntl`、`sqlite3` |
| `device_websocket_agent.py`                 | Python 2.7/3.x | CentOS7 边缘主机 WebSocket 通道客户端，响应服务端指令     | Python 标准库 `socket`/`ssl`/`hashlib`          |
| `install_frpc_ssh.sh`                       | Bash           | 一键安装 frpc、创建用户、配置 SSH 密码登录并注册 systemd 服务 | `wget`、`tar`、`systemctl`                     |
| `stress_test/traffic_upload_stress_test.py` | Python 3.10+   | 异步并发的 `/api/traffic/upload` 接口压测         | `asyncio`、`ssl`、`dataclasses`                |
| `stress_test/traffic_upload_wrk.lua`        | Lua            | 配合 `wrk` 使用的压测脚本，按线程分片生成 time\_id        | `wrk >= 4.2`                                 |

## 脚本说明

### `package_dist.py`

将 `frontend/dist` 整个目录压缩为 `scripts/dist.zip`，供后端 FastAPI 进程以单端口方式统一托管前端构建产物。

关键行为：

- 路径常量：`PROJECT_ROOT = <repo>/scripts` 的上一级，`DIST_DIR = <repo>/frontend/dist`，`TARGET_DIR = <repo>/scripts`。
- 使用 `zipfile.ZIP_DEFLATED` 进行压缩，压缩后通过 `os.replace` 原子替换旧产物，避免半成品文件被外部进程读取。
- 在 `scripts/` 下创建 `pcdn_tx_dist_*` 临时目录写入临时压缩包，完成后清理。

执行方式：

```bash
python scripts/package_dist.py
# 输出：已生成压缩包：.../scripts/dist.zip
```

### `net_traffic_monitor.py`

面向 CentOS7 / Python 2.7 的边缘主机流量统计与上报守护进程，同时兼容 Python 3。功能包括：

- 解析 `/proc/net/dev`，按整 5 分钟（`INTERVAL = 300` 秒）窗口计算每块网卡的平均上下行速率。
- 读取 `/proc/net/vlan/config` 识别 VLAN 父子关系，从父接口增量中扣除直属 VLAN 子接口增量，避免汇总时重复统计。
- 以「北京时间的自然月」为粒度创建 SQLite 数据库文件（`net_traffic_data/net_traffic_YYYYMM.db`），便于按月切片归档。
- 通过 `/sys/class/net` + `fcntl(SIOCGIFHWADDR)` 获取 MAC 地址，写入 `net_speed` 表的 `time_id / iface / mac / rx_speed / tx_speed` 五元组。
- 当 `--traffic_api_post true` 时，将每条记录 POST 到 `--TRAFFIC_UPLOAD_URL` 指定的上报接口，并写入按月切片的 API 日志与异常日志；上报失败支持指数退避重试（`TRAFFIC_UPLOAD_MAX_RETRIES=5`）。
- 每个统计窗口结束后重新随机选择 100ms 上报桶，在 10 秒窗口内分散 API 请求。
- 启动时扫描 `api_log/` 下的历史 API 日志，对「已入库但上报失败」或「异常退出」导致的丢单记录进行补偿重报。
- 接口过滤规则：`TRAFFIC_UPLOAD_IGNORED_IFACES = {'eth1'}`，不参与上报。

启动示例：

```bash
python scripts/net_traffic_monitor.py \
  --TRAFFIC_UPLOAD_URL https://api.example.com/api/traffic/upload \
  --traffic_api_post true
```

辅助函数：

- `parse_bool` 支持 `true/false、1/0、yes/no、on/off` 等命令行布尔写法。
- `build_traffic_payload` / `filter_upload_records` / `append_api_error_log` 等被拆分为可独立单测的纯函数。

### `device_websocket_agent.py`

CentOS7 边缘主机侧的 WebSocket 通道客户端，与 `pcdn-tx` 后端的 `backend/websocket_main.py` 配套。能力：

- 读取 `/etc/machine-id`，使用 SHA-256 + URL-safe Base64 派生稳定的 `device_id`。
- 支持命令行 `--device-type {ACDN,PCDN}`，默认 `ACDN`。
- 解析 `/proc/cpuinfo`、`/proc/meminfo`、`/etc/os-release`、`/sys/class/net`、`lsblk` 等系统信息，构造响应 `collect_system_info` 指令的负载。
- `collect_acceptance_info` 指令会附带 SSD/HDD 数量与总容量（`ssdCount / hddCount / ssdTotalDisk / hddTotalDisk`）、CPU 架构、SSH 端口、内外网 IP 等验收单信息。
- TCP 层手工实现 RFC 6455 WebSocket 握手与帧解析（文本/关闭/Ping/Pong），避免引入第三方客户端库。
- 连接断开后按 `DEFAULT_RECONNECT_DELAY = 5` 秒自动重连。

启动示例：

```bash
python scripts/device_websocket_agent.py \
  --wss-url wss://api.example.com/ws/device \
  --device-type ACDN
```

### `install_frpc_ssh.sh`

Bash 一键脚本，用于在目标边缘主机上：

1. 下载并解压 frp `0.68.1`（通过 `https://gh-proxy.org` 加速）。
2. 自动检测缺失的 `wget`，按 `yum` / `apt-get` 顺序自动安装。
3. 检测并复用 `sshd_config` 的 `Port`，或回退到 `ss` / `netstat` 实时识别 SSH 监听端口；仍无法识别时回退到默认 22。
4. 备份并修改 `/etc/ssh/sshd_config` 启用 `PasswordAuthentication yes`，执行 `sshd -t` 校验后重启 `sshd`/`ssh`。
5. 写入 `/etc/hosts.allow` 允许 `127.0.0.1`、`localhost` 访问 `sshd`。
6. 创建 `xiaohessh` 用户并加入 `wheel`/`sudo` 组，写入预置密码（请按实际运维策略修改）。
7. 生成 `frpc.toml`（`serverAddr` + `auth.token` + TCP 代理到本地 SSH 端口）并注册 `frp_cool` systemd 服务，设置开机自启。

执行示例：

```bash
sudo bash scripts/install_frpc_ssh.sh --serverAddr 1.2.3.4 --name my-ssh
```

参数：

- `-s, --serverAddr`：frp 服务端地址（必填）。
- `-n, --name`：frpc 中 `[[proxies]]` 的 `name`（必填）。
- `-h, --help`：查看帮助。

### `stress_test/traffic_upload_stress_test.py`

使用 `asyncio` 直接构造 HTTP 报文，对 `/api/traffic/upload` 进行高并发压测。特性：

- 通过 `--url` 指定目标接口，`--requests`、`--concurrency`、`--launch-window` 控制总量、并发与启动斜坡。
- 启动期间按 `launch-window / requests` 的固定间隔派发任务，模拟瞬时高并发。
- 内置 MAC 地址池（60 个），按请求序号派生 `time_id = floor(now/300) - (index // 60) * 300`，保证每个 time\_id 唯一。
- 汇总输出 `total / success / failed / requests_per_second`、状态码分布、错误计数、延迟 min/avg/p50/p90/p95/p99/max。
- 使用 `asyncio.Semaphore` 限制最大并发，避免 fd 耗尽。

执行示例：

```bash
python scripts/stress_test/traffic_upload_stress_test.py \
  --url http://127.0.0.1:8000/api/traffic/upload \
  --requests 10000 --concurrency 10000 --launch-window 1.0
```

### `stress_test/traffic_upload_wrk.lua`

配合 `wrk` 的 Lua 脚本，与 Python 压测脚本产生等价的请求模式。特性：

- 通过 `setup`/`init` 为每个线程分配稳定编号，结合 `thread_count` 全局计算请求序号，避免重复 time\_id。
- 通过 `TRAFFIC_UPLOAD_API_KEY` 环境变量覆盖 `x-api-key` 头。
- 通过 `TRAFFIC_UPLOAD_REQUESTS`（环境变量）和 `-- <total_requests>`（脚本参数）控制总请求数；脚本参数优先级更高。
- `done` 钩子聚合多线程状态码计数并打印 2xx/4xx/5xx/other 分布与详细状态码。

执行示例：

```bash
wrk -t8 -c10000 -d30s -s scripts/stress_test/traffic_upload_wrk.lua \
  http://127.0.0.1:8000/api/traffic/upload -- 10000 8
```

## VitePress 帮助中心（`vitepress_project/`）

基于 VitePress `2.0.0-alpha.17` 的中英双语文档站源码，部署为「小合云帮助中心」，提供用户协议、计费 SLA、发票开具须知等对外说明文档。

常用命令：

```bash
cd scripts/vitepress_project
pnpm install                # 或 npm install
pnpm run docs:dev           # 本地预览
pnpm run docs:build         # 构建到 ../dist_docs
```

构建完成后会同时打包为 `scripts/dist_docs.zip`，便于上传到对象存储或 CDN。

## 产物文件

| 文件                      | 生成方式                                       | 用途              |
| ----------------------- | ------------------------------------------ | --------------- |
| `scripts/dist.zip`      | `python scripts/package_dist.py`           | 后端单端口部署时的前端构建产物 |
| `scripts/dist_docs.zip` | `pnpm run docs:build`（`vitepress_project`） | 帮助中心静态站点，可直接托管  |

> 上述产物建议在 CI 中按 `frontend build → package_dist → docs:build` 流水线生成，避免手工上传。

## 常见工作流

1. **发布新版本前端**
   - `cd frontend && pnpm run build`
   - `python scripts/package_dist.py`
   - 后端重启加载最新 `dist.zip`。
2. **部署边缘节点**
   - `ssh user@edge-host`
   - `sudo bash scripts/install_frpc_ssh.sh --serverAddr <addr> --name <name>`
   - `python scripts/net_traffic_monitor.py --TRAFFIC_UPLOAD_URL <url> --traffic_api_post true &`
   - `python scripts/device_websocket_agent.py --wss-url <url> --device-type ACDN &`
3. **压测流量上报接口**
   - `python scripts/stress_test/traffic_upload_stress_test.py --requests 10000 --concurrency 1000`
   - 或 `wrk -t8 -c1000 -d30s -s scripts/stress_test/traffic_upload_wrk.lua http://...`
4. **更新帮助中心**
   - 编辑 `scripts/vitepress_project/docs/*.md`
   - `cd scripts/vitepress_project && pnpm run docs:build`
   - 部署 `dist_docs/` 或上传 `dist_docs.zip`。
5. **追加提交并同步多个远程仓库**
   - 将暂存区改动并入最近一次提交（保持提交信息不变），随后分别推送到 `origin` 的 `master` 分支与 `github` 的 `main` 分支：

   ```powershell
   git add . && git commit --amend --no-edit && git push --force-with-lease origin main:master && git push --force-with-lease github main:main
   ```
