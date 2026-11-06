#!/usr/bin/env python3
"""librime 的 ctypes 薄外壳 (#54): 给 ime_core 当引擎. 没有 librime / 词库时 open_engine 返回 (None, 原因), 不抛.

接口照 librime 的 rime_api.h (BSD-3, github.com/rime/librime): 只用 rime_get_api() 拿函数表, 按头文件里的
顺序声明; 表头 data_size 盖不到的项 (老版本没有) 当不存在. 结构体都以 int data_size 开头, 用前先 _init.

配置 (环境变量, 都可不设):
  OPS_CART_LIBRIME       librime 动态库路径 (默认按 librime.so.1 / ctypes.util.find_library 找)
  OPS_CART_RIME_SHARED   共享数据 (方案 + 词库) 目录, 默认 /usr/share/rime-data
  OPS_CART_RIME_USER     用户目录 (部署产物、用户词频), 默认 ~/.local/share/ops-cart/rime
  OPS_CART_RIME_SCHEMA   方案 id, 如 luna_pinyin_simp / rime_ice; 不设用 default.yaml 的第一个
librime 进程内只能初始化一次: 第一次 open_engine 定下目录, 之后每个输入框各开一个会话.
第一次部署 (编译词库) 可能要几秒到几分钟, 调用方放后台线程跑.
"""

import ctypes
import ctypes.util
import os
import threading

import ime_core

SHARED_DIR = "/usr/share/rime-data"
USER_DIR = "~/.local/share/ops-cart/rime"
_LIB_NAMES = ("librime.so.1", "librime.1.dylib")

_c_int, _vp, _cp, _sid = ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t


class RimeTraits(ctypes.Structure):
    _fields_ = [("data_size", _c_int), ("shared_data_dir", _cp), ("user_data_dir", _cp),
                ("distribution_name", _cp), ("distribution_code_name", _cp), ("distribution_version", _cp),
                ("app_name", _cp), ("modules", ctypes.POINTER(_cp)), ("min_log_level", _c_int),
                ("log_dir", _cp), ("prebuilt_data_dir", _cp), ("staging_dir", _cp)]


class RimeComposition(ctypes.Structure):
    _fields_ = [("length", _c_int), ("cursor_pos", _c_int), ("sel_start", _c_int), ("sel_end", _c_int),
                ("preedit", _cp)]


class RimeCandidate(ctypes.Structure):
    _fields_ = [("text", _cp), ("comment", _cp), ("reserved", _vp)]


class RimeMenu(ctypes.Structure):
    _fields_ = [("page_size", _c_int), ("page_no", _c_int), ("is_last_page", _c_int),
                ("highlighted_candidate_index", _c_int), ("num_candidates", _c_int),
                ("candidates", ctypes.POINTER(RimeCandidate)), ("select_keys", _cp)]


class RimeContext(ctypes.Structure):
    _fields_ = [("data_size", _c_int), ("composition", RimeComposition), ("menu", RimeMenu),
                ("commit_text_preview", _cp), ("select_labels", ctypes.POINTER(_cp))]


class RimeCommit(ctypes.Structure):
    _fields_ = [("data_size", _c_int), ("text", _cp)]


# rime_api.h 里 RimeApi 的成员顺序 (全是函数指针), 只抄到用得着的最后一项
_API_NAMES = (
    "setup set_notification_handler initialize finalize start_maintenance is_maintenance_mode "
    "join_maintenance_thread deployer_initialize prebuild deploy deploy_schema deploy_config_file sync_user_data "
    "create_session find_session destroy_session cleanup_stale_sessions cleanup_all_sessions process_key "
    "commit_composition clear_composition get_commit free_commit get_context free_context get_status free_status "
    "set_option get_option set_property get_property get_schema_list free_schema_list get_current_schema "
    "select_schema schema_open config_open config_close config_get_bool config_get_int config_get_double "
    "config_get_string config_get_cstring config_update_signature config_begin_map config_next config_end "
    "simulate_key_sequence register_module find_module run_task get_shared_data_dir get_user_data_dir "
    "get_sync_dir get_user_id get_user_data_sync_dir config_init config_load_string config_set_bool config_set_int "
    "config_set_double config_set_string config_get_item config_set_item config_clear config_create_list "
    "config_create_map config_list_size config_begin_list get_input get_caret_pos select_candidate get_version "
    "set_caret_pos select_candidate_on_current_page").split()


