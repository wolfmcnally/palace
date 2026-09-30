"""Tests for the Phase 1.3 LaunchAgent lifecycle.

The plist file itself is validated as-is (with templated ``{{...}}`` tokens)
and after substitution. The install/uninstall pipeline is exercised against
the hermetic ``lifecycle_env`` fixture so nothing touches the host's real
per-user LaunchAgents directory or the live ``gui/<uid>`` launchd domain —
the ``run_bootstrap=False`` / ``run_bootout=False`` switches gate the side
effects. The status helper is exercised end-to-end via ``subprocess.run``
with a stubbed ``launchctl`` on PATH.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from palace.daemons.capture import lifecycle
from palace.daemons.capture.lifecycle import (
    PLIST_FILENAME,
    PLIST_LABEL,
    PLIST_TEMPLATE_PATH,
)
from tests.daemons.conftest import LifecycleEnv

EXPECTED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "KeepAlive",
        "ThrottleInterval",
        "ProcessType",
        "StandardErrorPath",
        "StandardOutPath",
        "EnvironmentVariables",
        "WorkingDirectory",
        "LimitLoadToSessionType",
    }
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
STATUS_HELPER: Path = REPO_ROOT / "bin" / "palace-capture-status"


def _render_with_stub_paths() -> dict[str, object]:
    """Render the committed template with absolute stub paths and parse."""
    template = (REPO_ROOT / PLIST_TEMPLATE_PATH).read_text(encoding="utf-8")
    rendered = lifecycle.render_plist(
        template,
        uv_bin=Path("/usr/bin/true"),
        home=Path("/tmp/home"),
        repo=Path("/tmp/repo"),
    )
    parsed: dict[str, object] = plistlib.loads(rendered.encode("utf-8"))
    return parsed


def test_plist_is_valid_property_list() -> None:
    parsed = _render_with_stub_paths()
    assert frozenset(parsed.keys()) == EXPECTED_TOP_LEVEL_KEYS


def test_plist_label_matches_filename() -> None:
    parsed = _render_with_stub_paths()
    assert parsed["Label"] == PLIST_LABEL
    assert PLIST_TEMPLATE_PATH.stem == PLIST_LABEL
    assert PLIST_TEMPLATE_PATH.name == PLIST_FILENAME


def test_plist_program_arguments_shape() -> None:
    parsed = _render_with_stub_paths()
    args = parsed["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0] == "/usr/bin/true"
    # The tail after the uv binary must match palace's serve invocation exactly.
    assert args[1:10] == [
        "run",
        "palace",
        "capture",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--store",
    ]
    assert args[10] == "/tmp/home/palace-data.noindex"


def test_install_substitutes_placeholders(lifecycle_env: LifecycleEnv) -> None:
    rc = lifecycle.install(
        home=lifecycle_env.home,
        repo_root=lifecycle_env.repo_root,
        launch_agents_dir=lifecycle_env.launch_agents_dir,
        uv_bin=lifecycle_env.stub_uv,
        run_bootstrap=False,
    )
    assert rc == 0

    installed_path = lifecycle_env.launch_agents_dir / PLIST_FILENAME
    assert installed_path.is_file()

    text = installed_path.read_text(encoding="utf-8")
    assert "{{" not in text, "no placeholders should remain after install"

    parsed = plistlib.loads(text.encode("utf-8"))
    assert frozenset(parsed.keys()) == EXPECTED_TOP_LEVEL_KEYS
    assert parsed["ProgramArguments"][0] == str(lifecycle_env.stub_uv)
    assert parsed["ProgramArguments"][-1] == str(lifecycle_env.home / "palace-data.noindex")
    assert parsed["WorkingDirectory"] == str(lifecycle_env.repo_root)
    assert parsed["EnvironmentVariables"]["HOME"] == str(lifecycle_env.home)
    assert parsed["EnvironmentVariables"]["TZ"] == "America/Boise"

    # The install must create the log directory so the daemon's first launchd
    # spawn does not fail on a missing StandardErrorPath parent.
    assert (lifecycle_env.home / "palace-data.noindex" / "logs").is_dir()


def test_uninstall_is_idempotent(
    lifecycle_env: LifecycleEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = lifecycle.uninstall(
        home=lifecycle_env.home,
        launch_agents_dir=lifecycle_env.launch_agents_dir,
        run_bootout=False,
    )
    assert rc == 0

    captured = capsys.readouterr()
    assert "already absent" in captured.err
    assert "log history preserved" in captured.out
    assert "palace capture uninstalled." in captured.out


def test_status_helper_handles_missing_state(lifecycle_env: LifecycleEnv, tmp_path: Path) -> None:
    # Stub launchctl: a no-op script on PATH so the helper does not hit the
    # host's real launchctl. The script exits 0 with empty stdout, which
    # mirrors "no service loaded under this label".
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    fake_launchctl = stubs / "launchctl"
    fake_launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_launchctl.chmod(fake_launchctl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Provide a real /usr/bin/curl etc. via the parent PATH; the helper's
    # curl invocation will fail (loopback unreachable in the hermetic env)
    # which lands the "(unreachable)" branch.
    bash_path = shutil.which("bash")
    assert bash_path is not None, "bash must be available to run the status helper"

    env = {
        "HOME": str(lifecycle_env.home),
        "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
        # Force the /health probe at a reserved unreachable loopback port so
        # the assertion lands the "(unreachable)" branch even when the host
        # has a real palace daemon running on the default port.
        "PALACE_CAPTURE_URL": "http://127.0.0.1:1/capture",
    }

    result = subprocess.run(
        [bash_path, str(STATUS_HELPER)],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"status helper exit={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert b"(no running service)" in result.stdout
    assert b"(no log yet)" in result.stdout
    assert b"(unreachable)" in result.stdout
