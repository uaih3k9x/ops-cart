"""clone_core: Clonezilla 启动参数生成 (#18). 纯函数, 不碰文件."""

import pytest

import clone_core as cc

IP = "192.0.2.1"
SMB = {"proto": "smb", "user": "clonesave", "password": "Example-pass1",
       "save_share": "clone-rw", "restore_share": "clone-ro", "local_dir": "/srv/data/clone"}
NFS = {"proto": "nfs", "save_share": "/srv/data/clone", "restore_share": "/srv/data/clone-ro",
       "local_dir": "/srv/data/clone"}
DEV = "SERIALNO=SN-EXAMPLE-1"


def _param(params, key):
    hits = [p for p in params if p.startswith(key + "=")]
    assert len(hits) == 1, (key, params)
    return hits[0][len(key) + 1:]


# ---------------------------------------------------------------- 镜像名

@pytest.mark.parametrize("name", ["lab-image-example", "win11_2026.10", "A1"])
def test_image_name_ok(name):
    assert cc.image_name_error(name) is None


@pytest.mark.parametrize("name", ["", " ", "a b", "../x", "x/y", ".hidden", "a\"b", "a$b", "x" * 65,
                                  "ask_user", "autoname", "autohostname", "autoproductname",
                                  "autoname-lab", None, 3])
def test_image_name_refused(name):
    assert cc.image_name_error(name)


# ---------------------------------------------------------------- ocs-sr 命令

def test_restore_cmd_exact():
    assert cc.ocs_sr_cmd("clone-restore", "lab-img", DEV) == (
        "ocs-sr -g auto -e1 auto -e2 -r -j2 -k0 -p poweroff -batch restoredisk lab-img " + DEV)


def test_save_cmd_exact():
    assert cc.ocs_sr_cmd("clone-save", "lab-img", DEV) == (
        "ocs-sr -q2 -j2 -z9p -sfsck -senc -p poweroff -batch savedisk lab-img " + DEV)


@pytest.mark.parametrize("purpose", cc.CLONE_PURPOSES)
def test_cmd_never_skips_safety_checks(purpose):
    """只许 -batch 跳过确认; 跳过检查/不建分区表/按比例改分区的一律不许出现, -b 也不许 (init 会吃掉)."""
    words = cc.ocs_sr_cmd(purpose, "lab-img", DEV).split()
    assert "-batch" in words
    for bad in ("-b", "--batch", "-scr", "-icds", "-k", "-k1", "-k2", "-iefi", "-c", "-p reboot"):
        assert bad not in words, bad
    assert words[words.index("-p") + 1] == "poweroff"         # 不重启: 免得 PXE 回来再来一遍


@pytest.mark.parametrize("purpose", cc.CLONE_PURPOSES)
def test_cmd_device_must_be_serialno(purpose):
    for dev in ("sda", "/dev/sda", "nvme0n1", "all", "ask_user", "", None, "SERIALNO=", "SERIALNO=a b",
                "SERIALNO=x;reboot", "PTUUID=03c8b280"):
        with pytest.raises(ValueError):
            cc.ocs_sr_cmd(purpose, "lab-img", dev)


def test_cmd_refuses_bad_image_and_purpose():
    with pytest.raises(ValueError):
        cc.ocs_sr_cmd("clone-restore", "ask_user", DEV)
    with pytest.raises(ValueError):
        cc.ocs_sr_cmd("rocky", "lab-img", DEV)


def test_multicast_is_an_interface_not_an_implementation():
    """第一版只做单播; 组播的参数位置留着, 传了就明确报没做, 不偷偷退成单播."""
    with pytest.raises(NotImplementedError):
        cc.ocs_sr_cmd("clone-restore", "lab-img", DEV,
                      multicast={"clients_to_wait": 2, "max_wait_s": 300, "max_bitrate_mbps": 500})
    with pytest.raises(ValueError):                       # 抓镜像没有组播这回事
        cc.ocs_sr_cmd("clone-save", "lab-img", DEV, multicast={"clients_to_wait": 2})


# ---------------------------------------------------------------- 镜像仓库

def test_repo_smb_save_and_restore_use_different_shares():
    assert cc.repo_url(SMB, "clone-save", IP) == "smb://clonesave:Example-pass1@192.0.2.1/clone-rw/"
    assert cc.repo_url(dict(SMB, restore_user="", restore_password=""), "clone-restore", IP) == \
        "smb://192.0.2.1/clone-ro/"


