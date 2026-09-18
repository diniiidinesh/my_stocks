from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO


def acquire_single_instance(state_dir: Path, name: str = "watch.lock") -> IO[str]:
    """Acquire an exclusive lock so only one `watch` process runs at a time.

    `state_dir` must be the shared bind-mounted state directory (host and
    Docker container both resolve to the same path on disk) — that is what
    makes the lock effective across runtimes, not just within one process
    tree. Raises SystemExit(1) if another process already holds it; the
    caller should not catch this.

    The returned handle must be kept referenced (and ideally closed
    explicitly on shutdown) for the process lifetime — closing it, or the
    process exiting, releases the lock.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / name
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(
            f"another nse-alert watch already holds {lock_path} — refusing to "
            "start a second watcher (two watchers on the same Telegram bot "
            "token silently break order confirmation; see docs/RCA-2026-09-17.md)"
        )
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def release_single_instance(fh: IO[str]) -> None:
    """Release a lock acquired by `acquire_single_instance`. Safe to call twice."""
    if fh.closed:
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
