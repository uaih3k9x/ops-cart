"""ime_rime 测试: ctypes 结构布局对得上 rime_api.h, 缺库 / 缺词库时清楚降级. 真 librime 的集成测试要显式给环境变量."""

import ctypes
import os

import pytest

import ime_core as ic
import ime_rime as ir

P = ctypes.sizeof(ctypes.c_void_p)


# ------------------------------------------------------------ 结构布局 (照 rime_api.h 逐字段数)

def test_api_table_is_int_then_pointers_in_header_order():
    names = [n for n, _t in ir.RimeApi._fields_]
    assert names[:3] == ["data_size", "setup", "set_notification_handler"]
    for i, n in enumerate(names[1:]):
        assert getattr(ir.RimeApi, n).offset == P * (i + 1), n      # int 后按指针对齐, 每项一个指针
    # 用到的几项在头文件里的位置 (0 起, 不算 data_size)
    want = {"setup": 0, "initialize": 2, "finalize": 3, "start_maintenance": 4, "join_maintenance_thread": 6,
            "create_session": 13, "destroy_session": 15, "process_key": 18, "clear_composition": 20,
            "get_commit": 21, "free_commit": 22, "get_context": 23, "free_context": 24, "set_option": 27,
            "select_schema": 34, "get_input": 69, "get_version": 72, "select_candidate_on_current_page": 74}
    for n, i in want.items():
        assert names.index(n) - 1 == i, n


def test_struct_sizes_match_header_on_64bit():
    if P != 8:
        pytest.skip("只核 64 位布局")
    assert ctypes.sizeof(ir.RimeComposition) == 24
    assert ctypes.sizeof(ir.RimeCandidate) == 24
    assert ctypes.sizeof(ir.RimeMenu) == 40
    assert ir.RimeContext.composition.offset == 8 and ir.RimeContext.menu.offset == 32
    assert ctypes.sizeof(ir.RimeContext) == 88
    assert ctypes.sizeof(ir.RimeCommit) == 16
    assert ctypes.sizeof(ir.RimeTraits) == 8 + 11 * 8


def test_init_struct_sets_data_size():
    t = ir._init(ir.RimeTraits)
    assert t.data_size == ctypes.sizeof(ir.RimeTraits) - ctypes.sizeof(ctypes.c_int)


def test_fn_respects_data_size():
    """老版本 librime 的表短: data_size 盖不到的项当没有, 不去读表外的内存."""
    api = ir.RimeApi()
    api.data_size = ir.RimeApi.create_session.offset + P - ctypes.sizeof(ctypes.c_int)
    cb = ctypes.CFUNCTYPE(ctypes.c_size_t)(lambda: 42)
    api.create_session = ctypes.cast(cb, ctypes.c_void_p).value
    api.destroy_session = ctypes.cast(cb, ctypes.c_void_p).value          # 在表里有值, 但 data_size 不够
    assert ir._fn(api, "create_session", ctypes.c_size_t)() == 42
    assert ir._fn(api, "destroy_session", ctypes.c_int, ctypes.c_size_t) is None
    api.data_size = ctypes.sizeof(ir.RimeApi)
    api.get_input = None
    assert ir._fn(api, "get_input", ctypes.c_char_p, ctypes.c_size_t) is None   # 空指针也当没有


# ------------------------------------------------------------ 降级

def test_missing_library_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CART_LIBRIME", str(tmp_path / "librime-nope.so"))
    eng, why = ir.open_engine(shared_dir=str(tmp_path), user_dir=str(tmp_path / "u"))
    assert eng is None and "找不到 librime" in why and "不可用" in why


def test_find_library_none_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_CART_LIBRIME", raising=False)
    monkeypatch.setattr(ir.ctypes.util, "find_library", lambda name: None)
    monkeypatch.setattr(ir, "_LIB_NAMES", ("librime-definitely-not-here.so.1",))
    eng, why = ir.open_engine(shared_dir=str(tmp_path), user_dir=str(tmp_path / "u"))
    assert eng is None and "找不到 librime" in why


def test_missing_shared_data_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "_load_api", lambda: pytest.fail("词库都没有就不该去加载库"))
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "nope"), user_dir=str(tmp_path / "u"))
    assert eng is None and "词库" in why and str(tmp_path / "nope") in why


def test_open_engine_never_raises(tmp_path, monkeypatch):
    (tmp_path / "s").mkdir()

    def boom():
        raise RuntimeError("炸了")
    monkeypatch.setattr(ir, "_load_api", boom)
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "s"), user_dir=str(tmp_path / "u"))
    assert eng is None and "炸了" in why


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("OPS_CART_RIME_SHARED", "/opt/rime-data")
    monkeypatch.setenv("OPS_CART_RIME_USER", "/home/ops/.rime-cart")
    monkeypatch.setenv("OPS_CART_RIME_SCHEMA", "rime_ice")
    assert ir.settings() == {"shared_dir": "/opt/rime-data", "user_dir": "/home/ops/.rime-cart", "schema": "rime_ice"}
    for k in ("OPS_CART_RIME_SHARED", "OPS_CART_RIME_USER", "OPS_CART_RIME_SCHEMA"):
        monkeypatch.delenv(k)
    s = ir.settings()
    assert s["shared_dir"] == ir.SHARED_DIR and s["schema"] is None and s["user_dir"].endswith("rime")


