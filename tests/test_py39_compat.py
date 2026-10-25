"""推车上是 Python 3.9: 被跟踪的 .py 不许用 match、运行时求值的 X | Y 注解等 3.10+ 语法."""

import ast
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py_files():
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        out = [f for f in os.listdir(ROOT) if f.endswith(".py")]
    return sorted(set(out) | {"scripts/check_public_safe.py"})     # 新文件提交前也要查


class _NewSyntax(ast.NodeVisitor):
    def __init__(self, future_ann):
        self.hits, self.future_ann = [], future_ann

    def visit_Match(self, node):                          # Python 3.13 的 ast 才有这个节点
        self.hits.append((node.lineno, "match 语句"))

    def _ann(self, ann):
        if ann is None or self.future_ann:
            return
        for n in ast.walk(ann):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
                self.hits.append((n.lineno, "注解里的 X | Y (3.9 运行时会炸)"))
                return

    def visit_FunctionDef(self, node):
        for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            self._ann(a.annotation)
        self._ann(node.returns)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        self._ann(node.annotation)
        self.generic_visit(node)

    def visit_Call(self, node):                           # isinstance(x, int | str) 也是 3.10+
        if getattr(node.func, "id", None) in ("isinstance", "issubclass") and len(node.args) == 2:
            t = node.args[1]
            if isinstance(t, ast.BinOp) and isinstance(t.op, ast.BitOr):
                self.hits.append((node.lineno, "isinstance 里的 X | Y (3.9 运行时会炸)"))
        self.generic_visit(node)


def _hits(src):
    v = _NewSyntax("from __future__ import annotations" in src)
    v.visit(ast.parse(src))
    return [why for _n, why in v.hits]


def test_checker_catches_runtime_union_and_match():
    assert _hits("def f(x: int | None): pass") != []
    assert _hits("def f() -> int | None: pass") != []
    assert _hits("x: int | str = 1") != []
    assert _hits("isinstance(1, int | str)") != []
    assert _hits("match x:\n    case 1: pass") != []


def test_checker_allows_safe_forms():
    assert _hits("from __future__ import annotations\ndef f(x: int | None): pass") == []
    assert _hits("flags = 1 | 2\nisinstance(1, (int, str))") == []
    assert _hits("def f(x: 'int | None'): pass") == []


@pytest.mark.parametrize("rel", _py_files())
def test_parses_as_python39(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    tree = ast.parse(src, filename=rel, feature_version=(3, 9))
    v = _NewSyntax("from __future__ import annotations" in src)
    v.visit(tree)
    assert v.hits == [], f"{rel}: {v.hits}"
