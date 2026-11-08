"""审批推送通道的接口和假实现 (#50 第一阶段不接真飞书).

真实的飞书通道 (设计稿 4.3) 是下一阶段: 专用应用、只发白名单账号的单聊、长连接 + 官方 SDK 装在独立 venv,
回调用 card.action.trigger (字段和签名实现前对照官方文档核对). 到时候实现一个 Channel 子类即可,
notify() 和审计不用改. 卡片按纯文本发, 不当 markdown 渲染 (不可信文字已在 approval_core 里清洗)."""

import approval_core as ac
import totp_core as tc


class Channel:
    """send_card(账号, 卡片) -> 送达了没有 (bool). 网络错误可以直接抛, notify 会记成 receipt_fail."""
    name = "base"

    def send_card(self, account, card):
        raise NotImplementedError


class FakeChannel(Channel):
    """测试用: 记下发了什么; fail_for 里的账号模拟没送达."""
    name = "feishu"

    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send_card(self, account, card):
        if account in self.fail_for:
            return False
        self.sent.append((account, card))
        return True


def notify(channel, accounts, item, now):
    """把一条挂起项的卡片推给每个白名单账号 -> 每个账号一行审计 (ev=notify, result=sent/receipt_fail/bad_account)."""
    c = ac.card(item, channel.name, now)
    recs = []
    for acct in accounts:
        if not tc.valid_account(acct):
            result = "bad_account"
        else:
            try:
                result = "sent" if channel.send_card(acct, dict(c)) else "receipt_fail"
            except Exception:                   # 通道边界: 任何错误都只算没送达
                result = "receipt_fail"
        recs.append(ac.audit("notify", item, channel.name, now, result,
                             account=acct if tc.valid_account(acct) else None))
    return recs
