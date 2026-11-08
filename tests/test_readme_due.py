"""scripts/readme_due.py: 数 README 上次改动之后合并了几个 PR. 在临时 git 仓库里造提交测."""

import importlib.util
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("readme_due", os.path.join(ROOT, "scripts", "readme_due.py"))
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-11-01T12:00:00", GIT_COMMITTER_DATE="2026-11-01T12:00:00")
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def commit(repo, subject, path="code.py"):
    p = repo / path
    p.write_text((p.read_text() if p.exists() else "") + subject + "\n")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", subject)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    commit(tmp_path, "初始 (#1)")
    commit(tmp_path, "README 整理", path="README.md")
    return tmp_path


def test_counts_only_pr_squash_commits_after_readme(repo):
    commit(repo, "功能甲 (#2)")
    commit(repo, "直接改的小东西")
    commit(repo, "功能乙 (#13)")
    commit(repo, "标题中间有 (#4) 不算")
    h, date, n = rd.status(str(repo), "main")
    assert n == 2
    assert date == "2026-11-01"
    assert h == git(repo, "log", "-1", "--format=%h", "--", "README.md")


def test_commit_before_readme_not_counted(repo):
    assert rd.status(str(repo), "main")[2] == 0


def test_readme_change_in_pr_commit_resets_count(repo):
    commit(repo, "功能甲 (#2)")
    commit(repo, "README 跟上 (#3)", path="README.md")
    commit(repo, "功能乙 (#5)")
    assert rd.status(str(repo), "main")[2] == 1


def test_main_below_threshold_exit_0(repo, capsys):
    commit(repo, "功能甲 (#2)")
    assert rd.main(["--repo", str(repo), "--ref", "main"]) == 0
    out = capsys.readouterr().out
    assert "之后合并了 1 个 PR" in out and "该整理 README 了" not in out
    assert out.startswith("README 上次整理在 ")


def test_main_at_threshold_exit_1(repo, capsys):
    for i in range(3):
        commit(repo, "功能 (#%d)" % (i + 10))
    assert rd.main(["--repo", str(repo), "--ref", "main", "--threshold", "3"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == "该整理 README 了"
    assert "之后合并了 3 个 PR" in lines[0]


def test_default_threshold_is_10(repo):
    for i in range(9):
        commit(repo, "功能 (#%d)" % (i + 10))
    assert rd.main(["--repo", str(repo), "--ref", "main"]) == 0
    commit(repo, "功能 (#99)")
    assert rd.main(["--repo", str(repo), "--ref", "main"]) == 1


def test_no_readme_commit_is_error(tmp_path, capsys):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    commit(tmp_path, "初始 (#1)")
    assert rd.main(["--repo", str(tmp_path), "--ref", "main"]) == 2
