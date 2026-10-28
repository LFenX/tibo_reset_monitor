"""Every registered channel has to render both zh and en from the shared copy."""

from __future__ import annotations

import json
import unittest

from tests.support import FakeClock, make_event, make_post, snapshot
from tibo_reset_monitor.channels import REGISTRY
from tibo_reset_monitor.config import ChannelSpec
from tibo_reset_monitor.core.dedupe import extract_notices
from tibo_reset_monitor.core.i18n import compose
from tibo_reset_monitor.core.model import Target, notice_lines


def _notice():
    post = make_post("p9", "预告", text="搞定了。明天见。", original="short")
    post["fullText"] = post["text"]
    post["fullOriginalText"] = "And it's done. See you tomorrow."
    post["originalText"] = "And it's done."
    post["context"] = [
        {
            "id": "quoted",
            "author": "thsottiaux",
            "relation": "quote",
            "text": "中文引用",
            "originalText": "Lands by midnight PST.",
        }
    ]
    return extract_notices(snapshot([make_event("reset-9", [post])]))[0]


def _channel(name, factory):
    return factory(
        ChannelSpec(
            name=name,
            enabled=True,
            settings={
                "api_base": "https://open.feishu.cn",
                "app_id": "cli_example",
                "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
            },
            targets=[],
        ),
        transport=None,
        clock=FakeClock(),
    )


def _copy(rendered: dict) -> str:
    visible = {
        key: value
        for key, value in rendered.items()
        if key != "notice" and not isinstance(value, (bytes, bytearray))
    }
    return json.dumps(visible, ensure_ascii=False)


class RegisteredChannelLanguageTest(unittest.TestCase):
    def test_every_channel_renders_zh_and_en(self):
        notice = _notice()
        self.assertGreaterEqual(len(REGISTRY), 3)
        for name, factory in REGISTRY.items():
            channel = _channel(name, factory)
            for language in ("zh", "en"):
                target = Target(
                    name,
                    "t",
                    "personal",
                    "424242",
                    {"language": language, "receive_id_type": "open_id", "url_env": "TIBO_WEBHOOK_EXAMPLE_URL"},
                )
                rendered = channel.render(notice, target)
                self.assertIsInstance(rendered, dict)
                blob = _copy(rendered)
                if language == "zh":
                    self.assertIn("预告", blob, name)
                    self.assertNotIn("View original post", blob, name)
                else:
                    self.assertIn("Preview", blob, name)
                    self.assertIn("And it's done. See you tomorrow.", blob, name)
                    self.assertNotIn("预告", blob, name)
                    self.assertNotIn("查看原帖", blob, name)
                    self.assertNotIn("搞定了", blob, name)
                    self.assertNotIn("原帖", blob, name)
            if name == "webhook":
                english = channel.render(
                    notice,
                    Target(name, "t", "group", "", {"language": "en", "url_env": "TIBO_WEBHOOK_EXAMPLE_URL"}),
                )
                self.assertEqual(english["language"], "en")
                self.assertEqual(english["notice"]["stage"], notice.stage)
                self.assertEqual(english["notice"]["url"], notice.url)
                self.assertEqual(english["notice"]["published_at"], notice.published_at)
                self.assertEqual(english["notice"]["text"], notice.text)
                self.assertEqual(english["text"].split("\n\n")[0], notice.original_text)
                self.assertIn("Quoting @thsottiaux: Lands by midnight PST.", english["text"])
                self.assertIn("Reset (form unspecified, defaults to quota reset)", english["tags"])
                self.assertIn("Preview", english["tags"])
                self.assertEqual(english["scope"], "Scope not specified in the post")

    def test_chinese_feishu_body_matches_the_existing_lines(self):
        notice = _notice()
        message = compose(notice, "zh", (2026, 10))
        self.assertEqual(message.card_body, "\n\n".join(notice_lines(notice)))
        self.assertEqual(message.title, notice.headline())
        self.assertEqual(message.link_label, "查看原帖")
        self.assertNotIn("View original post", message.card_body)


if __name__ == "__main__":
    unittest.main()
