#!/usr/bin/env bash
set -euo pipefail

ALLOW=/etc/hosts.allow
DENY=/etc/hosts.deny

# SSH 白名单：每个位置参数传入一个 IPv4 地址。
# 用法：sudo bash ssh_acl_fix.sh <IPv4> [IPv4 ...]
IPS=("$@")

declare -A EXPECT=() EXIST=()
MISSING=() EXTRA=() DUP=()
TMP_A="" TMP_D="" BACKUP=""

# 功能：输出错误并终止脚本；参数：错误信息；返回：以状态码 1 退出。
die() { echo "✗ $*" >&2; exit 1; }

# 功能：清理尚未替换的临时文件；参数：无；返回：清理命令的状态码。
cleanup() {
    [[ -z $TMP_A ]] || rm -f "$TMP_A"
    [[ -z $TMP_D ]] || rm -f "$TMP_D"
}
trap cleanup EXIT

(($#)) || die "白名单为空；用法: sudo bash ssh_acl_fix.sh <IPv4> [IPv4 ...]"
[[ $EUID -eq 0 ]] || die "请使用 root 执行"
[[ -f $ALLOW && -f $DENY ]] || die "配置文件不存在"

# 验证白名单
for ip in "${IPS[@]}"; do
    [[ $ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] ||
        die "非法IP: $ip"

    IFS=. read -r a b c d <<< "$ip"
    for n in "$a" "$b" "$c" "$d"; do
        [[ $n != 0* || $n == 0 ]] || die "IP格式错误: $ip"
        ((10#$n <= 255)) || die "非法IP: $ip"
    done

    # Bash 4.2 的 -v 不支持数组元素判断，使用参数展开检查键是否存在。
    [[ -z ${EXPECT[$ip]+present} ]] || die "重复白名单IP: $ip"
    EXPECT[$ip]=1
done

# 功能：解析 SSH IPv4 允许规则并保留原有注释；参数：文件路径、list 或 keep 模式；返回：输出 IP 或非托管内容。
process_rules() {
    awk -F: -v mode="$2" '
    /^[[:space:]]*#/ {
        if (mode=="keep") print
        next
    }
    {
        s=$1; ip=$2; a=$3
        gsub(/[[:space:]]/, "", s)
        gsub(/[[:space:]]/, "", ip)
        sub(/[[:space:]]*#.*/, "", a)
        gsub(/[[:space:]]/, "", a)
        rule=(NF==3 && tolower(s)=="sshd" && tolower(a)=="allow" &&
              ip ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/)
        if (mode=="list" && rule) print ip
        if (mode=="keep" && !rule) print
    }' "$1"
}

# 功能：按格式逐项输出 IP，空列表不输出；参数：printf 格式、IP 列表；返回：输出命令的状态码。
print_ips() {
    local format=$1
    shift
    (($# == 0)) || printf "$format" "$@"
}

# 功能：输出一类 IP 检查结果；参数：类别名称、IP 列表；返回：输出命令的状态码。
report_ips() {
    local label=$1
    shift
    printf '\n=== %s规则 ===\n' "$label"
    if (($#)); then
        print_ips "✗ ${label}IP: %s\n" "$@"
    else
        printf '✓ 无%sIP\n' "$label"
    fi
}

# 功能：检查 SSH 默认拒绝规则；参数：配置文件路径；返回：存在为 0，否则为 1。
check_deny() {
    grep -Eiq '^[[:space:]]*sshd[[:space:]]*:[[:space:]]*all[[:space:]]*:[[:space:]]*deny[[:space:]]*(#.*)?$' "$1"
}

# 功能：收集缺失、额外和重复规则；参数：allow、deny 文件路径；返回：全部符合为 0，否则为 1。
check() {
    local ip
    EXIST=()
    MISSING=()
    EXTRA=()
    DUP=()
    DENY_OK=0

    while IFS= read -r ip; do
        [[ -n $ip ]] || continue
        EXIST[$ip]=$(( ${EXIST[$ip]:-0} + 1 ))
    done < <(process_rules "$1" list)

    for ip in "${IPS[@]}"; do
        [[ -n ${EXIST[$ip]+present} ]] || MISSING+=("$ip")
    done

    for ip in "${!EXIST[@]}"; do
        [[ -n ${EXPECT[$ip]+present} ]] || EXTRA+=("$ip")
        (( EXIST[$ip] <= 1 )) || DUP+=("$ip")
    done

    check_deny "$2" && DENY_OK=1

    ((${#MISSING[@]} == 0 && ${#EXTRA[@]} == 0 &&
      ${#DUP[@]} == 0 && DENY_OK == 1))
}

# ==================== 检查 ====================
echo "========== SSH 访问控制检查 =========="

ISSUES=0
check "$ALLOW" "$DENY" || ISSUES=1

echo
echo "=== hosts.deny ==="
if ((DENY_OK)); then
    echo "✓ 拒绝规则正常"
else
    echo "✗ 缺少 sshd:ALL:deny"
fi

echo
echo "=== hosts.allow ==="
echo "期望IP: ${#IPS[@]}，现有IP: ${#EXIST[@]}"

report_ips 缺失 ${MISSING[@]+"${MISSING[@]}"}
report_ips 额外 ${EXTRA[@]+"${EXTRA[@]}"}

echo
echo "=== 重复规则 ==="
if ((${#DUP[@]})); then
    for ip in "${DUP[@]}"; do
        echo "✗ 重复IP: $ip (${EXIST[$ip]}次)"
    done
else
    echo "✓ 无重复IP"
fi

if ((ISSUES == 0)); then
    echo
    echo "✓ 所有检查通过，无需修复"
    exit 0
fi

# 保存修复前的问题
# Bash 4.2 在 set -u 下直接展开空数组会报错；仅在存在元素时展开。
OLD_MISSING=(${MISSING[@]+"${MISSING[@]}"})
OLD_EXTRA=(${EXTRA[@]+"${EXTRA[@]}"})
OLD_DUP=(${DUP[@]+"${DUP[@]}"})
OLD_DENY=$DENY_OK

# ==================== 自动修复 ====================
echo
echo "========== SSH 访问控制修复 =========="

# 防止当前 SSH 来源被白名单阻断
if [[ -n ${SSH_CONNECTION:-} ]]; then
    CLIENT=${SSH_CONNECTION%% *}
    [[ -n ${EXPECT[$CLIENT]+present} ]] ||
        die "当前SSH来源 $CLIENT 不在白名单，取消修复"
else
    echo "⚠ 无法确认SSH来源，请确保具备恢复通道"
fi

# 备份原始文件
BACKUP=$(mktemp -d /etc/ssh-acl-backup.XXXXXXXX)
chmod 700 "$BACKUP"
cp -p "$ALLOW" "$BACKUP/hosts.allow"
cp -p "$DENY" "$BACKUP/hosts.deny"

TMP_A=$(mktemp /etc/.hosts.allow.XXXXXX)
TMP_D=$(mktemp /etc/.hosts.deny.XXXXXX)

# 继承原文件权限和所有者
chmod --reference="$ALLOW" "$TMP_A"
chown --reference="$ALLOW" "$TMP_A"
chmod --reference="$DENY" "$TMP_D"
chown --reference="$DENY" "$TMP_D"

# 删除旧 SSH IP 允许规则
process_rules "$ALLOW" keep > "$TMP_A"

# 写入标准白名单
printf '\n' >> "$TMP_A"
for ip in "${IPS[@]}"; do
    printf 'sshd:%s:allow\n' "$ip" >> "$TMP_A"
done

# 补充拒绝规则
cp "$DENY" "$TMP_D"
if ((OLD_DENY == 0)); then
    printf '\nsshd:ALL:deny\n' >> "$TMP_D"
fi

# 写入前验证临时配置
check "$TMP_A" "$TMP_D" ||
    die "新配置验证失败，原文件未修改"

# 功能：恢复原始访问控制文件并终止；参数：无；返回：以状态码 1 退出。
rollback() {
    cp -p "$BACKUP/hosts.allow" "$ALLOW"
    cp -p "$BACKUP/hosts.deny" "$DENY"
    die "修复验证失败，已尝试恢复原配置，备份: $BACKUP"
}

# 替换并验证
mv -f "$TMP_A" "$ALLOW" || rollback
TMP_A=""
mv -f "$TMP_D" "$DENY" || rollback
TMP_D=""

check "$ALLOW" "$DENY" || rollback

# ==================== 修复结果 ====================
echo "✓ 自动修复成功"

print_ips '✓ 新增IP: %s\n' ${OLD_MISSING[@]+"${OLD_MISSING[@]}"}
print_ips '✓ 移除IP: %s\n' ${OLD_EXTRA[@]+"${OLD_EXTRA[@]}"}
print_ips '✓ 清理重复IP: %s\n' ${OLD_DUP[@]+"${OLD_DUP[@]}"}

if ((OLD_DENY == 0)); then
    echo "✓ 补充 sshd:ALL:deny"
fi

echo "✓ 备份目录: $BACKUP"
echo
echo "========== 修复后验证 =========="
echo "✓ 白名单: ${#EXIST[@]}/${#IPS[@]}"
echo "✓ 无缺失、额外或重复IP"
echo "✓ hosts.deny 拒绝规则正常"
