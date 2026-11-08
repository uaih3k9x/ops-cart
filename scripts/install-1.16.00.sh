#!/bin/bash
# 推车上一条命令装 1.16.00: sudo bash /tmp/deploy-1.16.00/install.sh
# 包的布局见 docs/DEPLOY_1.16.00.md 第 3 步. 只从 1.15.01 升; 不是就停 (FORCE=1 跳过版本检查).
# 不碰 /etc/ops-cart 里已有的配置; 新单元 cart-mcp-ro / cart-ble-input 只装不开.
set -Eeuo pipefail

PKG=$(cd "$(dirname "$0")" && pwd)
# 路径可用环境变量换 (只给 tests/test_install_script.py 用)
OPT=${OPT:-/opt/pxe}
SYS=${SYS:-/etc/systemd/system}
ETC=${ETC:-/etc/ops-cart}
LOG=${LOG:-/var/log/ops-cart}
FROM=1.15.01
TO=$(cat "$PKG/opt/VERSION")
NEW_UNITS="cart-mcp-ro.service cart-ble-input.service"
CHANGED_UNITS="cart-mcp.service"
# 改过代码、要重启的常驻服务 (开着才重启); kiosk 会回到锁屏
RESTART="cart-dash kiosk webkiosk kvm-broker pxe-server pxe-http-boot pxe-http-repo cart-mcp"
B=""

die() { echo "!! $*" >&2; exit 1; }

on_err() {
    echo >&2
    echo "!! 第 $1 行出错, 已停下." >&2
    if [ -n "$B" ] && [ -x "$B/rollback.sh" ]; then
        echo "!! 回滚: sudo bash $B/rollback.sh" >&2
    else
        echo "!! 备份前就停了, 推车上什么都没改." >&2
    fi
}
trap 'on_err $LINENO' ERR

[ "$(id -u)" = 0 ] || die "要 root: sudo bash $0"
[ -f "$PKG/opt/BUILD" ] || die "包里缺 BUILD"
grep -q -- '-dirty' "$PKG/opt/BUILD" && die "BUILD 带 -dirty, 包不是从干净提交打的"
CUR=$(cat "$OPT/VERSION" 2>/dev/null || echo "?")
if [ "$CUR" != "$FROM" ] && [ "${FORCE:-}" != 1 ]; then
    die "推车现在是 $CUR, 本包只从 $FROM 升 (确认无误再 FORCE=1 sudo -E bash $0)"
fi
# 有机器正解锁着: 重写默认菜单会把它的装机菜单一并删掉, 先等装完或在车机上上锁
if ls "$OPT"/tftp/grub.cfg-* >/dev/null 2>&1; then
    die "tftp 里有按 MAC 解锁的菜单 ($(ls "$OPT"/tftp/grub.cfg-* | xargs -n1 basename | tr '\n' ' ')), 先上锁再装"
fi

echo "==> $CUR ($(cat "$OPT/BUILD" 2>/dev/null)) -> $TO ($(cat "$PKG/opt/BUILD"))"

