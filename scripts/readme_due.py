#!/usr/bin/env python3
"""README 该整理了吗: 数 <ref> 上最后一次改 README.md 之后合并了几个 PR.

仓库用 squash 合并, PR 合并提交的标题以 "(#数字)" 结尾. 到阈值 (默认 10) 退出码 1, 否则 0; 出错 2.
    python3 scripts/readme_due.py [--ref origin/main] [--threshold 10]
"""

import argparse
import os
import re
import subprocess
import sys

PR_SUBJECT = re.compile(r"\(#\d+\)$")


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True,
                          text=True).stdout.strip()


def _at_least_1(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("阈值至少是 1")
    return n


def status(repo, ref):
    """-> (短哈希, 日期, PR 数). 浅克隆或 README.md 从没改过时抛 LookupError."""
    if _git(repo, "rev-parse", "--is-shallow-repository") == "true":
        raise LookupError("浅克隆数不准, 先 git fetch --unshallow")
    last = _git(repo, "log", "-1", "--format=%h %ad", "--date=short", ref, "--", "README.md")
    if not last:
        raise LookupError("%s 上找不到改过 README.md 的提交" % ref)
    h, date = last.split()
    subjects = _git(repo, "log", "--format=%s", "%s..%s" % (h, ref)).splitlines()
    return h, date, sum(1 for s in subjects if PR_SUBJECT.search(s.strip()))


def main(argv=None):
    ap = argparse.ArgumentParser(description="README 上次整理之后合并了几个 PR")
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--threshold", type=_at_least_1, default=10, help="到这么多个就提示整理 (默认 10)")
    a = ap.parse_args(argv)
    try:
        h, date, n = status(a.repo, a.ref)
    except (LookupError, subprocess.CalledProcessError) as e:
        print("readme_due: %s" % (getattr(e, "stderr", None) or e), file=sys.stderr)
        return 2
    print("README 上次整理在 %s %s，之后合并了 %d 个 PR" % (h, date, n))
    if n >= a.threshold:
        print("该整理 README 了")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
