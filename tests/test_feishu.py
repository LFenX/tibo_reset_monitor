"""Feishu request formation for group and personal targets."""

from __future__ import annotations

import json
import logging
import os
import unittest

from tests.support import FakeClock, make_event, make_post, snapshot, utc
from tibo_reset_monitor.channels.feishu import FeishuChannel
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.http import HttpResponse, TransportError
from tibo_reset_monitor.core.model import Target
from tibo_reset_monitor.logutil import configure_logging


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def send(self, method, url, headers, body, timeout):
        call = {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        self.calls.append(call)
        if url.endswith("/tenant_access_token/internal"):
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"code": 0, "msg": "ok", "tenant_access_token": "t-test-token", "expire": 7200}).encode(),
                url,
            )
        if "receive_id_type=chat_id" in url and b"oc_group" in body:
            return HttpResponse(200, {}, json.dumps({"code": 0, "msg": "success", "data": {"message_id": "om_group", "chat_id": "oc_group"}}).encode(), url)
        if "receive_id_type=email" in url:
            return HttpResponse(200, {}, json.dumps({"code": 0, "msg": "success", "data": {"message_id": "om_mail", "chat_id": "oc_virtual_dm"}}).encode(), url)
        if "receive_id_type=open_id" in url:
            return HttpResponse(400, {}, json.dumps({"code": 230002, "msg": "Bot/User can NOT be out of the chat."}).encode(), url)
        raise AssertionError(url)