# ------------------------------------------------------------ 真 librime (要显式给库和词库, 平时跳过)

_LIB, _DATA = os.environ.get("OPS_CART_TEST_LIBRIME"), os.environ.get("OPS_CART_TEST_RIME_DATA")


@pytest.mark.skipif(not (_LIB and _DATA), reason="要 OPS_CART_TEST_LIBRIME + OPS_CART_TEST_RIME_DATA 才跑真 librime")
def test_real_librime_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CART_LIBRIME", _LIB)
    eng, why = ir.open_engine(shared_dir=_DATA, user_dir=os.environ.get("OPS_CART_TEST_RIME_USER", str(tmp_path)),
                              schema=os.environ.get("OPS_CART_TEST_RIME_SCHEMA", "luna_pinyin"))
    assert eng is not None, why
    try:
        ime = ic.Ime()
        ime.set_engine(eng, zh=True)
        for ch in "nihao":
            assert ime.press(ch, ch, 0).consume
        v = ime.view()
        assert v["composing"] and "你好" in v["candidates"], v
        r = ime.press("space", " ", 0)
        assert r.commit == v["candidates"][v["highlighted"]] and not ime.view()["composing"]
        for ch in "ni":
            ime.press(ch, ch, 0)
        assert ime.press("Return", "\r", 0).commit == "ni"
        for ch in "hao":
            ime.press(ch, ch, 0)
        cands = ime.view()["candidates"]
        assert ime.press("2", "2", 0).commit == cands[1]
        ime.press("a", "a", 0)
        ime.press("Page_Down", "", 0)
        assert ime.view()["page_no"] == 1
        assert ime.press("Escape", "\x1b", 0).commit is None and not ime.view()["composing"]
        user = os.environ.get("OPS_CART_TEST_RIME_USER", str(tmp_path))
        eng2, why2 = ir.open_engine(shared_dir=_DATA, user_dir=user, schema="luna_pinyin")
        assert eng2 is not None, why2                       # 第二个会话 (另一个输入框) 也能开
        eng2.close()
        eng3, why3 = ir.open_engine(shared_dir=_DATA, user_dir=user, schema="no_such_schema")
        assert eng3 is None and "没部署" in why3            # 没部署的方案: 清楚报不可用, 不给一个按键全不认的引擎
    finally:
        eng.close()


def test_undeployed_schema_is_unavailable(tmp_path, monkeypatch):
    """方案没部署 (没列进 schema_list): select_schema 照样成功、按键全不认, 所以先看 build/ 里有没有."""
    (tmp_path / "s").mkdir()
    closed = []
    api = _fake_api(closed)
    monkeypatch.setattr(ir, "_ensure", lambda shared, user: api)
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "s"), user_dir=str(tmp_path / "u"), schema="rime_ice")
    assert eng is None and "rime_ice" in why and "没部署" in why and closed == [7]
    (tmp_path / "u" / "build").mkdir(parents=True)
    (tmp_path / "u" / "build" / "rime_ice.schema.yaml").write_text("x")
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "s"), user_dir=str(tmp_path / "u"), schema="rime_ice")
    assert eng is not None and why == ""



def _fake_api(closed, schema_id=b"rime_ice"):
    def get_status(sid, ref):
        ref._obj.schema_id = schema_id
        return 1
    return {"create_session": lambda: 7, "destroy_session": lambda sid: closed.append(sid),
            "select_schema": lambda sid, s: 1, "set_option": lambda *a: None,
            "get_status": get_status, "free_status": lambda ref: 1}


def test_default_schema_also_checked(tmp_path, monkeypatch):
    """没指定方案时用 get_status 看当前方案, 一样检查部署产物 (#72: 没部署就会吞键)."""
    (tmp_path / "s").mkdir()
    closed = []
    monkeypatch.setattr(ir, "_ensure", lambda shared, user: _fake_api(closed))
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "s"), user_dir=str(tmp_path / "u"))
    assert eng is None and "rime_ice" in why and closed == [7]
    monkeypatch.setattr(ir, "_ensure", lambda shared, user: _fake_api(closed, schema_id=None))
    eng, why = ir.open_engine(shared_dir=str(tmp_path / "s"), user_dir=str(tmp_path / "u"))
    assert eng is None and "方案" in why


def test_feed_returns_process_key_result():
    eng = ir.RimeEngine({"process_key": lambda sid, code, mask: 0}, 7)
    assert eng.feed("a") is False
    eng = ir.RimeEngine({"process_key": lambda sid, code, mask: 1}, 7)
    assert eng.feed("a") is True
