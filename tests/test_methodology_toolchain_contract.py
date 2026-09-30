from __future__ import annotations

import copy
import runpy
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text().split())


POLICY = _normalized(REPO_ROOT / "policies" / "build-gates.md")
COMMIT_STAGING_POLICY = _normalized(REPO_ROOT / "policies" / "commit-staging.md")
MECHANISTIC_POLICY = _normalized(REPO_ROOT / "policies" / "mechanistic-vs-intelligence.md")
ORCHESTRATION_POLICY = _normalized(REPO_ROOT / "policies" / "orchestration-evidence.md")
INCREMENTAL_BRIEF = _normalized(REPO_ROOT / "briefs" / "incremental-orchestration.md")
METHODOLOGY_BRIEF = _normalized(REPO_ROOT / "briefs" / "methodology.md")
BOOTSTRAP_BRIEF = _normalized(REPO_ROOT / "briefs" / "agentic-bootstrap.md")
CLAUDE = _normalized(REPO_ROOT / "CLAUDE.md")
KICKOFF = " ".join(
    _normalized(path) for path in sorted((REPO_ROOT / ".claude/skills/kickoff").glob("*.md"))
)
METHODOLOGY = _normalized(REPO_ROOT / ".claude" / "skills" / "methodology" / "SKILL.md")
PLANNER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-planner.md")
PLAN_REVIEWER = _normalized(REPO_ROOT / ".claude" / "agents" / "plan-reviewer.md")
CODER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-coder.md")
CODE_CRITIC = _normalized(REPO_ROOT / ".claude" / "agents" / "code-critic.md")
LEARN = _normalized(REPO_ROOT / ".claude" / "skills" / "learn" / "SKILL.md")
TEACH = _normalized(REPO_ROOT / ".claude" / "skills" / "teach" / "SKILL.md")
DEMO = _normalized(REPO_ROOT / ".claude" / "skills" / "demo" / "SKILL.md")
TREATISE = _normalized(REPO_ROOT / ".claude" / "skills" / "treatise" / "SKILL.md")
RESEARCH_POLICY = _normalized(REPO_ROOT / "policies" / "research-authority.md")
VERIFICATION_POLICY = _normalized(REPO_ROOT / "policies" / "verification-discipline.md")
TREATISE_POLICY = _normalized(REPO_ROOT / "policies" / "treatise.md")
USER_DEMO_POLICY = _normalized(REPO_ROOT / "policies" / "user-demo-protocols.md")
TEST_GOVERNANCE_POLICY = _normalized(REPO_ROOT / "policies" / "test-suite-governance.md")
TEST_GOVERNANCE_BRIEF = _normalized(REPO_ROOT / "briefs" / "test-suite-value-governance.md")
CONTROL_POLICY = _normalized(REPO_ROOT / "policies" / "orchestration-control-plane.md")
CONTROL_BRIEF = _normalized(REPO_ROOT / "briefs" / "deterministic-orchestration-control-plane.md")
RULE_ONE = _normalized(REPO_ROOT / ".claude" / "skills" / "rule-one" / "SKILL.md")
RULE_ONE_BRIEF = _normalized(REPO_ROOT / "briefs" / "rule-one-diagnostic-learning.md")


def test_proof_estate_governance_propagates_without_local_judgments() -> None:
    required = (
        "briefs/test-suite-value-governance.md",
        "policies/test-suite-governance.md",
        "bin/test-governance",
        "lib/agentic_starter/test_governance.py",
        "tests/test_test_governance.py",
        "tests/test_pre_commit.py",
        "reports/test-governance/README.md",
    )
    for phrase in required:
        assert (REPO_ROOT / phrase).exists(), f"{phrase} missing from the local bundle"
    for document in (LEARN, TEACH, BOOTSTRAP_BRIEF):
        assert "proof" in document and ("estate" in document or "governance" in document)
    for document in (
        TEST_GOVERNANCE_POLICY,
        TEST_GOVERNANCE_BRIEF,
        LEARN,
        TEACH,
        BOOTSTRAP_BRIEF,
    ):
        assert "local" in document or "recipient" in document
        assert "full" in document
    assert "Never copy donor family choices" in LEARN
    assert "Never seed the target" in TEACH


