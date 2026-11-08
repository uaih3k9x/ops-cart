#!/bin/bash
# 推车上一条命令装 1.16.00. 用 systemd-run 跑, 不依赖 ssh 会话 (断线 / 关终端照样跑完); 脚本没有交互:
#   sudo systemd-run --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.00/install.sh
#   journalctl -fu ops-cart-deploy          # 看进度
# 包的布局见 docs/DEPLOY_1.16.00.md 第 3 步. 只从 1.15.01 升; 不是就停 (跳过版本检查: sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash .../install.sh).
# 不碰 /etc/ops-cart 里已有的配置; 新单元 cart-mcp-ro / cart-ble-input 只装不开.
set -Eeuo pipefail

PKG=$(cd "$(dirname "$0")" && pwd)
# 路径可用环境变量换 (只给 tests/test_install_script.py 用)
OPT=${OPT:-/opt/pxe}
SYS=${SYS:-/etc/systemd/system}
ETC=${ETC:-/etc/ops-cart}
LOG=${LOG:-/var/log/ops-cart}
FROM=1.15.01
EXPECT_TO=1.16.00
TO=$(cat "$PKG/opt/VERSION" 2>/dev/null || true)
NEW_UNITS="cart-mcp-ro.service cart-ble-input.service"
CHANGED_UNITS="cart-mcp.service"
# 改过代码、要重启的常驻服务 (部署前开着的才重启); kiosk 会回到锁屏
RESTART="cart-dash kiosk webkiosk kvm-broker pxe-server pxe-http-boot pxe-http-repo cart-mcp"
B=""
DONE=""
FORCE_CMD="sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash $0"

rollback_cmd() { echo "sudo systemd-run --unit=ops-cart-rollback --collect bash $1/rollback.sh   (看进度: journalctl -fu ops-cart-rollback)"; }
hint_rollback() {
    if [ -n "$DONE" ]; then
        echo "!! 已经装完, 只是验收被打断. 不用回滚; 重跑验收: sudo python3 $PKG/accept/cart_accept.py --expect-version $TO" >&2
    elif [ -n "$B" ] && [ -x "$B/rollback.sh" ]; then
        echo "!! 已经装了一半. 回滚: $(rollback_cmd "$B")" >&2
    else
        # 备份做到一半 (rollback.sh 还没写好): 推车上的文件还没动, 半截备份删掉, 免得被当成"上次装过"
        case "$B" in "$OPT"/.bak-*) rm -rf "$B";; esac
        echo "!! 还没开始换文件就停了, 推车上什么都没改." >&2
    fi
}
# die 是 exit 1, 不走 ERR trap: 自己打回滚提示
die() { echo "!! $*" >&2; hint_rollback; exit 1; }
on_err() { echo >&2; echo "!! 第 $1 行出错, 已停下." >&2; hint_rollback; }
trap 'on_err $LINENO' ERR
# ssh 断线 / Ctrl-C / systemctl stop: 不走 ERR, 也要给回滚提示
trap 'echo >&2; echo "!! 被信号打断." >&2; hint_rollback; exit 130' INT TERM HUP

[ "$(id -u)" = 0 ] || die "要 root: sudo bash $0"
[ -f "$PKG/opt/BUILD" ] || die "包里缺 BUILD"
grep -q -- '-dirty' "$PKG/opt/BUILD" && die "BUILD 带 -dirty, 包不是从干净提交打的"
[ "$TO" = "$EXPECT_TO" ] || die "包里 VERSION 是 '$TO', 不是 $EXPECT_TO"
for f in $(for u in $CHANGED_UNITS $NEW_UNITS; do echo "systemd/$u"; done) accept/cart_accept.py accept/accept_core.py \
         accept/accept-manifest.json opt/kiosk_core.py; do
    [ -f "$PKG/$f" ] || die "包不全: 缺 $f"
done
# 包内文件和 manifest 对得上 (sha256, build); 拷过来没损坏、没混进别的版本
python3 -I - "$PKG" "${MIN_OPT_FILES:-60}" <<'PY' || die "包内文件和 accept/accept-manifest.json 对不上 (上面列了哪几个)"
import hashlib, json, os, sys
pkg, min_opt = sys.argv[1], int(sys.argv[2])
mpath = os.path.join(pkg, "accept", "accept-manifest.json")
raw = open(mpath, "rb").read()
print("    manifest sha256 " + hashlib.sha256(raw).hexdigest())
m = json.loads(raw)
bad = []
files = m.get("files") or {}
if not files:
    bad.append("manifest 的 files 是空的")
