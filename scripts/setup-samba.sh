#!/bin/bash
# 推车 Samba (#11): smb.conf 由 samba_core.py 生成, 建账号, firewalld 只放 LAN1 网段的 445. root 运行, 幂等.
#   setup-samba.sh
# 装机账号 winpe 的密码: /etc/ops-cart/smb-winpe.pass (root 0600, 一行) 有就用, 没有就交互输入
#   (已有这个账号时直接回车 = 不改密码). 克隆账号和密码照 /opt/pxe/clone.json (#18 内核命令行里用的同一份).
# 先跑 setup-srv-data.sh (数据盘没挂上时 smbd 不起, 见 smb.service.d/ops-cart.conf).
set -euo pipefail

BASE=/opt/pxe
SRC=$(cd "$(dirname "$0")/.." && pwd)
CORE=$SRC/samba_core.py; [ -f "$CORE" ] || CORE=$BASE/samba_core.py
ENV=/etc/ops-cart/pxe.env
CLONE=$BASE/clone.json
PASSFILE=/etc/ops-cart/smb-winpe.pass
LAN1=eno1

[ "$(id -u)" = 0 ] || { echo "要 root"; exit 1; }
rpm -q samba >/dev/null || { echo "没装 samba: dnf install samba"; exit 1; }
[ -f "$ENV" ] || { echo "缺 $ENV (照 deploy/pxe.env.example 写)"; exit 1; }
mountpoint -q /srv/data || echo "!! /srv/data 还没挂数据盘: 配置照写, smbd 要等数据盘挂上才起"
[ -f "$CLONE" ] && echo "==> 有 $CLONE: 克隆共享照它生成" || echo "==> 没有 $CLONE: 只生成装机共享"

echo "==> smb.conf"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
python3 "$CORE" render --env "$ENV" --clone "$CLONE" > "$TMP"
testparm -s "$TMP" >/dev/null 2>&1 || { testparm -s "$TMP"; echo "生成的 smb.conf 过不了 testparm"; exit 1; }
if [ -f /etc/samba/smb.conf ] && ! grep -q 'setup-samba.sh' /etc/samba/smb.conf; then
    cp -n /etc/samba/smb.conf /etc/samba/smb.conf.orig-ops-cart      # 系统原来那份留一个底
fi
install -m 644 "$TMP" /etc/samba/smb.conf

echo "==> 账号"
getent group ops-smb >/dev/null || groupadd -r ops-smb
set_pass() {    # $1 账号, stdin 两行密码
    smbpasswd -a -s "$1" >/dev/null
    smbpasswd -e "$1" >/dev/null
}
python3 "$CORE" users --clone "$CLONE" | while read -r user mode; do
    id -u "$user" >/dev/null 2>&1 || useradd -r -M -d /nonexistent -s /sbin/nologin "$user"
    [ "$mode" = rw ] && usermod -aG ops-smb "$user"
    if [ "$user" = winpe ]; then
        if [ -f "$PASSFILE" ]; then
            [ "$(stat -c %a "$PASSFILE")" = 600 ] || { echo "$PASSFILE 要 chmod 600"; exit 1; }
            pw=$(head -n1 "$PASSFILE")
        else
            exists=$(pdbedit -L -u winpe >/dev/null 2>&1 && echo y || echo n)
            read -rsp "装机账号 winpe 的密码 (PE 里 net use 要敲$([ "$exists" = y ] && echo '; 回车 = 不改')): " pw </dev/tty; echo
            if [ -z "$pw" ] && [ "$exists" = y ]; then echo "   winpe 密码不改"; continue; fi
            read -rsp "再输一遍: " pw2 </dev/tty; echo
            [ "$pw" = "$pw2" ] || { echo "两遍不一样"; exit 1; }
        fi
        [ -n "$pw" ] || { echo "winpe 密码不能空"; exit 1; }
        printf '%s\n%s\n' "$pw" "$pw" | set_pass winpe
    else
        python3 "$CORE" clone-password --clone "$CLONE" "$user" | set_pass "$user"
    fi
    echo "   $user ($mode) 好了"
done

echo "==> systemd: smbd 等数据盘; LAN1 来地址时重启 smbd 绑上"
install -d -m 755 /etc/systemd/system/smb.service.d
install -m 644 "$SRC/deploy/srv-data/smb-ops-cart.conf" /etc/systemd/system/smb.service.d/ops-cart.conf
install -m 755 "$SRC/deploy/srv-data/50-ops-cart-smb" /etc/NetworkManager/dispatcher.d/50-ops-cart-smb
command -v restorecon >/dev/null && restorecon /etc/NetworkManager/dispatcher.d/50-ops-cart-smb || true

echo "==> firewalld: 只放 LAN1 网段来的 445"
if systemctl is-active -q firewalld; then
    RULE=$(python3 "$CORE" rich-rule --env "$ENV")
    ZONE=$(firewall-cmd --get-zone-of-interface="$LAN1" 2>/dev/null || true)
    if [ -z "$ZONE" ]; then
        echo "!! $LAN1 不在任何 firewalld 区域, 没加规则. 要人定: 把 $LAN1 放进哪个区 (见 docs/DATA_DISK.md)"
    else
        [ "$ZONE" = "$(firewall-cmd --get-zone-of-interface=enp3s0 2>/dev/null || true)" ] \
            && echo "!! 注意: LAN1 和 LAN2 在同一个区 $ZONE; 规则带了源网段限制, 但最好分开 (docs/DATA_DISK.md)"
        firewall-cmd -q --permanent --zone="$ZONE" --query-rich-rule="$RULE" \
            || firewall-cmd -q --permanent --zone="$ZONE" --add-rich-rule="$RULE"
        firewall-cmd -q --reload
        echo "   区 $ZONE: $RULE"
    fi
else
    echo "   firewalld 没在跑, 跳过 (smbd 本身只绑 LAN1 + hosts allow)"
fi

systemctl daemon-reload
systemctl enable smb.service
if mountpoint -q /srv/data; then
    systemctl restart smb.service
    systemctl --no-pager --lines=0 status smb.service || true
fi
IP=$(sed -n 's/^PXE_IP=//p' "$ENV" | tr -d "\"'")
echo "完成. PE 里: net use \\\\$IP\\win /user:winpe <密码>   (另有 \\drivers \\tools)"
