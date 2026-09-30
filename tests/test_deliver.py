"""Behavioral tests for the fail-closed ordinary-delivery helper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DELIVER = ROOT / "bin" / "deliver"
TREE_ID = ROOT / "bin" / "kickoff-tree-id"
CANDIDATE_BOUNDARIES = ROOT / "lib" / "agentic_starter" / "candidate_boundaries.py"


def install_tree_id_contract(root: Path) -> None:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    package = root / "lib" / "agentic_starter"
    package.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TREE_ID, root / "bin" / "kickoff-tree-id")
    shutil.copy2(CANDIDATE_BOUNDARIES, package / "candidate_boundaries.py")
    (package / "__init__.py").touch()
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")


def command(
    *arguments: str | Path,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def configure_identity(root: Path) -> None:
    command("git", "config", "user.name", "Fixture User", cwd=root)
    command("git", "config", "user.email", "fixture@example.invalid", cwd=root)


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    root = tmp_path / "repo"
    install_tree_id_contract(root)
    command("git", "init", "--bare", "-q", "-b", "master", remote, cwd=tmp_path)
    command("git", "init", "-q", "-b", "master", root, cwd=tmp_path)
    configure_identity(root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    command(
        "git",
        "add",
        "tracked.txt",
        "bin/kickoff-tree-id",
        "lib",
        ".gitignore",
        cwd=root,
    )
    command("git", "commit", "-q", "-m", "Initial fixture", cwd=root)
    command("git", "remote", "add", "origin", remote, cwd=root)
    command("git", "push", "-q", "-u", "origin", "master", cwd=root)
    return root, remote


def run_delivery(
    root: Path,
    *paths: str,
    message: str | None = "Deliver fixture change",
    no_commit: bool = False,
    no_push: bool = False,
) -> subprocess.CompletedProcess[str]:
    (root / "bin").mkdir(exist_ok=True)
    local_tree_id = root / "bin" / "kickoff-tree-id"
    if (
        not local_tree_id.exists()
        or not (root / "lib" / "agentic_starter" / "candidate_boundaries.py").exists()
    ):
        install_tree_id_contract(root)
    arguments = [str(DELIVER), "--root", str(root)]
    candidate = command(local_tree_id, "--root", root, cwd=root).stdout.strip()
    arguments.extend(["--candidate", candidate])
    if message is not None:
        arguments.extend(["--message", message])
    if no_commit:
        arguments.append("--no-commit")
    if no_push:
        arguments.append("--no-push")
    arguments.extend(paths)
    return subprocess.run(arguments, capture_output=True, text=True, check=False)


def head(root: Path) -> str:
    return command("git", "rev-parse", "HEAD", cwd=root).stdout.strip()


def remote_head(remote: Path) -> str:
    return command("git", "rev-parse", "master", cwd=remote).stdout.strip()


def test_exact_candidate_is_committed_pushed_and_verified(
    repository: tuple[Path, Path],
) -> None:
    root, remote = repository
    source = root / "tracked.txt"
    destination = root / "archived.txt"
    source.rename(destination)
    destination.write_text("archived and amended\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    command("git", "add", "-A", "--", "tracked.txt", "archived.txt", "new.txt", cwd=root)

    result = run_delivery(root, "tracked.txt", "archived.txt", "new.txt")

    assert result.returncode == 0, result.stderr
    assert "DELIVERY OK" in result.stdout
    assert head(root) == remote_head(remote)
    assert command("git", "status", "--porcelain", cwd=root).stdout == ""
    changed = command(
        "git",
        "show",
        "--no-renames",
        "--pretty=format:",
        "--name-only",
        "HEAD",
        cwd=root,
    ).stdout.splitlines()
    assert sorted(line for line in changed if line) == [
        "archived.txt",
        "new.txt",
        "tracked.txt",
    ]


def test_unexpected_path_parks_before_staging(repository: tuple[Path, Path]) -> None:
    root, _ = repository
    original = head(root)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "unexpected changed paths: unexpected.txt" in result.stderr
    assert head(root) == original
    assert command("git", "diff", "--cached", "--name-only", cwd=root).stdout == ""


def test_missing_upstream_parks_without_committing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    command("git", "init", "-q", "-b", "master", root, cwd=tmp_path)
    configure_identity(root)
    install_tree_id_contract(root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    command(
        "git",
        "add",
        "tracked.txt",
        "bin/kickoff-tree-id",
        "lib",
        ".gitignore",
        cwd=root,
    )
    command("git", "commit", "-q", "-m", "Initial fixture", cwd=root)
    original = head(root)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "branch.master.remote" in result.stderr or "upstream" in result.stderr
    assert head(root) == original


def test_ambiguous_upstream_parks_without_committing(
    repository: tuple[Path, Path],
) -> None:
    root, _ = repository
    original = head(root)
    command("git", "config", "--add", "branch.master.remote", "second", cwd=root)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "exactly one configured upstream" in result.stderr
    assert head(root) == original


def test_pre_commit_hook_refusal_parks_without_commit(
    repository: tuple[Path, Path],
) -> None:
    root, _ = repository
    original = head(root)
    hook = root / ".git" / "hooks" / "pre-commit"
    write_executable(hook, "#!/bin/sh\necho refused >&2\nexit 17\n")
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "commit or hook refused" in result.stderr
    assert "refused" in result.stderr
    assert head(root) == original


def test_remote_divergence_parks_before_commit(
    repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, remote = repository
    other = tmp_path / "other"
    command("git", "clone", "-q", remote, other, cwd=tmp_path)
    configure_identity(other)
    (other / "remote.txt").write_text("remote\n", encoding="utf-8")
    command("git", "add", "remote.txt", cwd=other)
    command("git", "commit", "-q", "-m", "Advance remote", cwd=other)
    command("git", "push", "-q", cwd=other)
    original = head(root)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "not aligned before delivery" in result.stderr
    assert head(root) == original


def test_push_rejection_parks_after_local_commit(
    repository: tuple[Path, Path],
) -> None:
    root, remote = repository
    original_remote = remote_head(remote)
    hook = remote / "hooks" / "pre-receive"
    write_executable(hook, "#!/bin/sh\necho push-refused >&2\nexit 19\n")
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "push refused" in result.stderr
    assert "push-refused" in result.stderr
    assert head(root) != original_remote
    assert remote_head(remote) == original_remote


def test_post_commit_residual_dirt_parks_before_push(
    repository: tuple[Path, Path],
) -> None:
    root, remote = repository
    original_remote = remote_head(remote)
    hook = root / ".git" / "hooks" / "post-commit"
    write_executable(hook, "#!/bin/sh\nprintf residue > residual.txt\n")
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt")

    assert result.returncode == 1
    assert "residual dirt after commit: residual.txt" in result.stderr
    assert remote_head(remote) == original_remote


def test_no_push_restriction_creates_only_a_local_commit(
    repository: tuple[Path, Path],
) -> None:
    root, remote = repository
    original_remote = remote_head(remote)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt", no_push=True)

    assert result.returncode == 0, result.stderr
    assert "RESTRICTED local-only" in result.stdout
    assert head(root) != original_remote
    assert remote_head(remote) == original_remote
    assert command("git", "status", "--porcelain", cwd=root).stdout == ""


def test_no_commit_restriction_leaves_candidate_unstaged(
    repository: tuple[Path, Path],
) -> None:
    root, _ = repository
    original = head(root)
    (root / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = run_delivery(root, "tracked.txt", message=None, no_commit=True)

    assert result.returncode == 0, result.stderr
    assert "RESTRICTED uncommitted" in result.stdout
    assert head(root) == original
    assert command("git", "diff", "--cached", "--name-only", cwd=root).stdout == ""
    assert command("git", "diff", "--name-only", cwd=root).stdout.strip() == "tracked.txt"
