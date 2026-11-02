"""Tests for fast_tmp: RAM temp dir when /dev/shm has room, normal temp dir otherwise."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fast_tmp  # noqa: E402


class FakeStat:
    def __init__(self, free):
        self.f_bavail, self.f_frsize = free, 1


class TestPick(unittest.TestCase):
    def pick(self, free=None, env=None, raises=False):
        env = env or {}
        stat = mock.Mock(side_effect=OSError) if raises else mock.Mock(return_value=FakeStat(free))
        with mock.patch.dict(os.environ, env), mock.patch("os.statvfs", stat), \
                mock.patch("os.access", return_value=True), mock.patch("os.makedirs"):
            if "SF_TEST_TMP" not in env:
                os.environ.pop("SF_TEST_TMP", None)
            return fast_tmp._pick()

    def test_shm_with_room(self):
        self.assertEqual(self.pick(free=fast_tmp.MIN_FREE), "/dev/shm/sf-tests")

    def test_falls_back_when_full_or_missing(self):
        self.assertIsNone(self.pick(free=fast_tmp.MIN_FREE - 1))
        self.assertIsNone(self.pick(raises=True))

    def test_env_override(self):
        self.assertEqual(self.pick(free=0, env={"SF_TEST_TMP": "/somewhere"}), "/somewhere")

    def test_active_in_this_process(self):
        if fast_tmp.TMP:
            self.assertEqual(tempfile.gettempdir(), fast_tmp.TMP)
        self.assertNotEqual(fast_tmp.SYSTEM_TMP, "/dev/shm/sf-tests")


if __name__ == "__main__":
    unittest.main()
