from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class RequiredModulesTests(unittest.TestCase):
    def test_design_named_core_modules_import(self) -> None:
        for module in (
            "core.endpoint",
            "core.ssh_transport",
            "core.path_policy",
            "core.state_store",
            "core.result",
            "core.preview",
            "core.read_ledger",
            "core.file_ops",
            "core.shell_ops",
            "core.search_ops",
            "core.patch_ops",
            "core.job_ops",
            "core.monitor_ops",
            "core.artifact_ops",
            "core.context_snapshot",
            "core.permissions",
            "core.errors",
        ):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))


if __name__ == "__main__":
    unittest.main()