class FeishuRequestTest(unittest.TestCase):
    def test_bad_success_receipts_are_ambiguous(self):
        cases = [[], {}, {"message_id": 7, "chat_id": "oc_group"},
                 {"message_id": "", "chat_id": "oc_group"},
                 {"message_id": "om_virtual", "chat_id": "oc_other"}]
        target = Target("feishu", "ops", "group", "oc_group", {"receive_id_type": "chat_id"})
        for data in cases:
            with self.subTest(data=data):
                class BadReceiptTransport(FakeTransport):
                    def send(inner, method, url, headers, body, timeout):
                        if "/im/v1/messages?" in url:
                            return HttpResponse(200, {}, json.dumps({"code": 0, "data": data}).encode(), url)
                        return super().send(method, url, headers, body, timeout)
                self.channel._transport = BadReceiptTransport()
                receipt = self.channel.send(self.channel.render(self.notice, target), target, "same-virtual-key")
                self.assertEqual(receipt.outcome, "ambiguous")

    def setUp(self) -> None:
        self._secret = "test-secret-not-real"
        os.environ["TIBO_FEISHU_APP_SECRET"] = self._secret
        self.addCleanup(os.environ.pop, "TIBO_FEISHU_APP_SECRET", None)
        self.transport = FakeTransport()
        self.clock = FakeClock(utc())
        self.channel = FeishuChannel(
            ChannelSpec(
                name="feishu",
                enabled=True,
                settings={
                    "api_base": "https://open.feishu.cn",
                    "app_id": "cli_example_not_a_real_app_id",
                    "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                },
                targets=[],
            ),
            self.transport,
            self.clock,
        )
        self.notice = extract_notices(snapshot([make_event("reset-1", [make_post("p1", "预告", text="今天重置 <b>")])]))[0]

    def test_group_and_personal_requests(self):
        configure_logging("DEBUG", None)
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logging.getLogger("tibo_reset_monitor").addHandler(Grab())
        group = Target("feishu", "ops", "group", "oc_group", {"receive_id_type": "chat_id"})
        mail = Target("feishu", "me", "personal", "person@example.com", {"receive_id_type": "email"})
        group_message = self.channel.render(self.notice, group)
        mail_message = self.channel.render(self.notice, mail)
        group_receipt = self.channel.send(group_message, group, "trm" + "a" * 45)
        mail_receipt = self.channel.send(mail_message, mail, "trm" + "b" * 45)
        self.assertEqual(group_receipt.outcome, "sent")
        self.assertEqual(group_receipt.provider_message_id, "om_group")
        self.assertEqual(mail_receipt.provider_message_id, "om_mail")

        token_calls = [call for call in self.transport.calls if call["url"].endswith("/tenant_access_token/internal")]
        self.assertEqual(len(token_calls), 1)
        token_body = json.loads(token_calls[0]["body"].decode())
        self.assertEqual(token_body["app_id"], "cli_example_not_a_real_app_id")
        self.assertEqual(token_body["app_secret"], self._secret)
        self.assertNotIn("Authorization", token_calls[0]["headers"])

        message_calls = [call for call in self.transport.calls if "/im/v1/messages?" in call["url"]]
        self.assertEqual(len(message_calls), 2)
        self.assertIn("receive_id_type=chat_id", message_calls[0]["url"])
        self.assertIn("receive_id_type=email", message_calls[1]["url"])
        self.assertTrue(message_calls[0]["headers"]["Authorization"].startswith("Bearer "))
        group_body = json.loads(message_calls[0]["body"].decode())
        self.assertEqual(group_body["receive_id"], "oc_group")
        self.assertEqual(group_body["msg_type"], "interactive")
        self.assertEqual(group_body["uuid"], "trm" + "a" * 45)
        self.assertLessEqual(len(group_body["uuid"]), 50)
        self.assertIsInstance(group_body["content"], str)
        card = json.loads(group_body["content"])
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("预告", card["header"]["title"]["content"])
        self.assertIn("@thsottiaux", card["header"]["subtitle"]["content"])
        self.assertEqual(card["body"]["elements"][0]["tag"], "markdown")
        self.assertIn("今天重置", card["body"]["elements"][0]["content"])
        self.assertIn("来源帖子的发布时间", card["body"]["elements"][0]["content"])
        self.assertEqual(card["body"]["elements"][1]["tag"], "button")
        self.assertEqual(card["body"]["elements"][1]["behaviors"][0]["default_url"], "https://x.com/thsottiaux/status/p1")
        self.assertNotIn("img", json.dumps(card))
        mail_body = json.loads(message_calls[1]["body"].decode())
        self.assertEqual(mail_body["receive_id"], "person@example.com")
        logged = "\n".join(records)
        self.assertNotIn(self._secret, logged)
        self.assertNotIn("t-test-token", logged)

    def test_english_card_uses_the_original_post(self):
        target = Target("feishu", "ops", "group", "oc_group", {"receive_id_type": "chat_id", "language": "en"})
        rendered = self.channel.render(self.notice, target)
        card = json.loads(rendered["content"])
        title = card["header"]["title"]["content"]
        body = card["body"]["elements"][0]["content"]
        button = card["body"]["elements"][1]
        self.assertIn("Preview", title)
        self.assertIn("Reset (form unspecified, defaults to quota reset)", title)
        self.assertNotIn("预告", title)
        self.assertIn(self.notice.original_text, body)
        self.assertNotIn("今天重置", body)
        self.assertNotIn("原文", body)
        self.assertIn("Scope not specified in the post", body)
        self.assertIn("Posted ·", body)
        self.assertIn("Data from the public AIHOT feed", body)
        self.assertIn("Reset calendar ·", body)
        self.assertEqual(button["text"]["content"], "View original post")
        self.assertNotIn("查看原帖", rendered["content"])
        self.assertIsNone(rendered["photo"])

    def test_calendar_image_is_uploaded_and_attached(self):
        class ImageTransport(FakeTransport):
            def send(self, method, url, headers, body, timeout):
                if url.endswith("/im/v1/images"):
                    self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
                    return HttpResponse(
                        200,
                        {},
                        json.dumps({"code": 0, "msg": "ok", "data": {"image_key": "img_calendar"}}).encode(),
                        url,
                    )
                return super().send(method, url, headers, body, timeout)

        transport = ImageTransport()
        channel = FeishuChannel(
            ChannelSpec(
                name="feishu",
                enabled=True,
                settings={
                    "api_base": "https://open.feishu.cn",
                    "app_id": "cli_example_not_a_real_app_id",
                    "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                },
                targets=[],
            ),
            transport,
            self.clock,
        )
        channel.bind_calendar(lambda language="zh": b"\x89PNG\r\ncalendar")
        target = Target("feishu", "ops", "group", "oc_group", {"receive_id_type": "chat_id", "language": "zh"})
        message = channel.render(self.notice, target)
        self.assertTrue(message["photo"].startswith(b"\x89PNG"))
        receipt = channel.send(message, target, "trm" + "c" * 45)
        self.assertEqual(receipt.outcome, "sent")
        uploads = [call for call in transport.calls if call["url"].endswith("/im/v1/images")]
        self.assertEqual(len(uploads), 1)
        self.assertIn(b"calendar.png", uploads[0]["body"])
        self.assertIn(b"\x89PNG", uploads[0]["body"])
        sent = [call for call in transport.calls if "/im/v1/messages?" in call["url"]]
        card = json.loads(json.loads(sent[0]["body"].decode())["content"])
        image = card["body"]["elements"][-1]
        self.assertEqual(image["tag"], "img")
        self.assertEqual(image["img_key"], "img_calendar")
        self.assertEqual(card["body"]["elements"][1]["tag"], "button")
        self.assertIn("今天重置", card["body"]["elements"][0]["content"])

    def test_personal_open_id_permanent_failure_and_timeout(self):
        person = Target("feishu", "me", "personal", "ou_example", {"receive_id_type": "open_id"})
        message = self.channel.render(self.notice, person)
        receipt = self.channel.send(message, person, "trm-key")
        self.assertEqual(receipt.outcome, "permanent")
        self.assertIn("230002", receipt.detail)

        def explode(*_args, **_kwargs):
            raise TransportError("timeout talking to https://open.feishu.cn/open-apis/im/v1/messages", ambiguous=True)

        self.channel._token_until = self.clock.now()
        self.transport.send = explode
        self.channel._token = "t-test-token"
        from datetime import timedelta

        self.channel._token_until = self.clock.now() + timedelta(hours=1)
        timed = self.channel.send(message, person, "trm-key")
        self.assertEqual(timed.outcome, "ambiguous")

    def test_missing_secret_is_retryable_and_does_not_call_network(self):
        os.environ.pop("TIBO_FEISHU_APP_SECRET", None)
        person = Target("feishu", "me", "personal", "ou_example", {"receive_id_type": "open_id"})
        receipt = self.channel.send(self.channel.render(self.notice, person), person, "trm-key")
        self.assertEqual(receipt.outcome, "retryable")
        self.assertIn("TIBO_FEISHU_APP_SECRET", receipt.detail)
        self.assertEqual(self.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
