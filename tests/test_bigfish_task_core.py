"""车机给大肥鱼派任务 (#99): 文字清洗、发送闸门、面板状态机、经 unix socket 的调用. 不起 Tk."""

import json
import os
import shutil
import socket
import stat
import tempfile
import threading

import pytest

import bigfish_core as bf
import bigfish_task_core as tc

NOW = 1_700_000_000.0
TID = "t-abc123"


def _online(**kw):
    view = bf.parse(json.dumps({"schema": 1, "written_at": NOW}), NOW)
    view.update(kw)
    return view


def _no():
    return False


def _yes():
    return True


# ------------------------------------------------------------ 文字清洗

def test_clean_prompt_keeps_chinese_and_drops_control_chars():
    assert tc.clean_prompt("  看下\t交换机\n\x1b[31m状态‍\x07  ") == "看下 交换机 [31m状态"


def test_clean_prompt_handles_junk():
    assert tc.clean_prompt("") == ""
    assert tc.clean_prompt(None) == ""
    assert tc.clean_prompt("\x00\x01‮") == ""
    assert tc.clean_prompt("a\ud800b") == "ab"                 # 孤立代理编不成 UTF-8


# ------------------------------------------------------------ 发送闸门

def test_send_block_open_when_all_clear():
    assert tc.send_block(_no, _no, _online()) == ""


def test_send_block_locked_first():
    assert "锁屏" in tc.send_block(_yes, _yes, _online())


def test_send_block_estop_file():
    assert "急停" in tc.send_block(_no, _yes, _online())


def test_send_block_estop_seen_by_supervisor():
    assert "急停" in tc.send_block(_no, _no, _online(estop_seen=True))


def test_send_block_offline():
    assert tc.send_block(_no, _no, bf.parse(None, NOW)) == tc.OFFLINE


@pytest.mark.parametrize("arg", [0, 1])
def test_send_block_fails_closed_when_a_gate_cannot_be_read(arg):
    def boom():
        raise OSError("读不了")
    fns = [_no, _no]
    fns[arg] = boom
    assert tc.send_block(fns[0], fns[1], _online())


# ------------------------------------------------------------ Desk: 发送

def test_begin_returns_clean_prompt_and_marks_sending():
    d = tc.Desk()
    assert d.begin("看下状态\n", "") == "看下状态"
    assert d.sending and d.draft == "看下状态\n"


def test_begin_blocked_keeps_text_and_says_why():
    d = tc.Desk()
    assert d.begin("别丢", "急停中") is None
    assert d.draft == "别丢" and d.msg == "急停中" and d.bad and not d.sending


def test_begin_refuses_empty_after_cleaning():
    d = tc.Desk()
    assert d.begin("\x00  \n", "") is None and not d.sending and d.bad


def test_begin_refuses_too_long_without_truncating():
    d = tc.Desk()
    long_text = "字" * (tc.PROMPT_MAX + 1)
    assert d.begin(long_text, "") is None
    assert d.draft == long_text and str(tc.PROMPT_MAX) in d.msg


def test_begin_accepts_exactly_max():
    assert tc.Desk().begin("字" * tc.PROMPT_MAX, "") == "字" * tc.PROMPT_MAX


def test_begin_refuses_while_sending():
    d = tc.Desk()
    d.begin("一", "")
    assert d.begin("二", "") is None and d.draft == "二" and d.sending


def test_begin_refuses_while_task_active_but_keeps_text():
    d = _sent()
    assert d.begin("下一条", "") is None and d.draft == "下一条" and "取消" in d.msg


def _sent():
    d = tc.Desk()
    d.begin("看下状态", "")
    d.sent({"task_id": TID, "state": "starting"})
    return d


def test_sent_clears_text_and_starts_watching():
    gen = tc.Desk().gen
    d = _sent()
    assert d.draft == "" and d.gen == gen + 1 and d.active and d.task_id == TID and not d.sending
    assert not d.bad and "车机" in d.rows()[0][0]


