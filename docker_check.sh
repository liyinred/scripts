#!/bin/bash
# JumpServer Docker 容器巡检脚本
# 检查镜像名称包含 lego-server 的所有容器
#
# 退出码：
# 0 - 所有匹配容器正常（输出简短结果）
# 1 - 容器不存在或状态异常
# 2 - Docker 命令不存在或执行失败

set -u

MATCH_KEY="lego-server"

# 1. 检查 Docker 命令
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker 命令不存在或不在 PATH 中" >&2
    exit 2
fi

# 2. 动态构造 Docker Go Template
# 避免 JumpServer 的 Jinja2 模板解析冲突
LEFT='{'
RIGHT='}'

DOCKER_FORMAT="${LEFT}${LEFT}.ID${RIGHT}${RIGHT}|${LEFT}${LEFT}.Image${RIGHT}${RIGHT}|${LEFT}${LEFT}.Status${RIGHT}${RIGHT}|${LEFT}${LEFT}.Names${RIGHT}${RIGHT}"

# 3. 获取所有容器信息
output=$(docker ps -a --format "$DOCKER_FORMAT" 2>&1)
rc=$?

if [ "$rc" -ne 0 ]; then
    echo "ERROR: docker ps -a 执行失败，退出码=$rc" >&2
    echo "$output" >&2
    exit 2
fi

total=0
abnormal=0
abnormal_lines=""

# 4. 检查匹配容器状态
while IFS='|' read -r id image status names; do

    [ -z "${id:-}" ] && continue

    # 仅匹配指定镜像
    case "$image" in
        *"$MATCH_KEY"*)
            ;;
        *)
            continue
            ;;
    esac

    total=$((total + 1))
    reason=""

    if [[ "$status" == Up* ]]; then

        if [[ "$status" == *"(Paused)"* ]]; then
            reason="容器被暂停(Paused)"

        elif [[ "$status" == *"(unhealthy)"* ]]; then
            reason="健康检查失败(unhealthy)"

        elif [[ "$status" == *"(health: starting)"* ]]; then
            # 健康检查初始化阶段暂不判定为异常
            reason=""

        else
            reason=""
        fi

    elif [[ "$status" == Exited* ]]; then

        exit_code=$(printf '%s' "$status" |
            sed -n 's/^Exited (\([0-9]*\)).*/\1/p')

        reason="容器已退出(退出码：${exit_code:-未知})"

    elif [[ "$status" == Restarting* ]]; then

        reason="容器反复重启"

    elif [[ "$status" == Dead* ]]; then

        reason="容器处于 Dead 状态"

    elif [[ "$status" == Created* ]]; then

        reason="容器创建后从未启动"

    elif [[ "$status" == Paused* ]]; then

        reason="容器被暂停(Paused)"

    else

        reason="未知状态"

    fi

    # 仅记录异常容器
    if [ -n "$reason" ]; then
        abnormal=$((abnormal + 1))

        abnormal_lines+="容器名=${names} | ID=${id} | 镜像=${image} | 状态=${status} | 异常原因=${reason}"$'\n'
    fi

done <<< "$output"

# 5. 输出巡检结果

# 情况一：未发现匹配容器
# 仅输出错误提示，不展示巡检报告块
if [ "$total" -eq 0 ]; then
    echo "ERROR: 未发现镜像名包含 '${MATCH_KEY}' 的容器"
    exit 1
fi

# 情况二：所有匹配容器正常
# 仅输出一行简短结果
if [ "$abnormal" -eq 0 ]; then
    echo "OK: ${MATCH_KEY} 容器全部正常（匹配容器数：${total}，异常容器数：0）"
    exit 0
fi

# 情况三：存在异常容器
# 输出完整巡检报告
echo "========== Docker 容器巡检 =========="
echo "巡检关键字：${MATCH_KEY}"
echo "匹配容器数：${total}"
echo "异常容器数：${abnormal}"
echo "====================================="
echo "---- 异常容器明细 ----"
printf "%s" "$abnormal_lines"

exit 1