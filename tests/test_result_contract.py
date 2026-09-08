from __future__ import annotations

import json
import unittest

from remote_dev.result import RESULT_SCHEMA_VERSION, make_result, result_schema_path


class ResultContractTests(unittest.TestCase):
    def test_make_result_uses_canonical_schema(self) -> None:
        result = make_result(
            tool="remote.bash",
            target={"kind": "direct-endpoint", "endpoint_id": "abc"},
            outcome="success",
            status="ok",
            summary="done",
        )
        self.assertEqual(result["schema_version"], RESULT_SCHEMA_VERSION)
        self.assertEqual(result["schema_version"], "remote-dev.result.v1")
        self.assertEqual(result["tool"], "remote.bash")
        self.assertEqual(result["outcome"], "success")
        self.assertIn("invocation_id", result)
        self.assertEqual(result["changed_files"], [])

    def test_result_schema_is_packaged(self) -> None:
        schema = json.loads(result_schema_path().read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], RESULT_SCHEMA_VERSION)
        self.assertIn("schema_version", schema["required"])
        self.assertEqual(schema["properties"]["schema_version"]["const"], RESULT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
