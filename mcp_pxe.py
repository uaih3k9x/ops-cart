#!/usr/bin/env python3
"""mcp_pxe: 经 MCP 申请 PXE 解锁 (#24). 规则全在 pxe_req_core, 这里只管 IO.

工具 (cart_mcp 注册): pxe_request (只挂起, 不解锁) / pxe_requests (只读) / pxe_retire (提前上锁, 只收紧, 直接执行).
解锁只有一条路: 车机 (root, 本机屏幕) 上人照抄确认码 -> human_confirm -> 重算清单和申请时一样 -> kiosk_core.pxe_arm.
MCP 工具拿不到 pxe_arm (build_tools 根本不收它), 也拿不到确认码和清单摘要.

状态: /run/ops-cart/pxe-req.json (root 0600; 挂起的申请含确认码), 每次操作 flock 一把锁"读 -> 改 -> 原子写";
  读不懂 = 没有任何有效申请. 重启推车 /run 清空 = 申请全没了.
急停: 不另设 PXE 急停, 复用串口的"Agent 全部急停" (车机红块、网页、Agent 的 estop 不给槽位):
  全部槽位急停时不收新申请、挂着的作废 (mcp_serial.Store 急停后调 drop_on_estop), 车机确认前再查一遍.
  串口状态读不懂 = 全部急停 = PXE 申请也停.
日志: /var/log/ops-cart/pxe-req.log (JSONL: 申请 / 确认 / 码不对 / 拒绝 / 清单变了 / 过期 / 作废 / 提前上锁),
  另在 events.log 记 [pxe-req] 一行; 确认码不进日志. 每次工具调用照常进 mcp-audit.log.
"""

import contextlib
import fcntl
import json
import os
import time

import atomicfile
import cartlog
import clone_core
import kiosk_core as kc
import mcp_core as mc
import mcp_serial
import pxe_req_core as pr

STATE_PATH = "/run/ops-cart/pxe-req.json"
LOG_PATH = os.path.join(cartlog.LOG_DIR, "pxe-req.log")


def serial_stopped(path=mcp_serial.STATE_PATH):
    """串口是不是全部急停 (PXE 申请跟它走). 没有文件 = 没急停; 读不了 / 读不懂 = 急停."""
    try:
        return all(s["estop"] for s in mcp_serial.peek(path)["slots"])
    except Exception:                   # noqa: BLE001  拿不准一律当急停
        return True


# ------------------------------------------------------------ 状态文件 + 日志

class Store:
    """run(fn): 加锁 -> 读状态 -> fn(book, now, stopped) -> 存 (fn 抛异常也存: 换码、作废要落盘) -> 写日志."""

    def __init__(self, path=STATE_PATH, log_path=LOG_PATH, clock=time.time, event=cartlog.event,
                 stopped=serial_stopped):
        self.path, self.log_path, self.clock, self.event, self.stopped = path, log_path, clock, event, stopped

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def run(self, fn):
        with self._locked():
            now = self.clock()
            book = pr.Book.from_text(_read(self.path), now)
            try:
                return fn(book, now, self.stopped())
            finally:
                atomicfile.write_text(self.path, book.dumps(), mode=0o600)
                self._log(book.events)

    def _log(self, events):
        for rec in events:
            try:
                os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:   # 每行开一次: copytruncate 后照样写对
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
            if self.event is not None:
                self.event("pxe-req", cartlog.fields(
                    ev=rec["ev"], id=rec.get("id"), who=rec.get("who"), by=rec.get("by"), purpose=rec.get("purpose"),
                    macs=",".join(rec["macs"]) if rec.get("macs") else None, why=rec.get("why")))


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return "\0"                     # 在但读不了: 按读不懂算 -> 没有申请


def peek(path=STATE_PATH, clock=time.time, stopped=serial_stopped):
    """车机只读看一眼 (带确认码, 只给 root 的车机进程): 不加锁不写盘, 过期 / 急停的在视图里算掉.
    读不了 / 读不懂 = 没有申请."""
    now = clock()
    text = _read(path)
    try:
        return pr.Book.from_text(text, now).view(now, stopped=stopped(), secret=True)
    except Exception:                   # noqa: BLE001
        return {"pending": [], "history": []}


def drop_on_estop(by):
    """mcp_serial.Store 全部急停之后调: 挂着的 PXE 申请全部作废."""
    Store().run(lambda b, now, _st: b.drop("system", now, f"急停 (by {by})"))


# ------------------------------------------------------------ 真机: 清单 / 解锁

def real_plan(purpose, mac):
    """按现有逻辑算清单: 克隆用途 kiosk_core.clone_plan (守卫、镜像在不在、目标盘), rocky / winpe 一台 (winpe 查文件齐不齐)."""
    if purpose in clone_core.CLONE_PURPOSES:
        return kc.clone_plan(purpose, mac=mac)
    return pr.simple_plan(purpose, mac, kc.winpe_missing() if purpose == "winpe" else ())


def real_arm(it, p):
    """人确认过、清单重算一致之后才走到这里. 克隆用途 pxe_arm 自己再按 digest 核一遍."""
    kc.pxe_arm(it["mac"], purpose=it["purpose"], confirm=p["digest"])


# ------------------------------------------------------------ MCP 工具 (没有一个能解锁)

