from __future__ import annotations

import copy
import hashlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import agentic_starter.test_governance as governance  # noqa: E402


def test_parameterized_leaves_collapse_to_one_family() -> None:
    assert governance._pytest_family("tests/test_x.py::test_case[value]") == (
        "tests/test_x.py::test_case"
    )


def test_inventory_counts_executable_families_and_expanded_leaves() -> None:
    observed = governance.inventory(REPO_ROOT)
    assert observed["counts"] == {"families": 289, "leaves": 325}
    assert observed["by_kind"]["pytest"] == {"families": 265, "leaves": 301}


def test_live_reset_validates() -> None:
    summary = governance.validate(REPO_ROOT)
    assert summary["state"] == "valid"
    assert summary["dispositions"]["delete"] > 0
    assert summary["dispositions"]["consolidate"] > 0


def test_family_or_leaf_cap_overrun_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = governance.inventory(REPO_ROOT)
    manifest = governance.load_yaml(REPO_ROOT / "tests/proof-estate.yaml")
    allowed = {
        dimension: manifest["post_reset_counts"][dimension]
        + manifest["test_budget_delta"][dimension]
        for dimension in ("families", "leaves")
    }
    over = copy.deepcopy(original)
    over["counts"]["families"] = allowed["families"] + 1
    monkeypatch.setattr(governance, "inventory", lambda _root: over)
    with pytest.raises(governance.GovernanceError, match="families cap exceeded"):
        governance.validate(REPO_ROOT)
    over["counts"]["families"] = allowed["families"]
    over["counts"]["leaves"] = allowed["leaves"] + 1
    with pytest.raises(governance.GovernanceError, match="leaves cap exceeded"):
        governance.validate(REPO_ROOT)


def test_incomplete_disposition_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = governance.load_ledger
    mode = ["missing"]

    def malformed(path: Path):
        rows = original(path)
        if path.name == "palace-reset.jsonl":
            if mode[0] == "missing":
                rows.pop(0)
            else:
                rows[0] = {key: value for key, value in rows[0].items() if key != "oracle"}
        return rows

    monkeypatch.setattr(governance, "load_ledger", malformed)
    with pytest.raises(governance.GovernanceError, match="misses 1 baseline proofs"):
        governance.validate(REPO_ROOT)
    mode[0] = "evidence"
    with pytest.raises(governance.GovernanceError, match="wrong disposition fields"):
        governance.validate(REPO_ROOT)


def test_shadow_deleted_proof_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    original = governance.load_ledger
    mode = ["delete"]

    def shadowed(path: Path):
        rows = original(path)
        if path.name == "palace-reset.jsonl":
            if mode[0] == "delete":
                retained = next(row for row in rows if row["disposition"] == "retain")
                retained["disposition"] = "delete"
                retained["replacement"] = None
            else:
                consolidated = next(row for row in rows if row["disposition"] == "consolidate")
                consolidated["replacement"] = "pytest:absent"
        return rows

    monkeypatch.setattr(governance, "load_ledger", shadowed)
    with pytest.raises(governance.GovernanceError, match="deleted proof still exists"):
        governance.validate(REPO_ROOT)
    mode[0] = "replacement"
    with pytest.raises(governance.GovernanceError, match="invalid replacement"):
        governance.validate(REPO_ROOT)


def test_critical_risk_requires_a_retained_direct_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = governance.load_yaml

    def missing(path: Path):
        payload = original(path)
        if path.name == "proof-estate.yaml":
            payload["critical_risks"]["custody"]["direct_proof"] = "pytest:absent"
        return payload

    monkeypatch.setattr(governance, "load_yaml", missing)
    with pytest.raises(governance.GovernanceError, match="direct proof is not retained"):
        governance.validate(REPO_ROOT)


def test_recall_below_eighty_percent_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = governance.load_ledger

    def weakened(path: Path):
        rows = original(path)
        if path.name == "palace-effectiveness.jsonl":
            for row in rows[:3]:
                row["observed"] = False
        return rows

    monkeypatch.setattr(governance, "load_ledger", weakened)
    with pytest.raises(governance.GovernanceError, match="recall below floor"):
        governance.validate(REPO_ROOT)

    monkeypatch.setattr(governance, "load_ledger", original)
    original_yaml = governance.load_yaml

    def drifted(path: Path):
        payload = original_yaml(path)
        if path.name == "corpus.yaml":
            payload["cases"][0]["patch_sha256"] = "0" * 64
        return payload

    monkeypatch.setattr(governance, "load_yaml", drifted)
    with pytest.raises(governance.GovernanceError, match="patch digest drifted"):
        governance.validate(REPO_ROOT)

    # A failure in an unmodified copy cannot count as mutation detection.
    fixture = tmp_path / "assay"
    fixture.mkdir()
    (fixture / "flag").write_text("good\n")
    (fixture / "alias").symlink_to("flag")
    patch = fixture / "defect.patch"
    patch.write_text("--- a/flag\n+++ b/flag\n@@ -1 +1 @@\n-good\n+bad\n")
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('alias').is_symlink(); "
            "assert Path('alias').read_text() == 'good\\n'",
        ]
    )
    corpus = {
        "selection_frozen": True,
        "cases": [
            {
                "id": "copied-link",
                "class": "historical_defect",
                "patch": "defect.patch",
                "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                "command": command,
                "owner": "fixture",
            }
        ],
    }
    monkeypatch.setattr(
        governance,
        "load_yaml",
        lambda path: (
            {"effectiveness_corpus": "corpus.yaml"}
            if path.name == "proof-estate.yaml"
            else copy.deepcopy(corpus)
        ),
    )
    rows = governance.assay(fixture)
    assert len(rows) == 1 and rows[0]["observed"] is True
    assert (fixture / "flag").read_text() == "good\n"
    (fixture / "alias").unlink()
    (fixture / "alias").write_text("good\n")
    with pytest.raises(governance.GovernanceError, match="assay baseline failed for copied-link"):
        governance.assay(fixture)


