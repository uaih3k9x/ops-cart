"""防断电的写文件: 写临时文件 → fsync → 改名 → fsync 目录. 断电时文件要么是旧的要么是新的.

推车经常不关机就拔电挪车; 盘有掉电保护, 但"写到一半"的半个文件得靠这里兜住.
mode=None 时沿用旧文件的权限, 新文件 0644.
"""

import json
import os


def write_text(path, text, mode=None):
    path = str(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = 0o644
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_json(path, obj, mode=None, **dump_kw):
    """先序列化再写: 序列化失败不碰旧文件."""
    dump_kw.setdefault("ensure_ascii", False)
    write_text(path, json.dumps(obj, **dump_kw), mode=mode)
