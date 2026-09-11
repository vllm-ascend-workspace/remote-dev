import hashlib
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest import mock

from remote_dev.core.file_ops import REMOTE_FILE_PY
from test_property_support import run_remote_script


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "log.txt"

    def read(self, **options):
        return run_remote_script(REMOTE_FILE_PY, {"op": "read", "root": str(self.path.parent),
            "file_path": str(self.path), "offset": 1, "limit": 1, **options})["file"]

    def test_fast_window_stops_before_the_rest_of_a_large_log(self):
        with self.path.open("wb") as stream:
            stream.write(b"first\nsecond\n" + b"x" * (16 * 1024 * 1024))
        real_open = Path.open
        sizes = []
        class Counting:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def read(self, size):
                value = self.stream.read(size); sizes.append(len(value)); return value
        def opened(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return Counting(stream) if path == self.path and args == ("rb",) else stream
        with mock.patch.object(Path, "open", opened):
            row = self.read(verify_content=False)
        self.assertEqual(row["content"], "1 | first")
        self.assertIsNone(row["sha256"])
        self.assertIsNone(row["total_lines"])
        self.assertLess(sum(sizes), 128 * 1024)

    def test_hash_scan_bounds_memory_for_one_giant_line(self):
        with self.path.open("wb") as stream:
            for _ in range(32): stream.write(b"x" * (1024 * 1024))
        tracemalloc.start()
        try:
            row = self.read(max_line_chars=20)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(row["total_lines"], 1)
        self.assertEqual(row["truncated_line_count"], 1)
        self.assertLess(peak, 8 * 1024 * 1024)
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        self.assertEqual(row["sha256"], digest.hexdigest())

    def test_crlf_and_unicode_across_chunk_boundaries(self):
        data = b"x" * 65535 + b"\r\n" + "你好\u2028last".encode()
        self.path.write_bytes(data)
        row = self.read(limit=5, max_line_chars=10)
        self.assertEqual(row["total_lines"], 3)
        self.assertIn("2 | 你好\n3 | last", row["content"])
        self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