def test_repo_smb_restore_user_separate_from_save_user():
    conf = dict(SMB, restore_user="cloneread", restore_password="Example-ro1")
    assert cc.repo_url(conf, "clone-restore", IP) == "smb://cloneread:Example-ro1@192.0.2.1/clone-ro/"


def test_repo_smb_restore_never_reuses_write_account():
    """恢复只读挂: 没写 restore_user 时不拿抓镜像那个能写的账号去挂, 当访客."""
    assert "clonesave" not in cc.repo_url(SMB, "clone-restore", IP)


def test_repo_smb_version_and_server():
    conf = dict(SMB, smb_version="smb3", server="192.0.2.5")
    assert cc.repo_url(conf, "clone-save", IP) == "smb3://clonesave:Example-pass1@192.0.2.5/clone-rw/"


def test_repo_nfs():
    assert cc.repo_url(NFS, "clone-save", IP) == "nfs://192.0.2.1/srv/data/clone/"
    assert cc.repo_url(NFS, "clone-restore", IP) == "nfs://192.0.2.1/srv/data/clone-ro/"
    assert cc.repo_url(dict(NFS, proto="nfs4"), "clone-restore", IP) == "nfs4://192.0.2.1/srv/data/clone-ro/"


@pytest.mark.parametrize("bad,purpose", [
    ({}, "clone-save"), ({}, "clone-restore"),
    ({"proto": "ftp", "save_share": "x", "restore_share": "y"}, "clone-save"),
    (dict(SMB, save_share=""), "clone-save"), (dict(SMB, restore_share=None), "clone-restore"),
    (dict(SMB, password="has space"), "clone-save"), (dict(SMB, password="a@b"), "clone-save"),
    (dict(SMB, password='q"x'), "clone-save"), (dict(SMB, password="$x"), "clone-save"),
    (dict(SMB, user="a:b"), "clone-save"), (dict(SMB, save_share="a b"), "clone-save"),
    (dict(SMB, save_share="../etc"), "clone-save"), (dict(SMB, server="192.0.2.1;reboot"), "clone-save"),
    (dict(SMB, smb_version="smb9"), "clone-restore"),
    (dict(SMB, password=""), "clone-save"),                 # 写账号要么不写用户, 要么带密码
    (dict(SMB, restore_user="r", restore_password="a b"), "clone-restore"),
    (dict(NFS, restore_share="relative/path"), "clone-restore"),
    (SMB, "rocky"),
])
def test_repo_bad_conf_refused(bad, purpose):
    with pytest.raises(ValueError):
        cc.repo_url(bad, purpose, IP)


# ---------------------------------------------------------------- 整串启动参数

@pytest.mark.parametrize("purpose", cc.CLONE_PURPOSES)
def test_boot_params_shape(purpose):
    params = cc.boot_params(purpose, "lab-img", DEV, SMB, IP)
    assert _param(params, "ocs_live_run") == '"' + cc.ocs_sr_cmd(purpose, "lab-img", DEV) + '"'
    assert _param(params, "ocs_live_extra_param") == '""'
    assert _param(params, "ocs_live_batch") == '"yes"'
    assert _param(params, "ocs_repository") == '"' + cc.repo_url(SMB, purpose, IP) + '"'
    for p in ("boot=live", "union=overlay", "noprompt", "locales=en_US.UTF-8", "keyboard-layouts=us"):
        assert p in params
    assert not any(p.startswith(("fetch=", "ocs_prerun", "ocs_postrun", "ocs_daemonon")) for p in params)


@pytest.mark.parametrize("purpose", cc.CLONE_PURPOSES)
def test_boot_params_are_grub_safe(purpose):
    """每个参数一个 token, 引号只在值两头; 没有 GRUB 会展开/截断的字符."""
    for p in cc.boot_params(purpose, "lab-img", DEV, NFS, IP):
        assert not any(c in p for c in "$\\'`;{}\n\t#"), p
        body = p.split("=", 1)[1] if "=" in p else p
        assert '"' not in body.strip('"'), p


def test_boot_params_restore_device_comes_through():
    run = _param(cc.boot_params("clone-restore", "lab-img", DEV, SMB, IP), "ocs_live_run")
    assert run.endswith(" restoredisk lab-img " + DEV + '"')
