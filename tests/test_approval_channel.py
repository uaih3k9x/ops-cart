"""飞书通道接口 + 假实现 (#50 第一阶段不接真飞书)."""

import pytest

import approval_channel as ach
import approval_core as ac

NOW = 1_700_000_010.0


def item():
    return ac.make_item("serial", 17, "1a2b3c4d", target="slot:2", created=NOW - 60,
                        action={"type": "line", "text": "vlan 20"}, ctx=5, agent="claude-laptop")


def test_base_channel_is_abstract():
    with pytest.raises(NotImplementedError):
        ach.Channel().send_card("alice", {})


def test_fake_channel_records_cards_per_account():
    ch = ach.FakeChannel()
    recs = ach.notify(ch, ["alice", "bob"], item(), NOW)
    assert [a for a, _ in ch.sent] == ["alice", "bob"]
    assert all(c["text"] == ac.render(item(), NOW)["text"] for _, c in ch.sent)
    assert [r["ev"] for r in recs] == ["notify", "notify"] and all(r["result"] == "sent" for r in recs)


def test_fake_channel_failure_gives_receipt_fail():
    ch = ach.FakeChannel(fail_for={"bob"})
    recs = ach.notify(ch, ["alice", "bob"], item(), NOW)
    assert [r["result"] for r in recs] == ["sent", "receipt_fail"]


def test_channel_exception_is_receipt_fail_not_crash():
    class Boom(ach.Channel):
        name = "feishu"

        def send_card(self, account, card):
            raise OSError("network down")

    recs = ach.notify(Boom(), ["alice"], item(), NOW)
    assert recs[0]["result"] == "receipt_fail"


def test_notify_skips_bad_account_names():
    ch = ach.FakeChannel()
    recs = ach.notify(ch, ["Alice Smith"], item(), NOW)
    assert ch.sent == [] and recs[0]["result"] == "bad_account"


def test_card_sent_carries_no_codes():
    ch = ach.FakeChannel()
    ach.notify(ch, ["alice"], item(), NOW)
    card = ch.sent[0][1]
    assert set(card) == {"title", "text", "buttons", "totp", "id", "digest"}
