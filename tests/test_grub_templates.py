"""被跟踪的 GRUB 菜单模板和脚本里的 heredoc: 默认项不许自动装机 (不许带 inst.ks).

自动装机菜单只能由推车解锁时按 MAC 单独生成 (pxeserver.grub_menu(install=True)),
仓库里任何模板、部署脚本写出来的菜单默认项都必须是救援."""

import pathlib
import re
import subprocess

import pytest

import kiosk_core as kc

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_SUFFIXES = (".sh", ".ps1", ".bat", ".cmd")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*$", re.S | re.M)
_PS_HERE = re.compile(r"@['\"]\s*\n(.*?)\n['\"]@", re.S)
_ENTRY = re.compile(r"^\s*menuentry\b", re.M)


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
        return [ROOT / f for f in out]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def default_entry(cfg):
    """GRUB 文本 -> 默认项的正文; 没有 menuentry 返回 None. default 不是数字时按最坏情况:
    只要有任何一项带 inst.ks 就当默认项带."""
    parts = _ENTRY.split(cfg)[1:]
    if not parts:
        return None
    m = re.search(r"^\s*set\s+default=[\"']?([^\s\"']+)", cfg, re.M)
    idx = m.group(1) if m else "0"
    if not idx.isdigit():
        return "\n".join(parts)
    return parts[int(idx)] if int(idx) < len(parts) else ""


def grub_texts():
    """(来源, 菜单文本): grub*.cfg 文件本身 + 脚本 heredoc / PowerShell here-string 里的菜单."""
    out = []
    for p in tracked():
        if "grub" in p.name and p.suffix == ".cfg":
            out.append((str(p.relative_to(ROOT)), p.read_text(errors="replace")))
        elif p.suffix in SCRIPT_SUFFIXES:
            text = p.read_text(errors="replace")
            bodies = [m.group(2) for m in _HEREDOC.finditer(text)] + _PS_HERE.findall(text)
            out += [(f"{p.relative_to(ROOT)} heredoc", b) for b in bodies if _ENTRY.search(b)]
    return out


def test_found_the_templates():
    names = [n for n, _ in grub_texts()]
    assert "windows/grub.cfg" in names


@pytest.mark.parametrize("name,cfg", grub_texts())
def test_template_default_entry_is_not_install(name, cfg):
    entry = default_entry(cfg)
    assert entry is not None, name
    assert "inst.ks" not in entry, name


def test_generated_rescue_menu_default_is_rescue():
    entry = default_entry(kc.grub_cfg_rescue())
    assert "inst.rescue" in entry and "inst.ks" not in entry


def test_default_entry_parser_catches_install_default():
    bad = "set default=1\nmenuentry 'a' {\n linux /vmlinuz inst.rescue\n}\nmenuentry 'b' {\n linux /vmlinuz inst.ks=x\n}\n"
    assert "inst.ks" in default_entry(bad)
    assert "inst.ks" in default_entry(bad.replace("set default=1", "set default=saved"))
    assert "inst.ks" not in default_entry(bad.replace("set default=1", "set default=0"))


def test_heredoc_scanner_sees_script_menus():
    script = "cat > x/grub.cfg <<'EOF'\nset default=0\nmenuentry 'x' {\n linux /vmlinuz inst.ks=y\n}\nEOF\n"
    bodies = [m.group(2) for m in _HEREDOC.finditer(script)]
    assert bodies and "inst.ks" in default_entry(bodies[0])


def test_setup_script_does_not_write_menus_itself():
    sh = (ROOT / "scripts" / "setup-cart.sh").read_text()
    assert "kiosk_core.py --write-rescue" in sh
    for line in sh.splitlines():                           # 不自己往 grub.cfg 里写
        code = line.split("#", 1)[0].strip()
        if code.startswith("echo"):
            continue
        assert not re.search(r"(>>?|\btee\b|\bcp\b|\binstall\b)[^#]*grub\.cfg", code), line
    assert "menuentry" not in sh