class RimeApi(ctypes.Structure):
    _fields_ = [("data_size", _c_int)] + [(n, _vp) for n in _API_NAMES]


def _init(cls):
    """RIME_STRUCT_INIT: data_size = sizeof(结构) - sizeof(data_size)."""
    v = cls()
    v.data_size = ctypes.sizeof(cls) - ctypes.sizeof(_c_int)
    return v


def _fn(api, name, restype, *argtypes):
    """函数表里的一项 -> 可调用; 表 (data_size) 盖不到或是空指针 -> None."""
    end = getattr(RimeApi, name).offset + ctypes.sizeof(_vp)
    if api.data_size + ctypes.sizeof(_c_int) < end:
        return None
    addr = getattr(api, name)
    return ctypes.CFUNCTYPE(restype, *argtypes)(addr) if addr else None


def settings():
    env = os.environ.get
    return {"shared_dir": env("OPS_CART_RIME_SHARED") or SHARED_DIR,
            "user_dir": os.path.expanduser(env("OPS_CART_RIME_USER") or USER_DIR),
            "schema": env("OPS_CART_RIME_SCHEMA") or None}


# ------------------------------------------------------------ 进程级: 加载库 + 初始化一次

_lock = threading.Lock()
_state = {"api": None, "dirs": None, "keep": None}

_SIGS = {   # 名字: (返回, 参数...)
    "setup": (None, ctypes.POINTER(RimeTraits)), "initialize": (None, ctypes.POINTER(RimeTraits)),
    "start_maintenance": (_c_int, _c_int), "join_maintenance_thread": (None,),
    "create_session": (_sid,), "destroy_session": (_c_int, _sid),
    "process_key": (_c_int, _sid, _c_int, _c_int), "clear_composition": (None, _sid),
    "get_commit": (_c_int, _sid, ctypes.POINTER(RimeCommit)), "free_commit": (_c_int, ctypes.POINTER(RimeCommit)),
    "get_context": (_c_int, _sid, ctypes.POINTER(RimeContext)),
    "free_context": (_c_int, ctypes.POINTER(RimeContext)),
    "set_option": (None, _sid, _cp, _c_int), "select_schema": (_c_int, _sid, _cp),
}
_OPTIONAL = {"get_input": (_cp, _sid), "get_version": (_cp,),
             "select_candidate_on_current_page": (_c_int, _sid, _sid)}


def _load_api():
    """dlopen librime + rime_get_api -> {名字: 函数}; 缺必需项抛 OSError."""
    path = os.environ.get("OPS_CART_LIBRIME")
    names = [path] if path else list(_LIB_NAMES) + [ctypes.util.find_library("rime")]
    lib, errs = None, []
    for n in names:
        if not n:
            continue
        try:
            lib = ctypes.CDLL(n)
            break
        except OSError as e:
            errs.append(f"{n}: {e}")
    if lib is None:
        raise OSError("找不到 librime (" + ("; ".join(errs) or "系统里没有") + ")")
    get = lib.rime_get_api
    get.restype = ctypes.POINTER(RimeApi)
    p = get()
    if not p:
        raise OSError("rime_get_api 返回空")
    api = p.contents
    fns = {}
    for name, (res, *args) in list(_SIGS.items()) + list(_OPTIONAL.items()):
        f = _fn(api, name, res, *args)
        if f is None and name in _SIGS:
            raise OSError(f"这个版本的 librime 没有 {name}")
        fns[name] = f
    fns["_keep"] = p                      # 函数表本身别被回收
    return fns


def _ensure(shared_dir, user_dir):
    """进程内第一次: 加载 + setup + initialize + 部署 (要的话). 之后直接返回函数表."""
    with _lock:
        if _state["api"] is not None:
            if _state["dirs"] != (shared_dir, user_dir):
                raise OSError(f"librime 已按 {_state['dirs'][0]} 初始化, 同一进程换不了目录")
            return _state["api"]
        api = _load_api()
        os.makedirs(user_dir, exist_ok=True)
        keep = [s.encode() for s in (shared_dir, user_dir, "ops-cart", "ops-cart", "1", "rime.ops-cart", "")]
        t = _init(RimeTraits)
        (t.shared_data_dir, t.user_data_dir, t.distribution_name, t.distribution_code_name,
         t.distribution_version, t.app_name, t.log_dir) = keep
        t.min_log_level = 2                 # 只记 ERROR, 只写 stderr (log_dir="")
        api["setup"](ctypes.byref(t))
        api["initialize"](ctypes.byref(t))
        if api["start_maintenance"](0):     # 0 = 只在有变化时部署
            api["join_maintenance_thread"]()
        _state.update(api=api, dirs=(shared_dir, user_dir), keep=(keep, t))
        return api