def test_every_universal_skill_propagates_with_its_codex_mirror(tmp_path: Path) -> None:
    """Every canonical skill except starter-only `stamp` reaches a derived project.

    `bin/check-harness-parity` fails closed on a canonical skill with no
    `.agents/skills` mirror, so a skill named in the transfer documents without
    its symlink breaks the destination's gate exactly as a missing skill does.
    """
    canonical_root = REPO_ROOT / ".claude" / "skills"
    universal = sorted(
        item.name for item in canonical_root.iterdir() if item.is_dir() and item.name != "stamp"
    )
    assert "plain" in universal, "the operator register is a universal skill"
    for skill in universal:
        canonical = REPO_ROOT / ".claude" / "skills" / skill
        mirror = REPO_ROOT / ".agents" / "skills" / skill
        assert mirror.is_symlink(), f"{skill} mirror is not a directory symlink"
        assert mirror.resolve() == canonical.resolve()
    from tests.test_mirror_parity import assert_repository_is_in_parity

    assert_repository_is_in_parity(tmp_path / "resource-delivery")
    _assert_rule_one_and_control_plane_propagate_as_atomic_bundles()

    for module in ("workflow.py", "advisory.py"):
        relative = "lib/agentic_starter/" + module
        for authority in (LEARN, TEACH, BOOTSTRAP_BRIEF):
            assert relative in authority
        shutil.copy2(REPO_ROOT / relative, tmp_path / module)
    copied = runpy.run_path(str(tmp_path / "workflow.py"))
    pins = {
        "role_models": {
            "default": {
                role: {"model": "default"} for role in ("planner", "reviewer", "coder", "critic")
            }
        },
        "workflow": copy.deepcopy(copied["DEFAULT_WORKFLOW"]),
    }
    pins["workflow"]["allowed_harnesses"] = ["codex"]
    resolved = copied["resolve"](pins, "codex")
    assert resolved["mode"] == "primary"
    assert resolved["roles"]["coder"]["execution"] == "inline"
    assert resolved["roles"]["critic"]["model"] == "astra"


def _assert_rule_one_and_control_plane_propagate_as_atomic_bundles() -> None:
    for phrase in (
        ".claude/skills/rule-one/SKILL.md",
        "briefs/rule-one-diagnostic-learning.md",
    ):
        assert phrase in LEARN and phrase in TEACH and phrase in BOOTSTRAP_BRIEF
    assert "symptom" in RULE_ONE and "mechanism" in RULE_ONE
    assert "Rule One" in RULE_ONE_BRIEF and "Rule One" in METHODOLOGY

    required = (
        "bin/kickoff-command-zero",
        "bin/check-log",
        "lib/agentic_starter/kickoff_runbook.py",
        "lib/agentic_starter/log_blocks.py",
    )
    for phrase in required:
        assert (REPO_ROOT / phrase).exists()
        assert phrase in LEARN and phrase in BOOTSTRAP_BRIEF
    for document in (CONTROL_POLICY, CONTROL_BRIEF, KICKOFF):
        assert "manifest" in document and "park" in document
    assert "recipient" in LEARN and "Never copy donor" in LEARN


def test_every_gate_required_executable_propagates() -> None:
    """`bin/check` names the executables it refuses to start without.

    That list is the authority: a transfer document that omits one produces a
    destination whose gate exits before running a single check. Reading the
    requirement out of `bin/check` keeps this test honest when the list grows.
    """
    check = (REPO_ROOT / "bin" / "check").read_text()
    marker = "for evidence_executable in \\\n"
    start = check.index(marker) + len(marker)
    end = check.index("; do", start)
    required = check[start:end].replace("\\\n", " ").split()
    assert len(required) >= 14, required
    for name in required:
        assert (REPO_ROOT / "bin" / name).is_file(), name
    assert "bin/test-governance" in BOOTSTRAP_BRIEF
    assert "Toolchain transfer is atomic" in TEACH


def test_research_authority_contract_propagates_and_stays_allow_by_default() -> None:
    assert "allow-by-default" in RESEARCH_POLICY
    assert "same-host structural neighbors" in RESEARCH_POLICY
    assert "GET" in RESEARCH_POLICY
    for document in (TEACH, BOOTSTRAP_BRIEF, KICKOFF):
        assert "research" in document
    for role in (PLANNER, PLAN_REVIEWER):
        assert "originate" in role and "retriev" in role
    for role in (CODER, CODE_CRITIC):
        assert "Do not originate" in role


def test_material_review_counts_are_reproducible() -> None:
    assert "Material counts are reproducible" in VERIFICATION_POLICY
    for document in (PLAN_REVIEWER, CODE_CRITIC, KICKOFF):
        assert "material count" in document
        assert "exact command or deterministic procedure" in document
