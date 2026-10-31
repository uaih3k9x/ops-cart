#!/bin/bash
# 数据盘 /srv/data + 装机库 ISO 只读挂载 (#11). root 运行, 幂等.
#   setup-srv-data.sh <UUID>      装 srv-data.mount (按 UUID, XFS, nofail) + 目录布局 + ISO 挂载单元
#   setup-srv-data.sh --rescan    /srv/data/iso 里加了 / 删了 ISO 之后重跑: 只同步 ISO 挂载单元
# 本脚本不格式化、不分区 (那几步在 docs/DATA_DISK.md, 人自己确认后手敲).
set -euo pipefail

BASE=/opt/pxe
SRC=$(cd "$(dirname "$0")/.." && pwd)        # 仓库 (或拷到车上的同样布局) 目录: 拿 deploy/srv-data
CORE=$SRC/srv_data_core.py; [ -f "$CORE" ] || CORE=$BASE/srv_data_core.py
UNITS=/etc/systemd/system
[ "$(id -u)" = 0 ] || { echo "要 root"; exit 1; }
[ $# = 1 ] || { echo "用法: $0 <数据盘 UUID> | --rescan"; exit 1; }

sync_iso() {
    if ! mountpoint -q /srv/data; then
        echo "数据盘没挂上 (/srv/data 不是挂载点), ISO 先不挂"
        return 0
    fi
    echo "==> 同步 ISO 挂载单元 (/srv/data/iso/*.iso -> /srv/data/win/<名>/)"
    local out
    out=$(python3 "$CORE" iso-units --iso-dir /srv/data/iso --unit-dir "$UNITS")
    echo "$out"
    systemctl daemon-reload
    while read -r op unit; do
        [ -n "$op" ] || continue
        if [ "$op" = write ]; then
            systemctl enable "$unit"
            systemctl restart "$unit" || echo "!! $unit 没挂上, 看 journalctl -u '$unit'"
        fi
    done <<< "$out"
    # 删掉的单元: 文件已删, 停掉还挂着的
    while read -r op unit; do
        [ "$op" = remove ] || continue
        systemctl stop "$unit" 2>/dev/null || true
        rm -f "$UNITS/multi-user.target.wants/$unit"
    done <<< "$out"
    systemctl daemon-reload
}

if [ "$1" = --rescan ]; then
    sync_iso
    exit 0
fi

python3 "$CORE" data-unit "$1" >/dev/null       # 不像 UUID 就在这停 (退出码 2)
UUID=$(printf '%s' "$1" | tr 'A-F' 'a-f' | tr -d '[:space:]')
DEV=/dev/disk/by-uuid/$UUID
if [ -e "$DEV" ]; then
    FSTYPE=$(blkid -o value -s TYPE "$DEV" || true)
    [ "$FSTYPE" = xfs ] || { echo "$DEV 是 '$FSTYPE' 不是 xfs; UUID 抄错了? 没格式化? (见 docs/DATA_DISK.md)"; exit 1; }
    echo "==> 数据盘: $(readlink -f "$DEV") ($FSTYPE)"
else
    echo "!! 现在找不到 $DEV (盘没插 / UUID 抄错). 照样装单元; 插上后 systemctl start srv-data.mount"
fi

echo "==> 组 ops-smb (可写目录属组)"
getent group ops-smb >/dev/null || groupadd -r ops-smb

echo "==> 单元: srv-data.mount + srv-data-layout.service"
python3 "$CORE" data-unit "$UUID" > "$UNITS/srv-data.mount"
install -m 644 "$SRC/deploy/srv-data/srv-data-layout.service" "$UNITS/"
install -d -m 755 /etc/ops-cart
install -m 644 "$SRC/deploy/srv-data/srv-data.tmpfiles.conf" /etc/ops-cart/srv-data.tmpfiles.conf
mkdir -p /srv/data

echo "==> SELinux: /srv/data 下打 samba_share_t (smbd 才读写得了)"
if command -v semanage >/dev/null; then
    semanage fcontext -l | grep -qF '/srv/data(/.*)?' \
        || semanage fcontext -a -t samba_share_t '/srv/data(/.*)?'
else
    echo "!! 没有 semanage (dnf install policycoreutils-python-utils), SELinux 标签没加"
fi

systemctl daemon-reload
systemctl enable srv-data.mount srv-data-layout.service
if systemctl restart srv-data.mount; then
    systemctl restart srv-data-layout.service
    # ISO 挂载点是只读的, 不 relabel (挂载时带 context=)
    command -v restorecon >/dev/null && restorecon -R -e /srv/data/win /srv/data || true
    df -h /srv/data
    sync_iso
else
    echo "!! srv-data.mount 没挂上: journalctl -u srv-data.mount. 开机照常 (nofail), PXE 不受影响"
fi
echo "完成. 加了 ISO 后: $0 --rescan"