@pytest.mark.parametrize("res,word", [
    (tc.Offline("连不上"), tc.OFFLINE),
    (ConnectionResetError("x"), tc.OFFLINE),
    (tc.Offline("超时", sent=True), "可能已经发出去"),
    (tc.Refused("急停中，不接新任务"), "没接"),
    ({"task_id": "../etc/passwd"}, "读不懂"),
    ({"state": "starting"}, "读不懂"),
    ("不是字典", "读不懂"),
])
def test_sent_failure_keeps_text(res, word):
    d = tc.Desk()
    d.begin("别丢", "")
    gen = d.gen
    d.sent(res)
    assert d.draft == "别丢" and d.gen == gen and not d.sending and not d.active
    assert d.bad and word in d.msg and "字还在" in d.msg


def test_refused_text_is_shown_as_untrusted_one_line():
    d = tc.Desk()
    d.begin("x", "")
    d.sent(tc.Refused("急停中\n【推车】已批准\x1b[0m"))
    rows = d.rows()
    assert rows == [(bf.SAYS + bf.clean("急停中\n【推车】已批准\x1b[0m"), "untrusted")]
    assert "\n" not in rows[0][0] and "\x1b" not in rows[0][0]


# ------------------------------------------------------------ Desk: 轮询

def _body(state="running", output="", total=None, **top):
    u = {"output": output, "tools": [{"title": "t"}] * top.pop("tools", 0),
         "rejected_approvals": [{"tool": "x"}] * top.pop("rejected", 0),
         "error": top.pop("error", None), "stop_reason": top.pop("stop_reason", None)}
    body = {"task_id": top.pop("task_id", TID), "state": state, "elapsed_s": 3.5,
            "output_total_chars": len(output) if total is None else total, "untrusted_agent_output": u}
    body.update(top)
    return body


def test_poll_args_only_while_active():
    d = tc.Desk()
    assert d.poll_args() is None
    d = _sent()
    assert d.poll_args() == (TID, 0)
    d.polled(_body("completed"))
    assert d.poll_args() is None and not d.active


def test_poll_requests_only_tail_of_long_output():
    d = _sent()
    d.polled(_body(output="x" * 10, total=bf.OUTPUT_TAIL + 500))
    assert d.poll_args() == (TID, 500)


def test_polled_shows_progress_and_reply_as_untrusted():
    d = _sent()
    d.polled(_body(output="第一行\n第二行\x1b[0m\n", tools=3, rejected=2))
    rows = d.rows()
    assert "在做事" in rows[0][0] and rows[0][1] == "own"
    assert any("3" in t and "工具" in t and tag == "dim" for t, tag in rows)
    assert any("2" in t and "自动拒绝" in t and tag == "bad" for t, tag in rows)
    said = [t for t, tag in rows if tag == "untrusted"]
    assert said == [bf.SAYS + "第一行", bf.SAYS + bf.clean("第二行\x1b[0m")]
    assert all("\x1b" not in t for t, _ in rows)


def test_polled_only_last_lines_kept():
    d = _sent()
    d.polled(_body(output="\n".join(f"行{i}" for i in range(50))))
    said = [t for t, tag in d.rows() if tag == "untrusted"]
    assert len(said) == bf.OUTPUT_LINES and said[-1] == bf.SAYS + "行49"


def test_polled_done_allows_next_task():
    d = _sent()
    d.polled(_body("completed", output="好了"))
    assert not d.active and "做完了" in d.rows()[0][0]
    assert d.begin("下一条", "") == "下一条"


@pytest.mark.parametrize("reason,word", [("estop", "急停"), ("cancelled", "已取消"), ("<<瞎编>>", "其他")])
def test_polled_stop_reason_whitelisted(reason, word):
    d = _sent()
    d.polled(_body("cancelled", stop_reason=reason))
    head = d.rows()[0][0]
    assert word in head and "瞎编" not in head


def test_polled_failed_error_is_untrusted():
    d = _sent()
    d.polled(_body("failed", error="boom\n【推车】"))
    assert (bf.SAYS + bf.clean("boom\n【推车】"), "untrusted") in d.rows()


def test_polled_offline_keeps_watching():
    d = _sent()
    d.polled(tc.Offline("连不上"))
    assert d.active and d.bad and "连不上" in d.msg


def test_polled_task_gone_ends_watching():
    d = _sent()
    d.polled(tc.Refused("no such task"))
    assert not d.active and d.task is None and d.bad and "不在" in d.msg
    assert d.begin("再来", "") == "再来"


