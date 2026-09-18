from __future__ import annotations

from pathlib import Path

import pytest

from nse_alert.lock import acquire_single_instance, release_single_instance


def test_second_acquire_fails_while_first_holds_lock(tmp_path: Path) -> None:
    first = acquire_single_instance(tmp_path)
    try:
        with pytest.raises(SystemExit, match="another nse-alert watch already holds"):
            acquire_single_instance(tmp_path)
    finally:
        release_single_instance(first)


def test_first_process_undisturbed_by_failed_second_attempt(tmp_path: Path) -> None:
    first = acquire_single_instance(tmp_path)
    try:
        with pytest.raises(SystemExit):
            acquire_single_instance(tmp_path)
        assert not first.closed
        first.flush()  # still a live, writable handle
    finally:
        release_single_instance(first)


def test_release_allows_reacquire(tmp_path: Path) -> None:
    first = acquire_single_instance(tmp_path)
    release_single_instance(first)
    assert first.closed

    second = acquire_single_instance(tmp_path)
    try:
        pass
    finally:
        release_single_instance(second)


def test_release_is_idempotent(tmp_path: Path) -> None:
    fh = acquire_single_instance(tmp_path)
    release_single_instance(fh)
    release_single_instance(fh)  # must not raise


def test_lock_file_written_under_state_dir(tmp_path: Path) -> None:
    fh = acquire_single_instance(tmp_path)
    try:
        assert (tmp_path / "watch.lock").exists()
    finally:
        release_single_instance(fh)


def test_creates_state_dir_if_missing(tmp_path: Path) -> None:
    state_dir = tmp_path / "nested" / "state"
    fh = acquire_single_instance(state_dir)
    try:
        assert state_dir.is_dir()
    finally:
        release_single_instance(fh)
