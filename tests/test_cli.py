import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tibo_reset_monitor.cli import main


class CliTest(unittest.TestCase):
    def test_preview_escapes_telegram_html_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "state": {"path": str(Path(tmp) / "state.sqlite3")},
                        "channels": {
                            "telegram": {
                                "enabled": True,
                                "bot_token_env": "TIBO_TELEGRAM_BOT_TOKEN",
                                "targets": [{"name": "me", "kind": "personal", "chat_id": "424242"}],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "--config",
                        str(path),
                        "manual",
                        "--channel",
                        "telegram",
                        "--target",
                        "me",
                        "--text",
                        "<b>a & b</b>",
                        "--preview",
                    ]
                )
        self.assertEqual(code, 0)
        rendered = stdout.getvalue()
        self.assertIn("&lt;b&gt;a &amp; b&lt;/b&gt;", rendered)
        self.assertIn('"parse_mode": "HTML"', rendered)
        self.assertIn("424242", rendered)
        self.assertNotIn("<b>a & b</b>", rendered)

    def test_missing_config_exits_2(self):
        code = main(["--config", "does-not-exist.json", "status"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