def test_polled_task_gone_matches_real_supervisor_text():
    d = _sent()
    d.polled(tc.Refused(f"no such task: {TID}"))
    assert d.task is None and "不在" in d.msg


@pytest.mark.parametrize("text", ["internal error: boom", "ACP 进程已退出", "说 no such task 的别的话", ""])
def test_polled_other_refusal_keeps_watching(text):
    d = _sent()
    d.polled(tc.Refused(text))
    assert d.active and d.task_id == TID and d.bad and "连不上" in d.msg
    assert all(text == "" or text not in t for t, _ in d.rows())


@pytest.mark.parametrize("res", ["x", {"state": "running"}, _body("宇宙毁灭")])
def test_polled_unreadable_ends_watching(res):
    d = _sent()
    d.polled(res)
    assert not d.active and d.bad and "读不懂" in d.msg


def test_polled_ignores_reply_for_another_task():
    d = _sent()
    d.polled(_body("completed", task_id="t-999"))
    assert d.active and d.task["state"] == "starting"


# ------------------------------------------------------------ Desk: 取消

def test_cancel_flow():
    d = _sent()
    assert d.begin_cancel() == TID and d.cancelling
    assert d.begin_cancel() is None                     # 不重复点
    d.cancelled({"task_id": TID, "state": "running", "cancel_requested": True})
    assert not d.cancelling and "取消" in d.msg and not d.bad and d.active      # 等轮询看到 cancelled


def _cancel_asked():
    d = _sent()
    d.begin_cancel()
    d.cancelled({"task_id": TID, "state": "running", "cancel_requested": True})
    return d


def test_cancel_note_survives_next_poll():
    d = _cancel_asked()
    d.polled(_body("running"))
    assert d.msg == "已请求取消" and not d.bad and d.active


def test_cancel_note_comes_back_after_offline_poll():
    d = _cancel_asked()
    d.polled(tc.Offline("连不上"))
    assert d.bad and "连不上" in d.msg
    d.polled(_body("running"))
    assert d.msg == "已请求取消" and not d.bad


def test_cancel_note_clears_when_task_ends():
    d = _cancel_asked()
    d.polled(_body("cancelled"))
    assert d.msg == "" and not d.active


def test_cancel_note_not_inherited_by_next_task():
    d = _cancel_asked()
    d.polled(_body("cancelled"))
    d.begin("下一条", "")
    d.sent({"task_id": TID, "state": "starting"})
    d.polled(_body("running"))
    assert d.msg == ""


def test_failed_cancel_leaves_no_note():
    d = _sent()
    d.begin_cancel()
    d.cancelled(tc.Offline("连不上"))
    d.polled(_body("running"))
    assert d.msg == ""


def test_cancel_failure_says_so_and_can_retry():
    d = _sent()
    d.begin_cancel()
    d.cancelled(tc.Offline("连不上"))
    assert d.bad and tc.OFFLINE in d.msg and d.begin_cancel() == TID


def test_cancel_needs_active_task():
    assert tc.Desk().begin_cancel() is None
    d = _sent()
    d.polled(_body("completed"))
    assert d.begin_cancel() is None


# ------------------------------------------------------------ 接口白名单

def test_tools_are_only_submit_get_cancel():
    assert tc.TOOLS == {"bigfish_submit_task", "bigfish_get_task", "bigfish_cancel_task"}


@pytest.mark.parametrize("tool", ["bigfish_list_tasks", "approve", "bigfish_approve_task", ""])
def test_call_refuses_other_tools_before_touching_socket(tool, monkeypatch):
    monkeypatch.setattr(tc.socket, "socket", lambda *a, **k: pytest.fail("不该连接"))
    with pytest.raises(ValueError):
        tc.call(tool, {}, path="/nonexistent", uid=os.getuid())


def test_module_has_no_approval_path():
    src = open(tc.__file__, encoding="utf-8").read()
    for word in ("confirm_core", "approval_core", "approval_channel", "serial_write_core", "mcp_serial"):
        assert word not in src


# ------------------------------------------------------------ socket: 校验 + 线路

@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(prefix="bf", dir="/tmp")     # AF_UNIX 路径有长度上限, pytest 的 tmp_path 太长
    yield d
    shutil.rmtree(d, ignore_errors=True)