def test_positive_growth_requires_named_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = governance.load_yaml
    mode = ["budget"]

    def unapproved(path: Path):
        payload = original(path)
        if path.name == "proof-estate.yaml":
            payload["test_budget_delta"]["families"] = 1
            payload.pop("budget_approval", None)
        elif path.name == "corpus.yaml" and mode[0] == "selection":
            payload["selection_frozen"] = False
        return payload

    monkeypatch.setattr(governance, "load_yaml", unapproved)
    with pytest.raises(governance.GovernanceError, match="positive families budget"):
        governance.validate(REPO_ROOT)
    mode[0] = "selection"
    with pytest.raises(governance.GovernanceError, match="selection must be frozen"):
        governance.validate(REPO_ROOT)

    # An admission may be funded by a named approved budget instead of a
    # retirement. Admission rows are leaves: each funded row consumes one
    # approved leaf unit, and family growth stays bounded by the family cap.
    ledger_path = REPO_ROOT / original(REPO_ROOT / "tests/proof-estate.yaml")["audit_ledger"]
    original_ledger = governance.load_ledger
    already_funded = sum(
        row.get("record_type") == "proof_admission" and row.get("compensating_retirement") is None
        for row in original_ledger(ledger_path)
    )
    # Cases are increments over the live manifest's own budget, which already
    # funds the live budget-funded admissions; family growth from them stays
    # inside the live family delta.
    live_delta = original(REPO_ROOT / "tests/proof-estate.yaml")["test_budget_delta"]
    single = "pytest:tests/test_test_governance.py::test_recall_below_eighty_percent_fails_closed"
    family = (
        "pytest:tests/test_toolchain_entrypoints.py::"
        "test_installed_symlink_chain_selects_the_owning_repository"
    )
    parametrized = [
        f"{family}[{case}]"
        for case in ("check-arguments2", "python-arguments3", "setup-arguments0", "test-arguments1")
    ]
    funded: list[str] = []
    families = 0
    leaves = 0
    approval: str | None = None

    def budgeted(path: Path):
        payload = original(path)
        if path.name == "proof-estate.yaml":
            payload["test_budget_delta"] = {
                "families": families + live_delta["families"],
                # A case with no leaf budget leaves every funded row unfunded.
                "leaves": leaves + already_funded if leaves else 0,
            }
            if approval is None:
                payload.pop("budget_approval", None)
            else:
                payload["budget_approval"] = approval
        return payload

    def budget_funded(path: Path):
        rows = original_ledger(path)
        if path == ledger_path:
            for row in rows:
                if row.get("record_type") == "proof_admission" and row.get("proof_id") in funded:
                    row["compensating_retirement"] = None
        return rows

    def refused(message: str) -> None:
        with pytest.raises(governance.GovernanceError, match=message):
            governance.validate(REPO_ROOT)

    monkeypatch.setattr(governance, "load_yaml", budgeted)
    monkeypatch.setattr(governance, "load_ledger", budget_funded)
    funded = [single]
    refused(f"lacks an available retirement: {re.escape(single)}")
    approval = "fixture owner approval"
    refused(f"lacks an available retirement: {re.escape(single)}")
    leaves = 1
    approval = "   "
    refused("positive leaves budget requires a named budget_approval")
    approval = "fixture owner approval"
    summary = governance.validate(REPO_ROOT)
    assert summary["state"] == "valid"
    assert summary["budget_funded_admissions"] == already_funded + 1
    families = 2
    funded = [single, parametrized[0]]
    refused("exceeds the approved budget")
    families = 0
    funded = parametrized
    leaves = 4
    summary = governance.validate(REPO_ROOT)
    assert summary["budget_funded_admissions"] == already_funded + 4
    leaves = 3
    refused("exceeds the approved budget")


def test_changed_selection_widens_on_an_unmapped_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/proof-estate.yaml").write_text(
        "families:\n"
        "- id: retained\n"
        "  tier: vital\n"
        "  kind: pytest\n"
        "  selectors: [tests/test_x.py]\n"
        "  covers: [known/**]\n"
        "  source_paths: [tests/test_x.py]\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "known").mkdir()
    (tmp_path / "known/file").write_text("base")
    subprocess.run(
        ["git", "add", "known/file", "tests/proof-estate.yaml"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / "unknown").write_text("change")
    monkeypatch.setattr(governance, "validate", lambda _root: {"state": "valid"})
    families, reason = governance.selected_families(tmp_path, "changed", "HEAD")
    assert reason == "unmapped-changes:unknown"
    assert [family["id"] for family in families] == ["retained"]


def test_report_reproduces_reset_ratios() -> None:
    payload = governance.report(REPO_ROOT)
    assert payload["baseline"] == {"families": 1355, "leaves": 1574}
    assert payload["current"] == {"families": 289, "leaves": 325}
    # Above the reset's 0.2 limit only by the approved family and leaf budgets.
    manifest = governance.load_yaml(REPO_ROOT / "tests/proof-estate.yaml")
    for dimension in ("families", "leaves"):
        funded = manifest["post_reset_counts"][dimension] + manifest["test_budget_delta"][dimension]
        ratio = payload["family_ratio" if dimension == "families" else "leaf_ratio"]
        assert ratio <= funded / payload["baseline"][dimension]