# ---- 1. 备份
TS=$(date +%Y%m%d-%H%M%S)
B=$OPT/.bak-$TS
mkdir -p "$B/etc/systemd" "$B/tftp"
cp -a "$OPT"/*.py "$OPT/VERSION" "$OPT/BUILD" "$OPT/web" "$OPT/icons" "$B/"
cp -a "$OPT/tftp/grub.cfg" "$B/tftp/"
for u in $CHANGED_UNITS; do cp -a "$SYS/$u" "$B/etc/systemd/"; done
cp -a "$ETC" "$B/etc/ops-cart"
cat > "$B/rollback.sh" <<EOF
#!/bin/bash
# 回滚到 $CUR (备份 $TS). 新加的 .py 留着无害, 旧代码不 import.
set -eu
B=$B
cp -a \$B/*.py \$B/VERSION \$B/BUILD $OPT/
rm -rf $OPT/web $OPT/icons && cp -a \$B/web \$B/icons $OPT/
cp -a \$B/tftp/grub.cfg $OPT/tftp/grub.cfg
for u in $NEW_UNITS; do systemctl disable --now \$u 2>/dev/null || true; rm -f $SYS/\$u; done
for u in $CHANGED_UNITS; do install -m 644 \$B/etc/systemd/\$u $SYS/\$u; done
systemctl daemon-reload
for s in $RESTART; do if systemctl is-active --quiet \$s; then systemctl restart \$s; fi; done
echo "回滚完: \$(cat $OPT/VERSION) \$(cat $OPT/BUILD)"
EOF
chmod 700 "$B/rollback.sh"
echo "==> 备份: $B"

# ---- 2. 代码和静态资源
install -m 644 "$PKG"/opt/*.py "$PKG/opt/VERSION" "$PKG/opt/BUILD" "$OPT/"
cp -r "$PKG/opt/web/." "$OPT/web/"
cp -r "$PKG/opt/icons/." "$OPT/icons/"

# ---- 3. 单元: 改过的覆盖, 新的只装不开
for u in $CHANGED_UNITS $NEW_UNITS; do install -m 644 "$PKG/systemd/$u" "$SYS/$u"; done
systemctl daemon-reload

# ---- 4. /etc/ops-cart: 这一版没有必需的新文件, 只看不写
for f in pxe.env pet-skin.json serial.conf kvm.json mcp.json serial-gate.json; do
    if [ -e "$ETC/$f" ]; then echo "    $ETC/$f 保留原样"; else echo "    $ETC/$f 没有 (这版不新建)"; fi
done

# ---- 5. PXE 默认菜单: 超时本地盘, 救援手选 (#76)
python3 "$OPT/kiosk_core.py" --write-rescue --root "$OPT/tftp"
grep -q "Boot from local disk" "$OPT/tftp/grub.cfg" || die "新 grub.cfg 里没有本地盘项"

# ---- 6. 重启 (开着的才重启)
for s in $RESTART; do
    if systemctl is-active --quiet "$s"; then systemctl restart "$s"; echo "    重启 $s"; fi
done
sleep "${SETTLE:-3}"
FAILED=""
for s in $RESTART; do
    if systemctl is-enabled --quiet "$s" 2>/dev/null && ! systemctl is-active --quiet "$s"; then FAILED="$FAILED $s"; fi
done
[ -z "$FAILED" ] || die "重启后没起来:$FAILED (journalctl -u <名字> -n 50)"
trap - ERR

# ---- 7. 验收 (只读; FAIL 不回滚, 看完再定)
echo "==> 验收"
REPORT=$LOG/accept-$TO-$TS.txt
python3 "$PKG/accept/cart_accept.py" --expect-version "$TO" --expect-build "$(cat "$PKG/opt/BUILD")" \
    | tee "$REPORT" || echo "!! 验收脚本退出码非 0, 看 $REPORT"

cat <<EOF

==================== 装完: $TO ($(cat "$OPT/BUILD")) ====================
备份      $B
回滚      sudo bash $B/rollback.sh
验收报告  $REPORT

要在车边看的:
  [ ] 车机回到锁屏, 解锁后界面正常; 网络唤醒面板"名字"框按 Shift 显示"英·无中文" (librime 没装, 正常)
  [ ] 车机开关按钮 (UVC 预览 / 串口会话 / PXE) 文字随开关切换 (#84)
  [ ] 副屏: 红条不再压住昨夜整理 (#84)
  [ ] 被测机 PXE 进默认菜单, 不按键等 10 秒, 从本地盘启动, 没有回 PXE 循环、没停在固件界面 (#76)
  [ ] 手选 Rescue shell 还能进救援 (#76)
  [ ] 串口发一条命令后 serial-slot<N>.log 里有 gate.shadow (#78)
  [ ] 车机确认框里挂起项带设备上下文, 字号不挤掉"确认发送"按钮 (#67 #68)
  [ ] 串口确认框里有"设备最后一行（不可信）"这一块, "确认发送"按钮没被挤掉 (#74)
  [ ] 车机真屏跑确认框 Tk 测试 (命令见 docs/DEPLOY_1.16.00.md 第 8 步, skip 不算过)
没开的 (要用再手工开, 见 docs/DEPLOY_1.16.00.md 第 6 步):
  cart-mcp-ro.service (要先在 mcp.json 加 readonly 段), cart-ble-input.service (要先配对 + trust)
EOF
