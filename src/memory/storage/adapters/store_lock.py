"""Kernel-managed leases for local storage and its sidecar journals."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
from stat import S_ISDIR
import time
from typing import BinaryIO, Iterator, Literal

from ...domain.exceptions import StorageBusyError, StorageError

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        """Win32 OVERLAPPED with the byte offset set to zero."""

        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file = _kernel32.LockFileEx
    _unlock_file = _kernel32.UnlockFileEx
    for _operation in (_lock_file, _unlock_file):
        _operation.restype = wintypes.BOOL
    _lock_file.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _unlock_file.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _LOCKFILE_FAIL_IMMEDIATELY = 1
    _LOCKFILE_EXCLUSIVE_LOCK = 2
    _ERROR_LOCK_VIOLATION = 33

    def _acquire(fd: int, *, shared: bool, blocking: bool) -> bool:
        """Take a shared or exclusive byte-range lock on a synchronous handle."""
        flags = 0 if shared else _LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        if _lock_file(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(_Overlapped())):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_LOCK_VIOLATION and not blocking:
            return False
        raise ctypes.WinError(error)

    def _release(fd: int) -> None:
        """Release the same range before closing the handle."""
        if not _unlock_file(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(_Overlapped())):
            raise ctypes.WinError(ctypes.get_last_error())

else:
    import fcntl

    def _acquire(fd: int, *, shared: bool, blocking: bool) -> bool:
        """Take a local flock on this open file description."""
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, operation)
        except OSError as exc:
            if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def _release(fd: int) -> None:
        """Drop the flock before closing the descriptor."""
        fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_path(target: Path) -> Path:
    """Keep the lock identity separate from replaceable graph and journal data."""
    return target.with_name(f"{target.name}.lock") / "access"


class StoreLease:
    """Hold a native lock on a stable resource until the lease exits."""

    def __init__(self, target_path: str | Path, *, timeout_seconds: float | None = None,
                 shared: bool = False, blocking: bool = False) -> None:
        if blocking and timeout_seconds is not None:
            raise ValueError("Choose either a blocking storage lease or an explicit timeout")
        self.target = Path(target_path).expanduser().resolve()
        self.path = _lock_path(self.target)
        self.shared = shared
        self._timeout_seconds = timeout_seconds
        self._blocking = blocking
        self._handle: BinaryIO | None = None

    def __enter__(self) -> StoreLease:
        self.acquire()
        return self

    def acquire(self) -> None:
        """Acquire once by default, or honor a caller's explicit wait policy."""
        if self._handle is not None:
            raise RuntimeError("A REQL storage lease cannot be entered twice")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = self.path.open("rb") if self.shared else self.path.open("r+b")
            except FileNotFoundError:
                handle = self.path.open("a+b")
            try:
                self._wait_for_lock(handle.fileno())
            except BaseException:
                handle.close()
                raise
        except OSError as exc:
            raise StorageError(f"Cannot acquire REQL storage lock for {self.target}: {exc}") from exc
        self._handle = handle

    def _wait_for_lock(self, fd: int) -> None:
        """Limit only explicitly timed callers to a deadline."""
        if self._blocking:
            _acquire(fd, shared=self.shared, blocking=True)
            return
        deadline = time.monotonic() + max(0.0, self._timeout_seconds or 0)
        delay = 0.001
        while not _acquire(fd, shared=self.shared, blocking=False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StorageBusyError(f"REQL storage is locked: {self.target}. "
                                       "Wait for the active command to finish and retry.")
            time.sleep(min(delay, remaining))
            delay = min(2 * delay, 0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def release(self) -> None:
        """Release and close the handle while keeping the lock file in place."""
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                with handle:
                    _release(handle.fileno())
            except OSError as exc:
                raise StorageError(f"Cannot release REQL storage lock for {self.target}: {exc}") from exc


@contextmanager
def exclusive_store_lock(target_path: str | Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Hold the store writer lock without opening or rewriting the graph."""
    with StoreLease(target_path, timeout_seconds=timeout_seconds):
        yield


def inspect_store_locks(target_path: str | Path) -> dict[str, str | bool | None]:
    """Probe current lock mode without creating files or opening graph data."""
    target = Path(target_path).expanduser().resolve()
    path = _lock_path(target)
    mode: Literal["read", "write"] | None = None
    try:
        # Windows can report a missing child when its parent is a file.
        if not S_ISDIR(path.parent.stat().st_mode):
            raise StorageError(f"Expected a REQL storage lock directory at {path.parent}")
        with path.open("r+b") as handle:
            fd = handle.fileno()
            if _acquire(fd, shared=False, blocking=False):
                _release(fd)
            elif _acquire(fd, shared=True, blocking=False):
                _release(fd)
                mode = "read"
            else:
                mode = "write"
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise StorageError(f"Cannot inspect REQL storage lock for {target}: {exc}") from exc
    return {"path": str(target), "locked": mode is not None, "mode": mode, "lock_path": str(path)}
