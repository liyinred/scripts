# scripts

`pcdn-tx` 运维与辅助工具集，涵盖边缘节点部署、流量上报、网络配置、硬件检测、接口压测及静态资源打包。

主机运维脚本面向 Linux，多数部署脚本依赖 CentOS 7 / yum、systemd 和 root 权限。

## 目录与工具

| 文件 / 目录 | 用途 |
| --- | --- |
| [net_traffic_monitor.sh](net_traffic_monitor.sh) | 下载流量采集程序、替换旧服务并注册开机自启，配置北京时间与 NTP 同步，支持普通节点和算力服务器上报 |
| [net_traffic_monitor.py](net_traffic_monitor.py) | 按整 5 分钟窗口统计网卡上下行速率，处理 VLAN 重复计量，按月保存 SQLite 数据，并支持 API 上报、失败重试与历史补报 |
| [deploy_device_websocket.sh](deploy_device_websocket.sh) | 安装设备通道所需工具、下载 Agent 并清理旧进程，通过 systemd 管理服务启动、异常重启和开机自启 |
| [device_websocket_agent.py](device_websocket_agent.py) | 与后端维持 WebSocket 连接，响应系统信息采集、设备验收、出口限速和带宽测试指令，并提供远程终端会话 |
| [batch_vlan_setup.sh](batch_vlan_setup.sh) | 安装交互式 VLAN 批量配置及查询命令，配置双栈地址与策略路由，支持所选父接口下 VLAN 全量替换和开机恢复 |
| [device_upload_limit.sh](device_upload_limit.sh) | 按指定时间窗口设置或解除网卡出口限速，支持跨午夜时段、端口豁免及 cron 定时检查任务管理 |
| [hardware_check_report.sh](hardware_check_report.sh) | 汇总系统、CPU、内存、GPU、网卡及 SSD 信息，在符合空间条件的挂载点执行 fio 随机混合读写测试，输出硬件与 IOPS 报告 |
| [install_frpc_ssh.sh](install_frpc_ssh.sh) | 按主机架构下载 frpc，识别本机 SSH 端口、配置登录账户与密码认证，生成穿透配置并注册开机服务 |
| [install_show_qrcode.sh](install_show_qrcode.sh) | 根据 machine-id 派生设备 ID，安装小程序绑定二维码展示命令及开机服务，便于节点扫码绑定 |
| [package_dist.py](package_dist.py) | 将相邻 `frontend/dist/` 构建产物压缩为 `dist.zip`，保留目录层级并原子替换旧压缩包，用于前端资源发布 |
| [stress_test/](stress_test/) | 提供 Python asyncio 和 wrk / Lua 两种流量上报接口压测方案，模拟并发请求并统计吞吐量、状态码和延迟 |
| [vitepress_project/](vitepress_project/) | 小合云 VitePress 帮助中心源码，包含用户协议、计费 SLA 和发票说明，支持静态站点构建、打包及同步至前端目录 |
| `dist.zip` / `dist_docs.zip` | 分别保存前端页面与帮助中心的静态发布资源，供应用集成或静态站点部署 |

## 产物与文档站

- `dist.zip`：由 `package_dist.py` 打包相邻 `frontend/dist/` 生成，压缩包保留 `dist/` 层级。
- `dist_docs.zip`：帮助中心构建后的发布压缩包；静态文件位于 `vitepress_project/dist_docs/`，并同步至相邻 `frontend/dist_docs/`。
- 帮助中心：内容位于 `vitepress_project/docs/`，配置位于 `vitepress_project/docs/.vitepress/config.mts`，部署基础路径为 `/help/`。

## Git 提交与多远程同步

将当前目录改动加入暂存区并合并到最近一次提交，保持提交信息不变；随后将本地 `main` 分支分别推送到 `origin/master` 与 `github/main`：

```powershell
git add . && git commit --amend --no-edit && git push --force-with-lease origin main:master && git push --force-with-lease github main:main
```
