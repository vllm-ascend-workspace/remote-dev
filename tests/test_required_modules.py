from __future__ import annotations

import importlib
import unittest


class RequiredModulesTests(unittest.TestCase):
    def test_design_named_core_modules_import(self) -> None:
        for module in (
            "remote_dev.result",
            "remote_dev.core.endpoint",
            "remote_dev.core.ssh_transport",
            "remote_dev.core.path_policy",
            "remote_dev.core.state_store",
            "remote_dev.core.preview",
            "remote_dev.core.read_ledger",
            "remote_dev.core.file_ops",
            "remote_dev.core.shell_ops",
            "remote_dev.core.search_ops",
            "remote_dev.core.patch_ops",
            "remote_dev.core.job_ops",
            "remote_dev.processes",
            "remote_dev.processes.client",
            "remote_dev.core.rpc_transport",
            "remote_dev.core.artifact_ops",
            "remote_dev.core.context_snapshot",
            "remote_dev.core.permissions",
            "remote_dev.core.errors",
        ):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))


if __name__ == "__main__":
    unittest.main()