# ------------------------------------------------------------ 一个会话 = 一个输入框的引擎

_BACKSPACE, _PAGE_UP, _PAGE_DOWN = 0xff08, 0xff55, 0xff56      # X11 keysym, librime 认这套


def _s(b):
    return b.decode("utf-8", "replace") if b else ""


class RimeEngine:
    """ime_core 要的引擎接口: feed / backspace / select / page / clear / snapshot / take_commit / close."""

    def __init__(self, api, session):
        self.api, self.sid = api, session

    def _key(self, code):
        self.api["process_key"](self.sid, code, 0)

    def feed(self, ch):
        self._key(ord(ch))

    def backspace(self):
        self._key(_BACKSPACE)

    def page(self, backward):
        self._key(_PAGE_UP if backward else _PAGE_DOWN)

    def select(self, i):
        f = self.api.get("select_candidate_on_current_page")
        if f is not None:
            return bool(f(self.sid, i))
        self._key(ord("1") + i)              # 老版本: 按选词键, 默认 1-9
        return True

    def clear(self):
        self.api["clear_composition"](self.sid)

    def snapshot(self):
        ctx = _init(RimeContext)
        if not self.api["get_context"](self.sid, ctypes.byref(ctx)):
            return ime_core.Snapshot("", "", [], 0, True, 0)
        try:
            m = ctx.menu
            cands = [_s(m.candidates[i].text) for i in range(m.num_candidates)] if m.candidates else []
            preedit = _s(ctx.composition.preedit)
            get_input = self.api.get("get_input")
            raw = _s(get_input(self.sid)) if get_input else preedit.replace(" ", "")
            return ime_core.Snapshot(raw=raw, preedit=preedit, candidates=cands, page_no=m.page_no,
                                     last_page=bool(m.is_last_page), highlighted=m.highlighted_candidate_index)
        finally:
            self.api["free_context"](ctypes.byref(ctx))

    def take_commit(self):
        c = _init(RimeCommit)
        if not self.api["get_commit"](self.sid, ctypes.byref(c)):
            return None
        try:
            return _s(c.text) or None
        finally:
            self.api["free_commit"](ctypes.byref(c))

    def close(self):
        if self.sid:
            self.api["destroy_session"](self.sid)
            self.sid = 0


def open_engine(shared_dir=None, user_dir=None, schema=None):
    """-> (RimeEngine, "") 或 (None, "中文输入不可用: 原因"). 不抛异常; 可能慢 (首次部署), 放后台线程."""
    cfg = settings()
    shared_dir, user_dir = shared_dir or cfg["shared_dir"], user_dir or cfg["user_dir"]
    schema = schema or cfg["schema"]
    if not os.path.isdir(shared_dir):
        return None, f"中文输入不可用: 没有词库目录 {shared_dir} (装 librime 数据包或设 OPS_CART_RIME_SHARED)"
    try:
        api = _ensure(shared_dir, user_dir)
        sid = api["create_session"]()
        if not sid:
            return None, "中文输入不可用: librime 开不了会话 (方案没部署成功? 看 stderr 里的 librime 日志)"
        eng = RimeEngine(api, sid)
        if schema:
            # 没列进 schema_list 的方案不会部署, select_schema 照样返回成功, 之后按键全不认; 先看部署产物在不在
            built = [os.path.join(d, "build", schema + ".schema.yaml") for d in (user_dir, shared_dir)]
            if not any(os.path.exists(b) for b in built) or not api["select_schema"](sid, schema.encode()):
                eng.close()
                return None, (f"中文输入不可用: 方案 {schema} 没部署 (要列进 default.custom.yaml 的 schema_list, "
                              f"或者词库里没有它)")
        api["set_option"](sid, b"ascii_mode", 0)       # 中英由 ime_core 管, librime 自己保持中文
        return eng, ""
    except Exception as e:
        return None, f"中文输入不可用: {e}"
