"""Core must not import channel implementations."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "tibo_reset_monitor" / "core"


class ArchitectureTest(unittest.TestCase):
    def test_core_does_not_import_channels(self):
        for path in CORE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "channels" in node.module.split("."):
                    self.fail(f"{path} imports {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "channels" in alias.name.split("."):
                            self.fail(f"{path} imports {alias.name}")

    def test_channels_do_not_import_each_other(self):
        channels = ROOT / "src" / "tibo_reset_monitor" / "channels"
        for path in channels.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if "channels" in parts and path.stem not in parts and parts[-1] not in {"base", path.stem}:
                        if parts[-1] in {"feishu", "telegram", "webhook"}:
                            self.fail(f"{path.name} imports {node.module}")


if __name__ == "__main__":
    unittest.main()
