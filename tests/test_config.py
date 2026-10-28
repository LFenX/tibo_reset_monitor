import json
import unittest
from pathlib import Path

from tibo_reset_monitor.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _write(tmp, payload):
    path = tmp / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ConfigTest(unittest.TestCase):
    def test_example_config_loads(self):
        config = load_config(ROOT / "config.example.json", environ={})
        names = {channel.name: channel for channel in config.channels}
        self.assertIn("feishu", names)
        self.assertIn("telegram", names)
        feishu_kinds = {target.kind for target in names["feishu"].targets}
        telegram_kinds = {target.kind for target in names["telegram"].targets}
        self.assertEqual(feishu_kinds, {"group", "personal"})
        self.assertEqual(telegram_kinds, {"group", "personal"})
        self.assertEqual(config.poll_interval_seconds, 600)
        self.assertTrue(config.source_url.startswith("https://aihot.news/"))
        blob = (ROOT / "config.example.json").read_text(encoding="utf-8")
        self.assertNotIn("app_secret\"", blob)
        self.assertIn("TIBO_FEISHU_APP_SECRET", blob)
        self.assertIn("TIBO_TELEGRAM_BOT_TOKEN", blob)

    def test_rejects_secrets_bad_kinds_and_id_types(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ConfigError) as raised:
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "telegram": {
                                    "enabled": True,
                                    "bot_token": "123456:should-not-be-here",
                                    "targets": [],
                                }
                            }
                        },
                    )
                )
            self.assertIn("environment", str(raised.exception))

            with self.assertRaises(ConfigError) as raised:
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "feishu": {
                                    "enabled": True,
                                    "app_id": "cli_example",
                                    "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                                    "targets": [
                                        {
                                            "name": "ops",
                                            "kind": "group",
                                            "receive_id": "ou_person",
                                            "receive_id_type": "open_id",
                                        }
                                    ],
                                }
                            }
                        },
                    )
                )
            self.assertIn("chat_id", str(raised.exception))

            with self.assertRaises(ConfigError) as raised:
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "telegram": {
                                    "enabled": True,
                                    "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                    "targets": [
                                        {"name": "me", "kind": "personal", "chat_id": "-100123"},
                                        {"name": "ops", "kind": "group", "chat_id": "424242"},
                                    ],
                                }
                            }
                        },
                    )
                )
            text = str(raised.exception)
            self.assertIn("positive numeric", text)
            self.assertIn("negative", text)

    def test_mixed_targets_are_valid(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(
                _write(
                    Path(tmp),
                    {
                        "channels": {
                            "feishu": {
                                "enabled": True,
                                "app_id": "cli_example_not_a_real_app_id",
                                "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                                "targets": [
                                    {"name": "ops", "kind": "group", "receive_id": "oc_example", "receive_id_type": "chat_id"},
                                    {"name": "me", "kind": "personal", "receive_id": "person@example.com", "receive_id_type": "email"},
                                ],
                            },
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "targets": [
                                    {"name": "ops", "kind": "group", "chat_id": "@example_channel"},
                                    {"name": "me", "kind": "personal", "chat_id": "424242"},
                                ],
                            },
                        }
                    },
                ),
                environ={"TIBO_STATE_PATH": str(Path(tmp) / "state.sqlite3")},
            )
        self.assertEqual(config.state_path, str(Path(tmp) / "state.sqlite3"))
        feishu = next(channel for channel in config.channels if channel.name == "feishu")
        self.assertEqual([target.kind for target in feishu.targets], ["group", "personal"])
        self.assertEqual(feishu.targets[1].options["receive_id_type"], "email")
        self.assertEqual(config.language, "zh")
        self.assertTrue(all(target.options["language"] == "zh" for channel in config.channels for target in channel.targets))

    def test_language_cascades_and_rejects_unknown_values(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                _write(
                    root,
                    {
                        "language": "en",
                        "channels": {
                            "feishu": {
                                "enabled": True,
                                "language": "zh",
                                "app_id": "cli_example",
                                "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                                "targets": [
                                    {"name": "ops", "kind": "group", "receive_id": "oc_example", "receive_id_type": "chat_id"},
                                    {"name": "me", "kind": "personal", "receive_id": "ou_example", "receive_id_type": "open_id", "language": "en"},
                                ],
                            },
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "targets": [
                                    {"name": "ops", "kind": "group", "chat_id": "-100123"},
                                    {"name": "me", "kind": "personal", "chat_id": "424242", "language": "zh"},
                                ],
                            },
                            "webhook": {
                                "enabled": False,
                                "targets": [{"name": "hook", "kind": "group", "url_env": "TIBO_WEBHOOK_EXAMPLE_URL"}],
                            },
                        },
                    },
                )
            )
            by_name = {channel.name: channel for channel in config.channels}
            self.assertEqual(config.language, "en")
            self.assertEqual(by_name["feishu"].settings["language"], "zh")
            self.assertEqual(by_name["feishu"].targets[0].options["language"], "zh")
            self.assertEqual(by_name["feishu"].targets[1].options["language"], "en")
            self.assertEqual(by_name["telegram"].settings["language"], "en")
            self.assertEqual(by_name["telegram"].targets[0].options["language"], "en")
            self.assertEqual(by_name["telegram"].targets[1].options["language"], "zh")
            self.assertEqual(by_name["webhook"].targets[0].options["language"], "en")

            with self.assertRaises(ConfigError) as raised:
                load_config(_write(root, {"language": "jp", "channels": {}}))
            self.assertIn('$.language must be "zh" or "en"', str(raised.exception))

            with self.assertRaises(ConfigError) as raised:
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "webhook": {"enabled": False, "language": "cn", "targets": []},
                                "telegram": {
                                    "enabled": True,
                                    "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                    "targets": [{"name": "me", "kind": "personal", "chat_id": "424242", "language": "english"}],
                                },
                            }
                        },
                    )
                )
            text = str(raised.exception)
            self.assertIn('$.channels.webhook.language must be "zh" or "en"', text)
            self.assertIn('$.channels.telegram.targets[0].language must be "zh" or "en"', text)

    def test_timezone_cascades_and_rejects_unknown_names(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(
                _write(
                    root,
                    {
                        "timezone": "America/Los_Angeles",
                        "channels": {
                            "feishu": {
                                "enabled": True,
                                "timezone": "Asia/Shanghai",
                                "app_id": "cli_example",
                                "app_secret_env": "TIBO_FEISHU_APP_SECRET",
                                "targets": [
                                    {"name": "ops", "kind": "group", "receive_id": "oc_example", "receive_id_type": "chat_id"},
                                    {
                                        "name": "me",
                                        "kind": "personal",
                                        "receive_id": "ou_example",
                                        "receive_id_type": "open_id",
                                        "timezone": "Asia/Kolkata",
                                    },
                                ],
                            },
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "targets": [{"name": "ops", "kind": "group", "chat_id": "-100123"}],
                            },
                        },
                    },
                )
            )
            by_name = {channel.name: channel for channel in config.channels}
            self.assertEqual(config.timezone, "America/Los_Angeles")
            self.assertFalse(config.timezone_fallback)
            self.assertEqual(by_name["feishu"].targets[0].options["timezone"], "Asia/Shanghai")
            self.assertEqual(by_name["feishu"].targets[1].options["timezone"], "Asia/Kolkata")
            self.assertEqual(by_name["telegram"].targets[0].options["timezone"], "America/Los_Angeles")

            with self.assertRaises(ConfigError) as raised:
                load_config(_write(root, {"timezone": "Not/AZone", "channels": {}}))
            self.assertIn("$.timezone must be an IANA time zone name like Asia/Shanghai", str(raised.exception))
            self.assertIn("Not/AZone", str(raised.exception))

            with self.assertRaises(ConfigError):
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "webhook": {
                                    "enabled": False,
                                    "timezone": "Beijing",
                                    "targets": [],
                                }
                            }
                        },
                    )
                )

    def test_webhook_secret_env_must_be_an_environment_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ConfigError) as raised:
                load_config(
                    _write(
                        root,
                        {
                            "channels": {
                                "webhook": {
                                    "enabled": False,
                                    "targets": [
                                        {
                                            "name": "hook",
                                            "kind": "group",
                                            "url_env": "TIBO_WEBHOOK_EXAMPLE_URL",
                                            "secret_env": "not-a-name",
                                        }
                                    ],
                                }
                            }
                        },
                    )
                )
            self.assertIn("$.channels.webhook.targets[0].secret_env", str(raised.exception))
            config = load_config(
                _write(
                    root,
                    {
                        "channels": {
                            "webhook": {
                                "enabled": False,
                                "targets": [
                                    {
                                        "name": "hook",
                                        "kind": "group",
                                        "url_env": "TIBO_WEBHOOK_EXAMPLE_URL",
                                        "secret_env": "TIBO_WEBHOOK_EXAMPLE_SECRET",
                                    }
                                ],
                            }
                        }
                    },
                )
            )
            webhook = next(channel for channel in config.channels if channel.name == "webhook")
            self.assertEqual(webhook.targets[0].options["secret_env"], "TIBO_WEBHOOK_EXAMPLE_SECRET")


if __name__ == "__main__":
    unittest.main()