MAC_PARAM = {"type": "string", "pattern": r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", "maxLength": 17}


def build_tools(store, plan=real_plan, mode=kc.pxe_mode_read, disarm=kc.pxe_disarm, retire=kc.pxe_retire,
                who=mcp_serial.current_who):
    """plan(purpose, mac) -> 清单; mode() -> pxe_mode_read 的结果; disarm() 整个上锁; retire([mac]) 删这几台的菜单.
    故意不收 pxe_arm: 解锁不在这里. 测试里全换成假的."""

    def _run(fn):
        try:
            return store.run(fn)
        except pr.Refused as e:
            raise mc.ToolError(str(e)) from None
        except OSError as e:
            raise mc.ToolError(f"写申请状态失败: {e}") from None

    def pxe_request(a):
        purpose = a["purpose"]
        try:
            mac = pr.target(purpose, a.get("mac"), a.get("batch"))
        except pr.Refused as e:
            raise mc.ToolError(str(e)) from None
        try:
            p = plan(purpose, mac)
        except (OSError, ValueError) as e:
            raise mc.ToolError(f"清单算不出: {e}") from None
        res = _run(lambda b, now, st: b.submit(who(), purpose, mac, a.get("batch"), p, now, st))
        res["hint"] = (f"已挂起, 没有解锁. 要人在推车屏幕上看清单、照抄确认码才解锁; {pr.REQ_TTL_S // 60} 分钟没人确认"
                       "就作废. 用 pxe_requests / pxe_status 看结果, 不要了就 pxe_retire.")
        return [mc.json_content(res)]

    def pxe_requests(_a):
        def go(b, now, st):
            b.tick(now, st)
            return b.view(now, st)
        return [mc.json_content(_run(go))]

    def pxe_retire(a):
        mac = None
        if a.get("mac"):
            try:
                mac = pr.target("rocky", a["mac"], False)
            except pr.Refused as e:
                raise mc.ToolError(str(e)) from None

        def go(b, now, _st):
            dropped = b.drop(who(), now, "Agent 提前上锁", mac=mac)
            act = pr.retire_action(mode(), mac)
            try:
                if act == "disarm":
                    disarm()
                elif act == "retire":
                    retire([mac])
            except OSError as e:
                b.note("retire", now, who=who(), why=f"上锁失败: {e}")
                raise mc.ToolError(f"上锁失败: {e}") from None
            b.note("retire", now, who=who(), macs=[mac] if mac else None, why=act or "那台没解锁")
            return {"dropped": dropped, "action": act, "mac": mac}
        return [mc.json_content(_run(go))]

    return [
        mcp_serial.Tool(
            "pxe_request", "申请 PXE 解锁 (只挂起, 不解锁): purpose = rocky (自动装 Rocky, 清空整块硬盘) / winpe "
            "(进 WinPE) / clone-save (整盘抓镜像到推车, 给 mac) / clone-restore (按推车上的克隆清单整批覆盖, "
            "给 batch=true). 推车按现有规则算出清单, 车机屏幕弹出申请, 人看过清单、照抄屏幕上的确认码才解锁; "
            "确认时推车重算清单, 和申请时不一样就拒. 申请 10 分钟没人确认作废; Agent 全部急停时不收.",
            {"purpose": {"type": "string", "enum": list(pr.PURPOSES), "required": True, "description": "用途"},
             "mac": dict(MAC_PARAM, description="目标机器 MAC (rocky / winpe / clone-save)"),
             "batch": {"type": "boolean", "description": "clone-restore 时给 true: 按推车上的克隆清单"}},
            pxe_request),
        mcp_serial.Tool(
            "pxe_requests", "看 PXE 解锁申请: 在等人确认的 (剩几秒) 和最近的结果 (armed 已解锁 / rejected / "
            "refused 清单变了等 / expired / dropped 急停或撤销). 只读. 解锁之后的状态用 pxe_status.",
            {}, pxe_requests, read_only=True),
        mcp_serial.Tool(
            "pxe_retire", "提前上锁 (只收紧, 直接执行): 不给 mac = 整个装机口上锁、撤掉所有在等的申请; "
            "给 mac = 撤掉涉及它的申请, 它正解锁着就删它的菜单 (只剩它一台就整个上锁).",
            {"mac": dict(MAC_PARAM, description="只收这一台; 不给 = 全部")}, pxe_retire),
    ]


def real_tools():
    return build_tools(Store())


# ------------------------------------------------------------ 车机上的人 (kiosk 后台线程调)

def human_confirm(store, iid, typed, shown_digest, by, plan=real_plan, arm=real_arm, mode=kc.pxe_mode_read):
    """人照抄确认码 -> 一句结果. 码不对 / 界面清单旧了 / 装机口正解锁着: 抛 Refused, 申请还挂着;
    重算清单和申请时不一样 / 解锁出错: 记下结果抛 Refused, 申请作废. 调用方保证是车机本机人."""
    def go(b, now, stopped):
        it = b.take(iid, typed, shown_digest, now, stopped)
        m = mode()
        if m.get("mode") == "install":
            raise pr.Refused("busy", f"装机口正解锁着 ({m.get('mac')}), 先上锁或等它到期再确认")
        try:
            p = plan(it["purpose"], it["mac"])
        except (OSError, ValueError) as e:
            p = {"error": str(e)}
        if p.get("error") or p.get("digest") != it["digest"]:
            why = f"清单和申请时不一样 ({p['error']})" if p.get("error") else "清单和申请时不一样 (清单或配置被改过?)"
            b.settle(iid, "refused", by, now, why=why)
            raise pr.Refused("changed", why + ", 已作废, 要 Agent 重新申请")
        try:
            arm(it, p)
        except (ValueError, OSError) as e:
            b.settle(iid, "failed", by, now, why=str(e))
            raise pr.Refused("failed", f"解锁失败: {e}") from None
        b.settle(iid, "armed", by, now)
        return f"#{iid} 已解锁 {pr.WHAT[it['purpose']]}: {', '.join(it['macs'])}"
    return store.run(go)


def human_reject(store, iid, by):
    store.run(lambda b, now, _st: b.reject(iid, by, now, why="车机上拒绝"))
    return f"已拒绝 #{iid}"