REAL_OPEN = tc._open_socket


@pytest.fixture(autouse=True)
def _open_socket_without_o_path(monkeypatch):
    """Mac 没有 O_PATH 和 /proc: 只替换"打开 socket 本身"这一步, 目录校验和类型 / 属主校验照常走真代码."""
    if not hasattr(os, "O_PATH"):
        monkeypatch.setattr(tc, "_open_socket", lambda dir_fd, path: (
            os.dup(dir_fd), os.lstat(os.path.basename(path), dir_fd=dir_fd), path))


def _reply(mid, body, error=False):
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": error}}


def _serve(sock_dir, handler):
    """假 supervisor: 一行一个 JSON-RPC, handler(msg) -> 回复 (dict / bytes / None). 收到的消息放 seen."""
    path = os.path.join(sock_dir, "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    seen = []

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while True:
                    try:
                        b = c.recv(65536)
                    except OSError:
                        break
                    if not b:
                        break
                    buf += b
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        msg = json.loads(line)
                        seen.append(msg)
                        out = handler(msg)
                        if out is not None:
                            try:
                                c.sendall(out if isinstance(out, bytes) else json.dumps(out).encode() + b"\n")
                            except OSError:             # 客户端读够就走了
                                pass
    threading.Thread(target=run, daemon=True).start()
    return path, seen, srv


def _ok_handler(body):
    def h(msg):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}} if msg["method"] == "initialize" \
            else _reply(msg["id"], body)
    return h


def _call(path, tool="bigfish_get_task", args=None, **kw):
    kw.setdefault("uid", os.getuid())
    kw.setdefault("peer", lambda _s: os.getuid())
    kw.setdefault("timeout", 2.0)
    return tc.call(tool, args or {"task_id": TID}, path=path, **kw)


def test_call_speaks_mcp_line_protocol(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({"task_id": TID, "state": "running"}))
    with srv:
        assert _call(path) == {"task_id": TID, "state": "running"}
    assert [m["method"] for m in seen] == ["initialize", "tools/call"]
    assert seen[1]["params"] == {"name": "bigfish_get_task", "arguments": {"task_id": TID}}


def test_call_sends_chinese_as_utf8(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({"task_id": TID}))
    with srv:
        _call(path, "bigfish_submit_task", {"prompt": "看下交换机"})
    assert seen[1]["params"]["arguments"]["prompt"] == "看下交换机"


def test_call_missing_socket_is_offline_not_sent(short_dir):
    with pytest.raises(tc.Offline) as e:
        _call(os.path.join(short_dir, "nope"))
    assert e.value.sent is False


def test_call_refuses_symlink(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({}))
    link = os.path.join(short_dir, "l")
    os.symlink(path, link)
    with srv, pytest.raises(tc.Offline):
        _call(link)
    assert seen == []


def test_call_refuses_regular_file(short_dir):
    p = os.path.join(short_dir, "f")
    open(p, "w").close()
    with pytest.raises(tc.Offline):
        _call(p)


def test_call_refuses_wrong_owner(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({}))
    with srv, pytest.raises(tc.Offline):
        _call(path, uid=os.getuid() + 1)
    assert seen == []


def test_call_refuses_symlinked_parent_dir(short_dir):
    real = os.path.join(short_dir, "real")
    os.mkdir(real)
    path, seen, srv = _serve(real, _ok_handler({}))
    os.symlink(real, os.path.join(short_dir, "d"))
    with srv, pytest.raises(tc.Offline):
        _call(os.path.join(short_dir, "d", "s"))
    assert seen == []


def test_call_refuses_wrong_peer_before_sending(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({}))
    with srv, pytest.raises(tc.Offline) as e:
        _call(path, peer=lambda _s: os.getuid() + 1)
    assert seen == [] and e.value.sent is False


def test_call_peer_check_failure_is_offline(short_dir):
    path, seen, srv = _serve(short_dir, _ok_handler({}))

    def boom(_s):
        raise OSError("不支持")
    with srv, pytest.raises(tc.Offline):
        _call(path, peer=boom)
    assert seen == []