n_opt = sum(1 for d in files if d.startswith("/opt/pxe/"))
if n_opt < min_opt:
    bad.append("manifest 只列了 %d 个 /opt/pxe 文件, 少于 %d" % (n_opt, min_opt))
build = open(os.path.join(pkg, "opt", "BUILD")).read().strip()
if m.get("build") != build:
    bad.append("BUILD %r != manifest build %r" % (build, m.get("build")))
units = set(os.listdir(os.path.join(pkg, "systemd")))
n_units = sum(1 for d in files if d.startswith("/etc/systemd/system/") and os.path.basename(d) in units)
if n_units < len(units):
    bad.append("包里 %d 个单元, manifest 只列了 %d 个" % (len(units), n_units))
for dest, want in sorted(files.items()):
    if dest.startswith("/opt/pxe/"):
        src = os.path.join(pkg, "opt", dest[len("/opt/pxe/"):])
    elif dest.startswith("/etc/systemd/system/") and os.path.basename(dest) in units:
        src = os.path.join(pkg, "systemd", os.path.basename(dest))
    else:
        continue                                    # 包里不带的系统件 (字体、sysctl ...), 验收时再比
    try:
        got = hashlib.sha256(open(src, "rb").read()).hexdigest()
    except OSError:
        bad.append("缺 " + src)
        continue
    if got != want:
        bad.append("sha256 不一致 " + src)
listed = {d for d in files}
extra = []
for root, _, names in os.walk(os.path.join(pkg, "opt")):
    for n in names:
        rel = os.path.relpath(os.path.join(root, n), os.path.join(pkg, "opt")).replace(os.sep, "/")
        if rel != "BUILD" and "/opt/pxe/" + rel not in listed:     # BUILD 上面单独比过 manifest 的 build
            extra.append("opt/" + rel)
extra += ["systemd/" + u for u in sorted(units) if "/etc/systemd/system/" + u not in listed]
for e in sorted(extra):
    print("    警告: 包里有, manifest 没列 (照样装, 不核对): " + e, file=sys.stderr)
for b in bad:
    print("!!   " + b, file=sys.stderr)
sys.exit(1 if bad else 0)
PY
CUR=$(cat "$OPT/VERSION" 2>/dev/null || echo "?")
# 上次装到一半 (断线 / 被打断) 又重跑: 先回滚或查明, 不叠着装
LAST=$(grep -l "^to=$TO$" "$OPT"/.bak-*/deploy.marker 2>/dev/null | sort | tail -n 1 || true)
if [ "$CUR" = "$TO" ] || [ -n "$LAST" ]; then
    if [ -n "$LAST" ] && grep -qx "done=1" "$LAST"; then
        echo "!! 推车现在是 $CUR; $TO 已经装完了 ($LAST). 不用再装." >&2
    else
        echo "!! 推车现在是 $CUR; 已经有一次装 $TO 的记录${LAST:+ ($LAST)}. 上次可能没装完." >&2
        if [ -n "$LAST" ]; then echo "!! 要回滚: $(rollback_cmd "$(dirname "$LAST")")" >&2; fi
    fi
    echo "!! 推车上这次什么都没改." >&2
    exit 1
fi
for u in $NEW_UNITS; do
    [ ! -e "$SYS/$u" ] || die "$SYS/$u 已经有了 (1.15.01 上不该有), 先查是谁装的"
done
if [ "$CUR" != "$FROM" ] && [ "${FORCE:-}" != 1 ]; then
    die "推车现在是 $CUR, 本包只从 $FROM 升 (确认无误再 $FORCE_CMD)"
fi
# 有机器正解锁着: 重写默认菜单会把它的装机菜单一并删掉, 先等装完或在车机上上锁
if ls "$OPT"/tftp/grub.cfg-* >/dev/null 2>&1; then
    die "tftp 里有按 MAC 解锁的菜单 ($(ls "$OPT"/tftp/grub.cfg-* | xargs -n1 basename | tr '\n' ' ')), 先上锁再装"
fi

echo "==> $CUR ($(cat "$OPT/BUILD" 2>/dev/null)) -> $TO ($(cat "$PKG/opt/BUILD"))"

# 现在开着的服务: 装完重启它们, 回滚时也无条件重启它们 (新代码起不来停掉的也要拉回来)
ACTIVE=""
for s in $RESTART; do if systemctl is-active --quiet "$s"; then ACTIVE="$ACTIVE $s"; fi; done

