#!/bin/bash
# 推车上一条命令装 1.16.01. 用 systemd-run 跑, 不依赖 ssh 会话 (断线 / 关终端照样跑完); 脚本没有交互:
#   sudo systemd-run --unit=ops-cart-deploy --collect bash /tmp/deploy-1.16.01/install.sh
#   journalctl -fu ops-cart-deploy          # 看进度
# 包的布局见 docs/DEPLOY_1.16.01.md 第 3 步. 只从 1.16.00 升; 不是就停 (跳过版本检查: sudo systemd-run --setenv=FORCE=1 --unit=ops-cart-deploy --collect bash .../install.sh).
# 照 install-1.16.00.sh 来 (备份 + rollback.sh + deploy.marker + 包核对 + trap), 这一版多 #95 迁移:
# pxeserver.log 挪进日志目录、logrotate 配置; smb 推车上没在用, 直接 disable --now (维护者定).
# 不碰 /etc/ops-cart, 不碰大肥鱼的目录; 不改 smb.conf、不装 smb drop-in / NM 钩子、不跑 setup-samba.sh
# (以后要用 smb 照 docs/MIGRATE_95.md 第 2 节恢复).
set -Eeuo pipefail

PKG=$(cd "$(dirname "$0")" && pwd)
# 路径可用环境变量换 (只给 tests/test_install_1601.py 用)
OPT=${OPT:-/opt/pxe}
SYS=${SYS:-/etc/systemd/system}
ETC=${ETC:-/etc/ops-cart}
LOG=${LOG:-/var/log/ops-cart}
LRD=${LRD:-/etc/logrotate.d}
FROM=1.16.00
EXPECT_TO=1.16.01
TO=$(cat "$PKG/opt/VERSION" 2>/dev/null || true)
CHANGED_UNITS="pxe-server.service"
# 改过代码 (或 import 了改过的模块)、要重启的常驻服务, 部署前开着的才重启; kiosk 会回到锁屏.
# pxe-server 只换单元 (日志路径), 在第 3 段停 / 起; kvm-broker、pxe-http-* 这版没改, 不动.
RESTART="cart-dash kiosk webkiosk cart-mcp cart-mcp-ro"
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
for f in $(for u in $CHANGED_UNITS; do echo "systemd/$u"; done) etc/logrotate.d/ops-cart \
         accept/cart_accept.py accept/accept_core.py accept/accept-manifest.json \
         opt/kiosk.py opt/bigfish_core.py opt/observe_core.py; do
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
# 包里 etc/ 下的 (logrotate 配置) 照推车路径 /etc/<相对路径> 查 manifest; 必须列着
etc_files = []
for root, _, names in os.walk(os.path.join(pkg, "etc")):
    for n in names:
        etc_files.append("/etc/" + os.path.relpath(os.path.join(root, n), os.path.join(pkg, "etc")).replace(os.sep, "/"))
for d in etc_files:
    if d not in files:
        bad.append("manifest 没列 " + d)
for dest, want in sorted(files.items()):
    if dest.startswith("/opt/pxe/"):
        src = os.path.join(pkg, "opt", dest[len("/opt/pxe/"):])
    elif dest.startswith("/etc/systemd/system/") and os.path.basename(dest) in units:
        src = os.path.join(pkg, "systemd", os.path.basename(dest))
    elif dest in etc_files:
        src = os.path.join(pkg, dest[1:])
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
if [ "$CUR" != "$FROM" ] && [ "${FORCE:-}" != 1 ]; then
    die "推车现在是 $CUR, 本包只从 $FROM 升 (确认无误再 $FORCE_CMD)"
fi
# 有机器正解锁着 = 有人在装机: 这版要停 / 起 pxe-server, 会把它断掉. 先等装完或在车机上上锁
if ls "$OPT"/tftp/grub.cfg-* >/dev/null 2>&1; then
    die "tftp 里有按 MAC 解锁的菜单 ($(ls "$OPT"/tftp/grub.cfg-* | xargs -n1 basename | tr '\n' ' ')), 有机器在装, 先等装完 / 上锁再装"
