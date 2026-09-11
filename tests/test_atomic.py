import unittest
from unittest import mock
from remote_dev.core import atomic


class AtomicReplaceTests(unittest.TestCase):
    def test_transient_windows_lock_retries_only_the_prepared_replace(self):
        error = PermissionError("sharing denied")
        error.winerror = 32
        with mock.patch.object(atomic.os, "replace", side_effect=[error, None]) as replace, \
                mock.patch.object(atomic.time, "sleep"):
            atomic.replace_file("prepared", "target")
        self.assertEqual(replace.call_args_list, [mock.call("prepared", "target")] * 2)

    def test_other_permission_errors_are_not_retried(self):
        with mock.patch.object(atomic.os, "replace", side_effect=PermissionError("denied")) as replace:
            with self.assertRaises(PermissionError):
                atomic.replace_file("prepared", "target")
        self.assertEqual(replace.call_count, 1)

    def test_windows_retry_deadline_does_not_delete_the_target(self):
        error = PermissionError("sharing denied")
        error.winerror = 5
        with mock.patch.object(atomic.os, "replace", side_effect=error), \
                mock.patch.object(atomic.time, "monotonic", side_effect=[0, 1]), \
                mock.patch.object(atomic.os, "unlink") as unlink:
            with self.assertRaises(PermissionError):
                atomic.replace_file("prepared", "target")
            unlink.assert_not_called()
