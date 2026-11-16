"""Real-session coverage for Codex's content-only hook-trust transition.

This is deliberately an opt-in host acceptance test.  It drives two real Codex
TUI sessions in ``tmux`` because ``codex exec`` does not expose an already-open
session's hook error text.  The test uses two released Driver payloads whose
hook bundles are byte-identical, so the transition changes plugin content while
leaving every hook declaration and entrypoint release-verified.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from test_codex_hook_cache_fixtures import link_target, reconciler

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEADING = (
    "## Scenario: an already-open session keeps running its hooks after a covered plugin's "
    "installed content changes but its hooks do not"
)
_TEST = (
    "tests.dev-tooling.test_codex_hook_trust_transition_real."
    "test_content_only_change_retrusts_before_a_fresh_session_runs_the_guard"
)
_REQUIRE_ENV = "LIVESPEC_REQUIRE_CODEX_HOOK_TRUST_TRANSITION"
_MARKETPLACE = "livespec-driver-codex"
_PLUGIN = "livespec"
_HOOK_FAILURE = "Hook failed"
_GUARD_DENIAL = "BLOCKED by livespec_footgun_guard.py"
_TRUST_PROMPT = re.compile(r"(?:trust|approve).*hook|hook.*(?:trust|approve)", re.IGNORECASE)


pytestmark = pytest.mark.real_only


def test_heading_coverage_binds_the_content_only_retrust_session_transition() -> None:
    """The scenario is owned by its real-session integration witness, not a TODO."""
    coverage = json.loads((_REPO_ROOT / "tests" / "heading-coverage.json").read_text("utf-8"))
    row = next(entry for entry in coverage if entry["heading"] == _HEADING)

    assert row["test"] == _TEST


def test_content_only_change_retrusts_before_a_fresh_session_runs_the_guard(
    tmp_path: Path,
) -> None:
    """A real open session reports hook failures while a fresh one is trusted.

    ``v<previous>`` is the hook path captured by the first TUI.  The test then
    installs the next released payload, whose hook bundle is byte-identical,
    removes the trust records, and invokes the ordinary all-plugin reconciler.
    It never supplies Codex's hook-trust bypass.
    """
    _require_real_host()
    codex = _codex_cli()
    host_home = _host_codex_home()
    version, previous = _released_content_only_pair(home=host_home)
    home, cache, project = _scratch_codex_home(
        tmp_path=tmp_path,
        host_home=host_home,
        previous=previous,
        current=version,
    )
    old_payload = cache / previous
    old_scripts, old_problems = reconciler.declared_hook_scripts(payload=old_payload)
    assert not old_problems
    captured_relatives = tuple(script.relative_to(old_payload) for script in old_scripts)

    initial_reports = reconciler.reconcile_all(home=home, only=(_MARKETPLACE, _PLUGIN))
    assert len(initial_reports) == 1
    assert initial_reports[0].ok, initial_reports[0].problems
    trust = _trust_module()
    assert trust.trust_status(
        config_path=home / "config.toml",
        marketplace=_MARKETPLACE,
        plugin=_PLUGIN,
        payload=old_payload,
    ).trusted

    open_session = _TuiSession.start(codex=codex, home=home, project=project, name="open")
    fresh_session: _TuiSession | None = None
    try:
        _probe_guard(session=open_session, marker=_GUARD_DENIAL)

        _replace_payload_with_current_release(
            cache=cache,
            previous=previous,
            current=version,
        )
        _set_marketplace_ref(config=home / "config.toml", ref="release")
        _withdraw_plugin_trust(config=home / "config.toml")
        current_payload = cache / version
        assert not trust.trust_status(
            config_path=home / "config.toml",
            marketplace=_MARKETPLACE,
            plugin=_PLUGIN,
            payload=current_payload,
        ).trusted

        release = _release_payload_module()
        hooks = _verify_hooks_against_release(home=home, payload=current_payload, release=release)
        reports = reconciler.reconcile_all(home=home, only=(_MARKETPLACE, _PLUGIN))
        assert len(reports) == 1
        assert reports[0].ok, reports[0].problems
        status = trust.trust_status(
            config_path=home / "config.toml",
            marketplace=_MARKETPLACE,
            plugin=_PLUGIN,
            payload=current_payload,
        )
        assert status.trusted
        _assert_trust_is_enabled(config=home / "config.toml", hooks=hooks)

        assert link_target(path=cache / previous) == "latest"
        assert link_target(path=cache / "latest") == version
        for relative in captured_relatives:
            retained = cache / previous / relative
            assert retained.is_file(), f"{retained} no longer resolves for the open session"
            assert retained.resolve() == (current_payload / relative).resolve()

        fresh_session = _TuiSession.start(codex=codex, home=home, project=project, name="fresh")
        fresh_output = _probe_guard(session=fresh_session, marker=_GUARD_DENIAL)
        assert _HOOK_FAILURE not in fresh_output
        assert _TRUST_PROMPT.search(fresh_output) is None

        open_session.send(_guard_prompt(label="after"))
        open_output = open_session.wait_for(marker=_HOOK_FAILURE)
        assert _HOOK_FAILURE in open_output, (
            "the already-open session silently skipped an untrusted hook instead of displaying "
            "its hook failure"
        )
    finally:
        if fresh_session is not None:
            fresh_session.close()
        open_session.close()


def _require_real_host() -> None:
    if os.environ.get(_REQUIRE_ENV) != "1":
        pytest.skip(f"set {_REQUIRE_ENV}=1 on a released Driver host to enforce this leg")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required to read an open Codex TUI's visible output")
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not found; a real Codex session cannot be exercised")
    if _codex_version() < (0, 166):
        pytest.skip("the content-hash hook-trust behavior requires codex-cli 0.166 or newer")


def _codex_cli() -> str:
    executable = shutil.which("codex")
    assert executable is not None
    return executable


def _codex_version() -> tuple[int, int]:
    result = subprocess.run(
        [_codex_cli(), "--version"], capture_output=True, check=True, text=True, timeout=30
    )
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    assert match is not None, result.stdout
    return int(match.group(1)), int(match.group(2))


def _host_codex_home() -> Path:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    if not (home / "auth.json").is_file():
        pytest.skip(f"{home / 'auth.json'} is absent; the real Codex TUI cannot authenticate")
    return home


def _released_content_only_pair(*, home: Path) -> tuple[str, str]:
    root = reconciler.cache_root(home=home)
    versions = reconciler.real_versions(root=root)
    if not versions:
        pytest.skip(f"{root} has no installed released Driver payload")
    current = versions[-1]
    tags = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=version:refname"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    try:
        current_index = tags.index(f"v{current}")
    except ValueError:
        pytest.skip(f"the installed Driver {current} has no matching release tag in this checkout")
    for tag in reversed(tags[:current_index]):
        previous = tag.removeprefix("v")
        if _same_hook_bundle(first=tag, second=f"v{current}"):
            return current, previous
    pytest.skip("no preceding released Driver payload has a byte-identical hook bundle")


def _same_hook_bundle(*, first: str, second: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", first, second, "--", "livespec/hooks"],
        cwd=_REPO_ROOT,
        timeout=30,
    )
    return result.returncode == 0


def _scratch_codex_home(
    *, tmp_path: Path, host_home: Path, previous: str, current: str
) -> tuple[Path, Path, Path]:
    home = tmp_path / "codex"
    home.mkdir()
    shutil.copy2(host_home / "auth.json", home / "auth.json")
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    (home / "config.toml").write_text(
        "\n".join(
            (
                f"[marketplaces.{_MARKETPLACE}]",
                'source_type = "git"',
                f"source = {json.dumps(origin)}",
                f"ref = {json.dumps(f'v{previous}')}",
                "",
                f'[plugins."{_PLUGIN}@{_MARKETPLACE}"]',
                "enabled = true",
                "",
            )
        ),
        encoding="utf-8",
    )
    cache = home / "plugins" / "cache" / _MARKETPLACE / _PLUGIN
    _extract_release(version=previous, destination=cache / previous)
    # The next released payload is added only after the old TUI is open.
    assert not (cache / current).exists()
    project = tmp_path / "project"
    subprocess.run(["git", "init", "--quiet", str(project)], check=True, timeout=30)
    return home, cache, project


def _extract_release(*, version: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", f"v{version}", "livespec"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(destination.parent)
    (destination.parent / "livespec").rename(destination)


def _replace_payload_with_current_release(*, cache: Path, previous: str, current: str) -> None:
    _extract_release(version=current, destination=cache / current)
    shutil.rmtree(cache / previous)


def _set_marketplace_ref(*, config: Path, ref: str) -> None:
    text = config.read_text(encoding="utf-8")
    updated = re.sub(r'^ref = ".*"\s*$', f"ref = {json.dumps(ref)}", text, flags=re.MULTILINE)
    assert updated != text
    config.write_text(updated, encoding="utf-8")


def _withdraw_plugin_trust(*, config: Path) -> None:
    prefix = f'[hooks.state."{_PLUGIN}@{_MARKETPLACE}:'
    retained: list[str] = []
    skipping = False
    for line in config.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("["):
            skipping = line.startswith(prefix)
        if not skipping:
            retained.append(line)
    config.write_text("".join(retained), encoding="utf-8")


def _trust_module() -> object:
    import codex_hook_trust

    return codex_hook_trust


def _release_payload_module() -> object:
    import _codex_hook_release_source

    return _codex_hook_release_source


def _verify_hooks_against_release(
    *, home: Path, payload: Path, release: object
) -> tuple[tuple[str, str], ...]:
    from codex_hook_release import verified_hook_keys

    version = json.loads((payload / ".codex-plugin" / "plugin.json").read_text("utf-8"))["version"]
    with release.release_payload(
        config_path=home / "config.toml",
        marketplace=_MARKETPLACE,
        plugin=_PLUGIN,
        version=version,
    ) as released:
        assert released.payload is not None, released.problem
        hooks, problems = verified_hook_keys(
            marketplace=_MARKETPLACE,
            plugin=_PLUGIN,
            payload=payload,
            release_payload=released.payload,
        )
    assert not problems
    assert hooks
    return hooks


def _assert_trust_is_enabled(*, config: Path, hooks: tuple[tuple[str, str], ...]) -> None:
    text = config.read_text(encoding="utf-8")
    assert "dangerously-bypass-hook-trust" not in text
    for key, _ in hooks:
        table = f"[hooks.state.{json.dumps(key)}]"
        assert table in text
    assert "enabled = false" not in text


def _guard_prompt(*, label: str) -> str:
    return (
        "Use the Bash tool exactly once to run this exact command, without changing it or "
        f"explaining first: git commit --allow-empty --no-verify -m hook-trust-{label}. "
        "Report the tool result verbatim."
    )


def _probe_guard(*, session: _TuiSession, marker: str) -> str:
    session.send(_guard_prompt(label="before"))
    return session.wait_for(marker=marker)


class _TuiSession:
    def __init__(self, *, socket: str, name: str) -> None:
        self.socket = socket
        self.name = name

    @classmethod
    def start(cls, *, codex: str, home: Path, project: Path, name: str) -> _TuiSession:
        socket = f"hook-trust-{os.getpid()}-{name}"
        command = " ".join(
            (
                f"CODEX_HOME={shlex.quote(str(home))}",
                shlex.quote(codex),
                "--no-alt-screen",
                "--ask-for-approval never",
                "--sandbox danger-full-access",
                f"--cd {shlex.quote(str(project))}",
            )
        )
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "new-session",
                "-d",
                "-x",
                "180",
                "-y",
                "50",
                "-s",
                name,
                command,
            ],
            check=True,
            timeout=30,
        )
        session = cls(socket=socket, name=name)
        session.wait_for(marker="Codex", timeout=90)
        return session

    def send(self, prompt: str) -> None:
        subprocess.run(
            ["tmux", "-L", self.socket, "send-keys", "-t", self.name, prompt, "Enter"],
            check=True,
            timeout=30,
        )

    def wait_for(self, *, marker: str, timeout: float = 180) -> str:
        deadline = time.monotonic() + timeout
        output = ""
        while time.monotonic() < deadline:
            output = self.capture()
            if marker in output:
                return output
            time.sleep(1)
        pytest.fail(f"timed out waiting for {marker!r} in open Codex session:\n{output}")

    def capture(self) -> str:
        return subprocess.run(
            ["tmux", "-L", self.socket, "capture-pane", "-p", "-J", "-t", self.name],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        ).stdout

    def close(self) -> None:
        subprocess.run(
            ["tmux", "-L", self.socket, "kill-session", "-t", self.name],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
