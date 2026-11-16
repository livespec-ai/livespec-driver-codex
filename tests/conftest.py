"""Shared fixtures."""

from __future__ import annotations

import os
import shutil
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _tmp_path_stays_removable(tmp_path: Path) -> Iterator[None]:
    """Give a passing test's `tmp_path` tree back its write permission before pytest removes it.

    pytest removes a passing test's `tmp_path` directory under the `failed` retention
    policy with `ignore_errors=True`, so a tree a fixture left non-writable (a git
    repository's 0444 objects under 0555 directories) is silently NOT removed and its
    files stay in /tmp until the host runs out of inodes (livespec-dev-tooling-jxhi63,
    2026-11-16). This fixture requests `tmp_path`, so it tears down first and restores
    the owner write bit on every directory in the tree.
    """
    yield
    for directory, _subdirectories, _files in os.walk(tmp_path):
        try:
            # os.* on purpose: some tests monkeypatch Path.stat while this teardown runs.
            os.chmod(directory, os.stat(directory).st_mode | 0o700)  # noqa: PTH101, PTH116
        except OSError:
            continue


_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Remember each test's call report so a fixture can tell at teardown whether it passed."""
    report = yield
    if report.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


def _make_removable(*, root: Path) -> None:
    """Restore the owner write bit on every directory under `root` so it can be removed."""
    for directory, _subdirectories, _files in os.walk(root):
        try:
            # os.* on purpose: some tests monkeypatch Path.stat while this teardown runs.
            os.chmod(directory, os.stat(directory).st_mode | 0o700)  # noqa: PTH101, PTH116
        except OSError:
            continue


@pytest.fixture(autouse=True)
def _factory_dirs_removed(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Remove the `tmp_path_factory.mktemp` directories a PASSING test made.

    pytest's `tmp_path_retention_policy` prunes only the `tmp_path` fixture's own
    directory, so every directory a fixture makes through `mktemp` (seven per test in
    this file) stayed in /tmp and, at about 105k files per suite run, exhausted a seat
    host's tmpfs inodes (livespec-dev-tooling-jxhi63, 2026-11-16). The directories a
    test makes are recorded here and, when its call phase passed, made writable and
    removed; a failing test keeps them for diagnosis, as the `failed` policy keeps its
    `tmp_path`. Higher-scoped fixtures are set up before this function-scoped one, so
    a session or module directory is never recorded.
    """
    made: list[Path] = []
    original = tmp_path_factory.mktemp
    remove_tree = shutil.rmtree  # bound before the test can monkeypatch shutil

    def recording(basename: str, *, numbered: bool = True) -> Path:
        path = original(basename, numbered=numbered)
        made.append(path)
        return path

    monkeypatch.setattr(tmp_path_factory, "mktemp", recording)
    yield
    report = request.node.stash.get(_CALL_REPORT, None)
    if report is not None and report.passed:
        for path in made:

            def sweep(target: Path = path) -> None:
                _make_removable(root=target)
                remove_tree(target, ignore_errors=True)

            # Removal waits for session cleanup: a background thread a test started can
            # still be writing into its directory when the test itself has ended.
            request.config.add_cleanup(sweep)
