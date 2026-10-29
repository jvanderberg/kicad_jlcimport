"""Crash-safe file writes."""

import errno
import os
import shutil
import time
import uuid
from typing import Union

# os.replace can fail transiently on Windows while a sync client, indexer or
# antivirus scanner has the target open without delete sharing.
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


def atomic_write(path: str, data: Union[str, bytes]) -> None:
    """Write *data* to *path*, replacing it atomically where possible.

    The data is written to a temporary file next to the target, flushed to
    disk, then renamed over the target, so a crash or full disk leaves the
    original intact.  Text is written as UTF-8 exactly as given (no newline
    translation).

    Symlinks are followed, so the file they point to is updated.  When an
    atomic replace would change more than the file's contents -- the file is
    hard-linked, owned by another user, has a group we cannot keep, or sits in
    a directory we cannot create files in -- the file is overwritten in place
    instead (see ``_write_in_place``).

    Raises PermissionError if the target exists but is not writable, exactly
    as ``open(path, "w")`` would, so read-only files stay protected.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    target = os.path.realpath(path)

    try:
        st = os.stat(target)
    except FileNotFoundError:
        st = None
    if st is not None and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, "Permission denied", path)
    if st is not None and not _can_replace(st):
        _write_in_place(target, data)
        return

    directory = os.path.dirname(target)
    tmp_path = os.path.join(directory, f".{os.path.basename(target)}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        # Mode 0o666 lets the process umask decide permissions for new files.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    except PermissionError:
        _write_in_place(target, data)
        return

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if st is not None and not _copy_metadata(st, tmp_path):
            os.unlink(tmp_path)
            _write_in_place(target, data)
            return
        if not _replace_with_retry(tmp_path, target):
            os.unlink(tmp_path)
            _write_in_place(target, data)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _can_replace(st: os.stat_result) -> bool:
    """Return False if replacing the file would break links or change its owner."""
    if st.st_nlink > 1:
        return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and st.st_uid != geteuid():
        return False
    return True


def _copy_metadata(st: os.stat_result, tmp_path: str) -> bool:
    """Give the temp file the original's group and permissions.

    Returns False if the original group cannot be kept.  Permission bits are
    best effort: some filesystems (FAT, CIFS) reject chmod.
    """
    if hasattr(os, "chown") and os.stat(tmp_path).st_gid != st.st_gid:
        try:
            os.chown(tmp_path, -1, st.st_gid)
        except OSError:
            return False
    try:
        os.chmod(tmp_path, st.st_mode & 0o7777)
    except OSError:
        pass
    return True


def _replace_with_retry(tmp_path: str, target: str) -> bool:
    """Rename *tmp_path* over *target*; return False if it stays locked."""
    for delay in _REPLACE_RETRY_DELAYS + (None,):
        try:
            os.replace(tmp_path, target)
            return True
        except PermissionError:
            if delay is None:
                return False
            time.sleep(delay)
    return False


def _write_in_place(target: str, data: bytes) -> None:
    """Overwrite *target* directly, for when an atomic replace isn't possible.

    Not crash-proof, but avoids the common ways a plain ``open(path, "w")``
    loses data: free space is checked before anything is touched, and the file
    is not truncated until the new contents have been written.
    """
    directory = os.path.dirname(target)
    if shutil.disk_usage(directory).free < len(data):
        raise OSError(errno.ENOSPC, "Not enough free space to write", target)
    if not os.path.exists(target):
        with open(target, "wb") as f:
            f.write(data)
        return
    with open(target, "r+b") as f:
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