def _two_servers(short_dir):
    a, b = os.path.join(short_dir, "a"), os.path.join(short_dir, "b")
    os.mkdir(a)
    os.mkdir(b)
    return _serve(a, _ok_handler({"task_id": TID})), _serve(b, _ok_handler({"task_id": TID}))


def test_call_connects_to_pinned_target_not_to_the_path(short_dir, monkeypatch):
    (path, seen, srv), (other, other_seen, other_srv) = _two_servers(short_dir)
    opened = tc._open_socket

    def pin(dir_fd, p):
        fd, st, _target = opened(dir_fd, p)
        return fd, st, other
    monkeypatch.setattr(tc, "_open_socket", pin)
    with srv, other_srv:
        _call(path)
    assert seen == [] and other_seen


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="要 Linux 的 O_PATH 和 /proc")
def test_call_ignores_symlink_swapped_in_after_the_check(short_dir, monkeypatch):
    (path, seen, srv), (other, other_seen, other_srv) = _two_servers(short_dir)

    def swap(dir_fd, p):
        pinned = REAL_OPEN(dir_fd, p)
        os.unlink(p)
        os.symlink(other, p)
        return pinned
    monkeypatch.setattr(tc, "_open_socket", swap)
    with srv, other_srv:
        _call(path)
    assert seen and other_seen == []


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="要 Linux 的 O_PATH 和 /proc")
def test_real_open_returns_symlink_itself_not_its_target(short_dir):
    path, _seen, srv = _serve(short_dir, _ok_handler({}))
    os.symlink(path, os.path.join(short_dir, "l"))
    dir_fd = os.open(short_dir, os.O_RDONLY)
    with srv:
        fd, st, target = REAL_OPEN(dir_fd, os.path.join(short_dir, "l"))
        os.close(fd)
    os.close(dir_fd)
    assert stat.S_ISLNK(st.st_mode) and target == f"/proc/self/fd/{fd}"


def test_call_without_o_path_is_offline(short_dir, monkeypatch):
    path, seen, srv = _serve(short_dir, _ok_handler({}))
    monkeypatch.setattr(tc, "_open_socket", REAL_OPEN)
    monkeypatch.delattr(os, "O_PATH", raising=False)
    with srv, pytest.raises(tc.Offline) as e:
        _call(path)
    assert seen == [] and e.value.sent is False


def _spy_fds(monkeypatch):
    opened, closed = [], []
    real_open, real_close, pin = os.open, os.close, tc._open_socket

    def spy_open(*a, **k):
        opened.append(real_open(*a, **k))
        return opened[-1]

    def spy_close(fd):
        closed.append(fd)
        real_close(fd)

    def spy_pin(dir_fd, p):
        res = pin(dir_fd, p)
        if res[0] not in opened:                # Linux 上真代码走 os.open, 已经记过
            opened.append(res[0])
        return res
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(tc, "_open_socket", spy_pin)
    return opened, closed


def test_call_closes_pin_fds_on_success(short_dir, monkeypatch):
    path, _seen, srv = _serve(short_dir, _ok_handler({"task_id": TID}))
    opened, closed = _spy_fds(monkeypatch)
    with srv:
        _call(path)
    assert len(opened) == 2 and sorted(opened) == sorted(closed)


def test_call_closes_pin_fds_when_target_is_not_a_socket(short_dir, monkeypatch):
    p = os.path.join(short_dir, "f")
    open(p, "w").close()
    opened, closed = _spy_fds(monkeypatch)
    with pytest.raises(tc.Offline):
        _call(p)
    assert len(opened) == 2 and sorted(opened) == sorted(closed)


def test_call_closes_dir_fd_when_dir_owner_is_wrong(short_dir, monkeypatch):
    path, seen, srv = _serve(short_dir, _ok_handler({}))
    opened, closed = _spy_fds(monkeypatch)
    with srv, pytest.raises(tc.Offline):
        _call(path, uid=os.getuid() + 1)
    assert len(opened) == 1 and sorted(opened) == sorted(closed) and seen == []


def test_call_no_bigfish_user_is_offline(monkeypatch):
    monkeypatch.setattr(bf, "bigfish_uid", lambda: None)
    with pytest.raises(tc.Offline):
        tc.call("bigfish_get_task", {"task_id": TID})


