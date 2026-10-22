"""The startup grace retries OS contention, never breaks or deletes a lock."""
import errno
import os
import unittest
from unittest.mock import patch

from app import server


@unittest.skipUnless(os.name == "nt", "Windows byte-lock behavior")
class PlaybackLockRetryTests(unittest.TestCase):
    def test_transient_denial_requires_a_later_real_os_grant(self):
        with patch("msvcrt.locking", side_effect=[PermissionError(errno.EACCES, "busy"), None]) as lock, \
                patch.object(server.time, "sleep") as sleep:
            server._acquire_windows_playback_lock(321)
        self.assertEqual(lock.call_count, 2)
        self.assertEqual(lock.call_args_list[0], lock.call_args_list[1])
        sleep.assert_called_once()

    def test_persistent_owner_is_rejected_within_bounded_grace(self):
        error = PermissionError(errno.EACCES, "busy")
        with patch("msvcrt.locking", side_effect=error) as lock, \
                patch.object(server.time, "monotonic", side_effect=[0., .1, .5]), \
                patch.object(server.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as caught:
                server._acquire_windows_playback_lock(321)
        self.assertIs(caught.exception, error)
        self.assertEqual(lock.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_unrelated_os_error_is_not_retried(self):
        error = OSError(errno.EBADF, "invalid descriptor")
        with patch("msvcrt.locking", side_effect=error) as lock, \
                patch.object(server.time, "sleep") as sleep:
            with self.assertRaises(OSError) as caught:
                server._acquire_windows_playback_lock(321)
        self.assertIs(caught.exception, error)
        lock.assert_called_once()
        sleep.assert_not_called()
