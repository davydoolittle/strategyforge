"""
Test temp files in RAM. Importing this points tempfile at /dev/shm/sf-tests.

Why: in the claude-dev container /tmp sits on the Docker image's loop device,
where a SQLite commit takes ~0.9 s (6 ms on the share). Tests that build a
database file spent seconds per commit, and test_jobs' two-writer test came
within a few seconds of the child job's 5 s lock timeout: one disk stall and
it failed ("FAILED (failures=1)", 2026-11-02).

Falls back to the normal temp dir when /dev/shm is missing, not writable or
has less than MIN_FREE free (Docker's default /dev/shm is 64 MB). SF_TEST_TMP
overrides the location. The regression harness copies the 110 MB live DB, so
it does not use this; it keeps SF_SCRATCH_DIR or SYSTEM_TMP.

Every test module that creates temp files imports this, so the setting does
not depend on which module unittest happens to import first.
"""

import os
import tempfile

SHM = "/dev/shm"
MIN_FREE = 32 * 1024 * 1024
SYSTEM_TMP = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"


def _pick():
    want = os.environ.get("SF_TEST_TMP")
    if want:
        os.makedirs(want, exist_ok=True)
        return want
    try:
        st = os.statvfs(SHM)
        if st.f_bavail * st.f_frsize < MIN_FREE or not os.access(SHM, os.W_OK):
            return None
        d = os.path.join(SHM, "sf-tests")
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


TMP = _pick()
if TMP:
    tempfile.tempdir = TMP