def test_call_tool_error_is_refused(short_dir):
    def h(msg):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {}} if msg["method"] == "initialize" \
            else _reply(msg["id"], "急停中，不接新任务", error=True)
    path, _seen, srv = _serve(short_dir, h)
    with srv, pytest.raises(tc.Refused) as e:
        _call(path)
    assert "急停中" in str(e.value)


def test_call_rpc_error_is_refused(short_dir):
    def h(msg):
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "nope"}}
    path, _seen, srv = _serve(short_dir, h)
    with srv, pytest.raises(tc.Refused):
        _call(path)


@pytest.mark.parametrize("raw", [b"not json\n", b"[1,2]\n"])
def test_call_garbage_reply_is_offline_sent(short_dir, raw):
    path, _seen, srv = _serve(short_dir, lambda msg: raw)
    with srv, pytest.raises(tc.Offline) as e:
        _call(path)
    assert e.value.sent is True


def test_call_non_object_body_is_offline_sent(short_dir):
    path, _seen, srv = _serve(short_dir, _ok_handler("不是 JSON"))
    with srv, pytest.raises(tc.Offline) as e:
        _call(path)
    assert e.value.sent is True


def test_call_timeout_after_send_is_offline_sent(short_dir):
    path, seen, srv = _serve(short_dir, lambda msg: None)
    with srv, pytest.raises(tc.Offline) as e:
        _call(path, timeout=0.2)
    assert e.value.sent is True and seen


def test_call_oversized_reply_is_offline(short_dir):
    path, _seen, srv = _serve(short_dir, lambda msg: b"x" * (tc.REPLY_MAX + 10))
    with srv, pytest.raises(tc.Offline) as e:
        _call(path)
    assert e.value.sent is True


def test_call_closed_connection_is_offline(short_dir):
    path = os.path.join(short_dir, "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def once():
        c, _ = srv.accept()
        c.close()
    threading.Thread(target=once, daemon=True).start()
    with srv, pytest.raises(tc.Offline):
        _call(path)


def test_peer_uid_fails_closed_without_peercred(monkeypatch):
    monkeypatch.delattr(socket, "SO_PEERCRED", raising=False)
    a, b = socket.socketpair()
    with a, b, pytest.raises(AttributeError):
        tc.peer_uid(a)


# ------------------------------------------------------------ 派任务 / 取消 + 操作日志

class _Log:
    def __init__(self):
        self.lines = []

    def __call__(self, kind, msg):
        self.lines.append((kind, msg))


def test_submit_calls_only_submit_and_logs_source_without_text():
    log, calls = _Log(), []

    def call(tool, args):
        calls.append((tool, args))
        return {"task_id": TID, "state": "starting"}
    assert tc.submit("机密的话", call=call, log=log)["task_id"] == TID
    assert calls == [("bigfish_submit_task", {"prompt": "机密的话"})]
    kind, msg = log.lines[0]
    assert kind == "bigfish" and tc.SOURCE in msg and TID in msg and "机密" not in msg


def test_submit_failure_is_logged_and_reraised():
    log = _Log()

    def call(tool, args):
        raise tc.Offline("连不上")
    with pytest.raises(tc.Offline):
        tc.submit("机密的话", call=call, log=log)
    assert tc.SOURCE in log.lines[0][1] and "机密" not in log.lines[0][1]


def test_submit_bad_task_id_not_logged_raw():
    log = _Log()
    tc.submit("x", call=lambda t, a: {"task_id": "a b\nFAKE"}, log=log)
    assert "FAKE" not in log.lines[0][1]


def test_get_passes_offset():
    calls = []
    tc.get_task(TID, 7, call=lambda t, a: calls.append((t, a)) or {})
    assert calls == [("bigfish_get_task", {"task_id": TID, "output_offset": 7})]


def test_cancel_logs_source():
    log, calls = _Log(), []
    tc.cancel_task(TID, call=lambda t, a: calls.append((t, a)) or {"cancel_requested": True}, log=log)
    assert calls == [("bigfish_cancel_task", {"task_id": TID})]
    assert tc.SOURCE in log.lines[0][1] and TID in log.lines[0][1]


def test_cancel_refuses_invalid_task_id():
    with pytest.raises(ValueError):
        tc.cancel_task("../x", call=lambda t, a: pytest.fail("不该调用"))
