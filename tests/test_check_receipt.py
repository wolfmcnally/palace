"""Behavioral coverage for durable, candidate-bound full-gate receipts."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_SOURCE = REPO_ROOT / "bin" / "check-receipt"
TREE_SOURCE = REPO_ROOT / "bin" / "kickoff-tree-id"
HOOK_SOURCE = REPO_ROOT / ".githooks" / "pre-push"
ZERO_SHA = "0" * 40


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    (root / ".githooks").mkdir()
    (root / "lib" / "agentic_starter").mkdir(parents=True)
    shutil.copy2(SCRIPT_SOURCE, root / "bin" / "check-receipt")
    shutil.copy2(TREE_SOURCE, root / "bin" / "kickoff-tree-id")
    shutil.copy2(HOOK_SOURCE, root / ".githooks" / "pre-push")
    shutil.copy2(
        REPO_ROOT / "lib" / "agentic_starter" / "__init__.py",
        root / "lib" / "agentic_starter" / "__init__.py",
    )
    shutil.copy2(
        REPO_ROOT / "lib" / "agentic_starter" / "candidate_boundaries.py",
        root / "lib" / "agentic_starter" / "candidate_boundaries.py",
    )
    write_executable(
        root / "bin" / "python",
        "\n".join(
            (
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'if [[ "${CHECK_RECEIPT_TEST_RUNTIME_MODE:-}" == "fail" ]]; then',
                "  echo 'selected runtime probe failed' >&2",
                "  exit 23",
                "fi",
                'if [[ "${CHECK_RECEIPT_TEST_RUNTIME_MODE:-}" == "malformed" ]]; then',
                "  printf '%s\\n' 'not-json'",
                "  exit 0",
                "fi",
                f"selected={shlex.quote(sys.executable)}",
                'if [[ -n "${CHECK_RECEIPT_TEST_MANAGED_PYTHON:-}" ]]; then',
                '  selected="$CHECK_RECEIPT_TEST_MANAGED_PYTHON"',
                "fi",
                'if [[ -n "${TOOLCHAIN_PYTHON:-}" ]]; then',
                '  selected="$TOOLCHAIN_PYTHON"',
                "fi",
                'if [[ -n "${CHECK_RECEIPT_TEST_PYTHON_VERSION:-}" ]]; then',
                '  export PYTHONPATH="$PWD/.kickoff/runtime-site"',
                "fi",
                'exec "$selected" "$@"',
                "",
            )
        ),
    )
    write_executable(
        root / "bin" / "check",
        "#!/usr/bin/env bash\n"
        'mkdir -p "$PWD/.kickoff"\n'
        "printf 'called\\n' >> \"$PWD/.kickoff/hook-called\"\n",
    )
    (root / ".gitignore").write_text(".kickoff/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    (root / "LOG.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True)
    site = root / ".kickoff" / "runtime-site"
    site.mkdir(parents=True)
    (site / "sitecustomize.py").write_text(
        "import os\n"
        "import platform\n"
        "import sys\n"
        "if version := os.environ.get('CHECK_RECEIPT_TEST_PYTHON_VERSION'):\n"
        "    platform.python_version = lambda: version\n"
        "if identity := os.environ.get('CHECK_RECEIPT_TEST_RUNTIME_IDENTITY'):\n"
        "    sys.executable = identity\n"
        "    sys._base_executable = identity\n",
        encoding="utf-8",
    )
    return root


def run_receipt(
    root: Path,
    *arguments: str,
    stdin: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "bin" / "check-receipt"), *arguments],
        cwd=root,
        env=environment,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def candidate(root: Path) -> str:
    result = run_receipt(root, "candidate", "--root", str(root))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def push_input(root: Path, local_sha: str | None = None) -> str:
    selected = local_sha or head(root)
    return f"refs/heads/master {selected} refs/heads/master {ZERO_SHA}\n"


def record_pass(
    root: Path,
    *,
    output: str = "gate output\n",
    environment: dict[str, str] | None = None,
    gate: str = "all",
) -> tuple[Path, Path]:
    identity = candidate(root)
    begun = run_receipt(
        root,
        "begin",
        "--root",
        str(root),
        "--gate",
        gate,
        "--candidate",
        identity,
        environment=environment,
    )
    assert begun.returncode == 0, begun.stderr
    log = Path(begun.stdout.strip())
    log.write_text(output, encoding="utf-8")
    completed = run_receipt(
        root,
        "complete",
        "--root",
        str(root),
        "--gate",
        gate,
        "--log",
        str(log),
        "--candidate-before",
        identity,
        "--candidate-after",
        identity,
        "--exit-code",
        "0",
        "--outcome",
        "passed",
        environment=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert f"CHECK {gate.upper()} PASS" in completed.stdout
    receipts = list((root / ".kickoff" / f"check-{gate}" / "receipts").glob("*.json"))
    assert len(receipts) == 1
    return log, receipts[0]


def record_native_failure(root: Path, environment: dict[str, str]) -> Path:
    identity = candidate(root)
    begun = run_receipt(
        root,
        "begin",
        "--root",
        str(root),
        "--gate",
        "native",
        "--candidate",
        identity,
        environment=environment,
    )
    assert begun.returncode == 0, begun.stderr
    log = Path(begun.stdout.strip())
    log.write_text("native lane failed\n", encoding="utf-8")
    completed = run_receipt(
        root,
        "complete",
        "--root",
        str(root),
        "--gate",
        "native",
        "--log",
        str(log),
        "--candidate-before",
        identity,
        "--candidate-after",
        identity,
        "--exit-code",
        "1",
        "--outcome",
        "failed",
        environment=environment,
    )
    assert completed.returncode == 0, completed.stderr
    return log.with_suffix(".json")


def host_environment(tmp_path: Path, host: str) -> dict[str, str]:
    """A fixture ``ioreg`` on PATH supplies a deterministic host identity."""
    tools = tmp_path / f"host-{host}"
    tools.mkdir(exist_ok=True)
    write_executable(
        tools / "ioreg",
        f'#!/usr/bin/env bash\necho \'  "IOPlatformUUID" = "{host}"\'\n',
    )
    environment = dict(os.environ)
    environment["PATH"] = f"{tools}:{environment['PATH']}"
    return environment


def release_verify(root: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run_receipt(root, "release-verify", "--root", str(root), environment=environment)


def test_passed_run_is_durable_and_reusable_for_exact_clean_head(
    repository: Path,
) -> None:
    log, receipt_path = record_pass(repository)

    result = run_receipt(
        repository,
        "pre-push",
        "--root",
        str(repository),
        stdin=push_input(repository),
    )

    assert result.returncode == 0, result.stderr
    assert "CHECK RECEIPT HIT" in result.stdout
    assert log.read_text(encoding="utf-8").endswith("CHECK ALL PASS\n")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    run = json.loads((repository / receipt["run"]).read_text(encoding="utf-8"))
    assert receipt["candidate_id"] == candidate(repository)
    assert run["status"] == "passed"
    assert run["candidate_before"] == run["candidate_after"]

    # The filename locates a receipt; it does not authenticate its candidate.
    receipt["candidate_id"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    refused = run_receipt(
        repository, "pre-push", "--root", str(repository), stdin=push_input(repository)
    )
    assert refused.returncode == 1
    assert "reason=receipt-identity-mismatch" in refused.stderr


def test_tampered_log_never_reuses_receipt(repository: Path) -> None:
    log, _ = record_pass(repository)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")

    result = run_receipt(
        repository,
        "pre-push",
        "--root",
        str(repository),
        stdin=push_input(repository),
    )

    assert result.returncode == 1
    assert "reason=receipt-log-mismatch" in result.stderr


def test_candidate_drift_is_terminal_and_never_creates_a_receipt(
    repository: Path,
) -> None:
    identity = candidate(repository)
    begun = run_receipt(repository, "begin", "--root", str(repository), "--candidate", identity)
    assert begun.returncode == 0, begun.stderr
    log = Path(begun.stdout.strip())
    log.write_text("gate output\n", encoding="utf-8")

    completed = run_receipt(
        repository,
        "complete",
        "--root",
        str(repository),
        "--log",
        str(log),
        "--candidate-before",
        identity,
        "--candidate-after",
        "1" * 64,
        "--exit-code",
        "1",
        "--outcome",
        "candidate-drift",
    )

    assert completed.returncode == 0, completed.stderr
    metadata = json.loads(log.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["status"] == "candidate-drift"
    assert not (repository / ".kickoff" / "check-all" / "receipts").exists()


def test_pre_push_hook_skips_only_a_verified_hit(repository: Path) -> None:
    record_pass(repository)
    marker = repository / ".kickoff" / "hook-called"
    hook = repository / ".githooks" / "pre-push"

    hit = subprocess.run(
        [str(hook), "origin", "fixture"],
        cwd=repository,
        input=push_input(repository),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert hit.returncode == 0, hit.stderr
    assert "CHECK RECEIPT HIT" in hit.stdout
    assert not marker.exists()

    (repository / "LOG.md").write_text("dirty\n", encoding="utf-8")
    miss = subprocess.run(
        [str(hook), "origin", "fixture"],
        cwd=repository,
        input=push_input(repository),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert miss.returncode == 0, miss.stderr
    assert "reason=working-tree-not-clean" in miss.stderr
    assert marker.read_text(encoding="utf-8") == "called\n"


@pytest.mark.parametrize(
    "case",
    [
        "both-current",
        "receipt-missing",
        "stale-candidate",
        "other-host",
        "dirty-tree",
        "native-failure-after-pass",
    ],
)
def test_release_verify(repository: Path, tmp_path: Path, case: str) -> None:
    host_a = host_environment(tmp_path, "HOST-A")
    host_b = host_environment(tmp_path, "HOST-B")
    identity = candidate(repository)

    def refused(result: subprocess.CompletedProcess[str], gate: str, reason: str) -> None:
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"CHECK RELEASE REFUSED gate={gate} reason={reason}" in result.stderr
        assert "CHECK RELEASE VERIFIED" not in result.stdout

    if case == "receipt-missing":
        result = release_verify(repository, host_a)
        refused(result, "all", "receipt-not-found")
        assert f"candidate={candidate(repository)}" in result.stderr
        record_pass(repository, environment=host_a)
        refused(release_verify(repository, host_a), "native", "receipt-not-found")
        return
    if case == "other-host":
        record_pass(repository, environment=host_a)
        record_pass(repository, environment=host_a, gate="native")
        refused(release_verify(repository, host_b), "all", "host-mismatch")
        record_pass(repository, environment=host_b)
        refused(release_verify(repository, host_b), "native", "host-mismatch")
        return
    if case == "native-failure-after-pass":
        record_pass(repository, environment=host_a)
        record_pass(repository, environment=host_a, gate="native")
        failed_run = record_native_failure(repository, host_a)
        failed_name = failed_run.relative_to(repository).as_posix()
        result = release_verify(repository, host_a)
        refused(result, "native", "native-failure-after-pass")
        assert f"latest-failed-run={failed_name}" in result.stderr
        # A later passing run on the exact candidate and host qualifies, and
        # the earlier failure stays on record and is named.
        record_pass(repository, environment=host_a, gate="native")
        verified = release_verify(repository, host_a)
        assert verified.returncode == 0, verified.stderr
        assert f"CHECK RELEASE NOTE earlier-failed-native-run={failed_name} status=failed" in (
            verified.stdout
        )
        assert "native-failed-runs=1" in verified.stdout
        assert failed_run.is_file()
        # A newer attempt that never finished is not a pass and refuses.
        unfinished = run_receipt(
            repository,
            "begin",
            "--root",
            str(repository),
            "--gate",
            "native",
            "--candidate",
            identity,
            environment=host_a,
        )
        assert unfinished.returncode == 0, unfinished.stderr
        result = release_verify(repository, host_a)
        refused(result, "native", "native-run-unfinished")
        assert "native-failed-runs=2" in result.stderr
        return

    record_pass(repository, environment=host_a)
    record_pass(repository, environment=host_a, gate="native")
    if case == "both-current":
        result = release_verify(repository, host_a)
        assert result.returncode == 0, result.stderr
        assert f"CHECK RELEASE VERIFIED candidate={identity} all=.kickoff/check-all/logs/" in (
            result.stdout
        )
        assert " native=.kickoff/check-native/logs/" in result.stdout
        assert "native-failed-runs=0" in result.stdout
    elif case == "stale-candidate":
        (repository / "LOG.md").write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "change"], cwd=repository, check=True)
        assert candidate(repository) != identity
        refused(release_verify(repository, host_a), "all", "receipt-identity-mismatch")
    else:
        (repository / "untracked.txt").write_text("dirt\n", encoding="utf-8")
        refused(release_verify(repository, host_a), "release", "working-tree-not-clean")