# ---- 1. 备份
TS=$(date +%Y%m%d-%H%M%S)
B=$OPT/.bak-$TS
mkdir -p "$B/etc/systemd" "$B/tftp"
cp -a "$OPT"/*.py "$OPT/VERSION" "$OPT/BUILD" "$OPT/web" "$OPT/icons" "$B/"
# PXE 菜单整批备份: tftp 树里所有 grub.cfg* (含 EFI/BOOT 下的), 保留相对路径
(cd "$OPT/tftp" && find . -name 'grub.cfg*' -type f) | while IFS= read -r f; do
    mkdir -p "$B/tftp/$(dirname "$f")"; cp -a "$OPT/tftp/$f" "$B/tftp/$f"
done
if [ -f "$OPT/pxe-mode.json" ]; then cp -a "$OPT/pxe-mode.json" "$B/"; fi
for u in $CHANGED_UNITS; do cp -a "$SYS/$u" "$B/etc/systemd/"; done
cp -a "$ETC" "$B/etc/ops-cart"
cat > "$B/rollback.sh" <<EOF
#!/bin/bash
# 回滚到 $CUR (备份 $TS). 新加的 .py 留着无害, 旧代码不 import.
set -Eeu
trap 'echo "!! 回滚第 \$LINENO 行失败, 修好后可直接重跑" >&2' ERR
B=$B
cp -a \$B/*.py \$B/VERSION \$B/BUILD $OPT/
rm -rf $OPT/web $OPT/icons && cp -a \$B/web \$B/icons $OPT/
# PXE 菜单整批换回备份时那一套; pxe-mode.json 备份时没有就删
find $OPT/tftp -name 'grub.cfg*' -type f -delete
(cd \$B/tftp && find . -type f) | while IFS= read -r f; do   # 逐个文件拷回, 不动 tftp 目录本身的属性 / SELinux 标签
    mkdir -p "$OPT/tftp/\$(dirname "\$f")"; cp -a "\$B/tftp/\$f" "$OPT/tftp/\$f"
done
if [ -f \$B/pxe-mode.json ]; then cp -a \$B/pxe-mode.json $OPT/; else rm -f $OPT/pxe-mode.json; fi
for u in $NEW_UNITS; do systemctl disable --now \$u 2>/dev/null || true; rm -f $SYS/\$u; done
for u in $CHANGED_UNITS; do install -m 644 \$B/etc/systemd/\$u $SYS/\$u; done
systemctl daemon-reload
# 部署前开着的这几个: 不看现在状态, 直接重启
BAD=""
for s in $ACTIVE; do systemctl restart \$s || BAD="\$BAD \$s"; done
if [ -f \$B/deploy.marker ]; then mv \$B/deploy.marker \$B/deploy.marker.rolled-back; fi   # 回滚完可以再装
echo "回滚完: \$(cat $OPT/VERSION) \$(cat $OPT/BUILD)"
if [ -n "\$BAD" ]; then echo "!! 这些没重启成功:\$BAD (journalctl -u <名字> -n 50)" >&2; exit 1; fi
EOF
chmod 700 "$B/rollback.sh"
# rollback.sh 写好之后才落 marker: 有 marker 就一定有能用的回滚
printf 'from=%s\nto=%s\nbuild=%s\n' "$CUR" "$TO" "$(cat "$PKG/opt/BUILD")" > "$B/deploy.marker"
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
python3 "$OPT/kiosk_core.py" --write-rescue --root "$OPT/tftp" --state "$OPT/pxe-mode.json"
grep -q "Boot from local disk" "$OPT/tftp/grub.cfg" || die "新 grub.cfg 里没有本地盘项"

# ---- 6. 重启 (开着的才重启)
for s in $ACTIVE; do systemctl restart "$s"; echo "    重启 $s"; done
sleep "${SETTLE:-3}"
# 只有部署前开着的才算失败; 部署前就没在跑的只提一句
FAILED=""
for s in $ACTIVE; do if ! systemctl is-active --quiet "$s"; then FAILED="$FAILED $s"; fi; done
for s in $RESTART; do
    case " $ACTIVE " in *" $s "*) ;; *) echo "    $s 部署前就没在跑, 没动它";; esac
done
[ -z "$FAILED" ] || die "重启后没起来:$FAILED (journalctl -u <名字> -n 50)"
trap - ERR
# 到这里已经装完: 之后被打断只是验收没跑完
DONE=1
echo "done=1" >> "$B/deploy.marker"

# ---- 7. 验收 (只读; FAIL 不回滚, 看完再定)
echo "==> 验收"
REPORT=$LOG/accept-$TO-$TS.txt
python3 "$PKG/accept/cart_accept.py" --expect-version "$TO" --expect-build "$(cat "$PKG/opt/BUILD")" \
    | tee "$REPORT" || echo "!! 验收脚本退出码非 0, 看 $REPORT"

cat <<EOF

==================== 装完: $TO ($(cat "$OPT/BUILD")) ====================
备份      $B
回滚      $(rollback_cmd "$B")
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