fi
echo "==> $CUR ($(cat "$OPT/BUILD" 2>/dev/null)) -> $TO ($(cat "$PKG/opt/BUILD"))"

# 现在开着的服务: 装完重启它们, 回滚时也无条件重启它们 (新代码起不来停掉的也要拉回来)
ACTIVE=""
for s in $RESTART; do if systemctl is-active --quiet "$s"; then ACTIVE="$ACTIVE $s"; fi; done
PXE_WAS=""
if systemctl is-active --quiet pxe-server; then PXE_WAS=1; fi
# smb 部署前的样子: 回滚照原样恢复 (enabled 才 enable, active 才 start). 没装 Samba 就整段不管
SMB=""; SMB_EN=""; SMB_ACT=""
if [ "$(systemctl show -p LoadState --value smb.service 2>/dev/null || true)" = loaded ]; then
    SMB=1
    if systemctl is-enabled --quiet smb.service; then SMB_EN=1; fi
    if systemctl is-active --quiet smb.service; then SMB_ACT=1; fi
fi

# ---- 1. 备份
TS=$(date +%Y%m%d-%H%M%S)
B=$OPT/.bak-$TS
mkdir -p "$B/etc/systemd" "$B/etc/logrotate.d"
cp -a "$OPT"/*.py "$OPT/VERSION" "$OPT/BUILD" "$OPT/web" "$OPT/icons" "$B/"
for u in $CHANGED_UNITS; do cp -a "$SYS/$u" "$B/etc/systemd/"; done
if [ -f "$LRD/ops-cart" ]; then cp -a "$LRD/ops-cart" "$B/etc/logrotate.d/"; fi
cp -a "$ETC" "$B/etc/ops-cart"
PXE_RESTART=""
if [ -n "$PXE_WAS" ]; then PXE_RESTART=pxe-server; fi
cat > "$B/rollback.sh" <<EOF
#!/bin/bash
# 回滚到 $CUR (备份 $TS). 新加的 .py 留着无害, 旧代码不 import.
# 保留新的 logrotate 配置 (旧的带 /opt/pxe/pxeserver.log, 装回去 logrotate 又会天天失败, 见 docs/MIGRATE_95.md);
# 挪进 $LOG 的 pxeserver.log 也留着, 旧单元重新写 $OPT/pxeserver.log.
set -Eeu
trap 'echo "!! 回滚第 \$LINENO 行失败, 修好后可直接重跑" >&2' ERR
B=$B
cp -a \$B/*.py \$B/VERSION \$B/BUILD $OPT/
rm -rf $OPT/web $OPT/icons && cp -a \$B/web \$B/icons $OPT/
for u in $CHANGED_UNITS; do install -m 644 \$B/etc/systemd/\$u $SYS/\$u; done
systemctl daemon-reload
BAD=""
# smb 照部署前的样子 (部署前: 装了=${SMB:-0} 自启=${SMB_EN:-0} 在跑=${SMB_ACT:-0}); 部署前没在跑 (含 failed) 的不拉起
if [ "${SMB_EN:-0}" = 1 ]; then systemctl enable smb.service || BAD="\$BAD smb(enable)"; fi
if [ "${SMB_ACT:-0}" = 1 ]; then systemctl start smb.service || BAD="\$BAD smb(start)"; fi
# 部署前开着的这几个: 不看现在状态, 直接重启
for s in $ACTIVE $PXE_RESTART; do systemctl restart \$s || BAD="\$BAD \$s"; done
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

# ---- 3. pxeserver.log 挪进 $LOG (#95). 先停: 开着文件时挪走, 进程会接着写旧位置
echo "==> pxeserver.log -> $LOG"
systemctl stop pxe-server
for u in $CHANGED_UNITS; do install -m 644 "$PKG/systemd/$u" "$SYS/$u"; done
systemctl daemon-reload
if [ -f "$OPT/pxeserver.log" ]; then mv -n "$OPT/pxeserver.log" "$LOG/pxeserver.log"; fi
if [ -f "$OPT/pxeserver.log" ]; then echo "    警告: $LOG/pxeserver.log 已经有了, $OPT/pxeserver.log 没挪 (看一眼留哪份)"; fi
# 同一个文件系统里 mv 带着 usr_t 过去, 要 restorecon 成 var_log_t
if [ -f "$LOG/pxeserver.log" ]; then restorecon -v "$LOG/pxeserver.log"; fi
if [ -n "$PXE_WAS" ]; then systemctl start pxe-server; echo "    起 pxe-server"; fi

# ---- 4. logrotate 配置: 去掉 /opt/pxe 那行
install -m 644 "$PKG/etc/logrotate.d/ops-cart" "$LRD/ops-cart"
restorecon "$LRD/ops-cart"

# ---- 5. smb: 推车上没在用, 停掉并关自启 (维护者定); smb.conf 原样不动. 以后要用照 docs/MIGRATE_95.md 第 2 节
if [ -n "$SMB" ]; then
    echo "==> smb: disable --now (没在用)"
    systemctl disable --now smb.service
    systemctl reset-failed smb.service || true
fi

# ---- 6. 重启 (开着的才重启)
for s in $ACTIVE; do systemctl restart "$s"; echo "    重启 $s"; done
sleep "${SETTLE:-3}"
# 只有部署前开着的才算失败; 部署前就没在跑的只提一句
FAILED=""
for s in $ACTIVE $PXE_RESTART; do if ! systemctl is-active --quiet "$s"; then FAILED="$FAILED $s"; fi; done
for s in $RESTART pxe-server; do
    case " $ACTIVE $PXE_RESTART " in *" $s "*) ;; *) echo "    $s 部署前就没在跑, 没动它";; esac
done
[ -z "$FAILED" ] || die "重启后没起来:$FAILED (journalctl -u <名字> -n 50)"
trap - ERR
# 到这里已经装完: 之后被打断只是验收没跑完
DONE=1
echo "done=1" >> "$B/deploy.marker"

# ---- 7. 收尾检查 (只报告, 不回滚): smb 不该 failed; logrotate 跑一次, 清掉上次的 failed
SMB_STATE=""
if [ -n "$SMB" ]; then
    if systemctl is-failed --quiet smb.service || systemctl is-enabled --quiet smb.service; then
        SMB_STATE="!! smb.service 没停干净: systemctl status smb; systemctl is-enabled smb"
    else
        SMB_STATE="smb.service 已停用 (disabled, inactive); 要用照 docs/MIGRATE_95.md 第 2 节"
    fi
    echo "    $SMB_STATE"
fi
if systemctl start logrotate.service && ! systemctl is-failed --quiet logrotate.service; then
    LR_STATE="logrotate 跑过一次, 没失败"
else
    LR_STATE="!! logrotate 还是失败: journalctl -u logrotate -n 20"
fi
echo "    $LR_STATE"

# ---- 8. 验收 (只读; FAIL 不回滚, 看完再定)
echo "==> 验收"
REPORT=$LOG/accept-$TO-$TS.txt
python3 "$PKG/accept/cart_accept.py" --expect-version "$TO" --expect-build "$(cat "$PKG/opt/BUILD")" \
    | tee "$REPORT" || echo "!! 验收脚本退出码非 0, 看 $REPORT"

cat <<EOF

==================== 装完: $TO ($(cat "$OPT/BUILD")) ====================
备份      $B
回滚      $(rollback_cmd "$B")
验收报告  $REPORT
smb       ${SMB_STATE:-没装 Samba, 没动}
logrotate $LR_STATE

要在车边看的 (详细见 docs/DEPLOY_1.16.01.md 第 8 步):
  [ ] 车机回到锁屏, 右上角有"大肥鱼 · ..."一行; 副屏锁屏页也有这一行
  [ ] 主菜单"大肥鱼"进面板: 状态正常显示, 大肥鱼的话带"大肥鱼说:"前缀; 没有"急停通道未就绪"
  [ ] 串口面板"观察": 人登录后开始观察, 前面板和端口灯对得上真机; 退出观察不往 console 发东西 (#85)
  [ ] systemctl is-enabled smb 是 disabled; systemctl --failed 里没有 smb
  [ ] 车机真屏跑确认框 + 大肥鱼面板 Tk 测试 (命令见第 8 步, skip 不算过)
EOF
