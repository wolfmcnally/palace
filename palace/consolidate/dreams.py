"""The ``dreams/DREAMS.md`` per-run audit renderer + append.

Each consolidation run appends one dated section to ``dreams/DREAMS.md``
recording every candidate it considered — kept AND discarded AND held —
with its full six-signal score breakdown, the three gate outcomes, the
PROMOTE / DISCARD / HELD verdict, and the candidate claim body. The audit is
plain Markdown a human can scan and a headless reader can parse, and it is
reproducible from the same inputs (deterministic given fixed model outputs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from palace.consolidate.scoring import SignalBreakdown

__all__ = ["RunSummary", "append_dreams", "render_dreams_entry"]


@dataclass(frozen=True)
class RunSummary:
    """Headline totals for one consolidation run, rendered into the audit.

    ``source_kind`` defaults to ``"sessions"`` (Wolf's path); the audit header
    names the kind only when it is not the default, so the sessions audit is
    byte-unchanged from before the captures source was added.
    """

    day: date
    candidates_total: int
    promoted: int
    discarded: int
    held_contradiction: int
    dry_run: bool
    source_kind: str = "sessions"


def render_dreams_entry(
    run_summary: RunSummary,
    breakdowns: list[SignalBreakdown],
    held_claims: frozenset[str],
) -> str:
    """Render one run's audit section.

    ``held_claims`` carries the claim text of candidates that passed the gates
    but were held for contradiction, so their verdict renders as HELD rather
    than the bare PROMOTE the gate outcome alone would give.
    """
    day = run_summary.day
    lines: list[str] = []
    mode = " (dry-run)" if run_summary.dry_run else ""
    # Name the source kind only when it is not the default sessions kind, so a
    # sessions-run audit is byte-identical to before the captures source landed.
    kind = "" if run_summary.source_kind == "sessions" else f" [{run_summary.source_kind}]"
    lines.append(f"## Consolidation {day.isoformat()}{kind}{mode}")
    lines.append("")
    lines.append(
        f"Candidates: {run_summary.candidates_total} · "
        f"promoted: {run_summary.promoted} · "
        f"discarded: {run_summary.discarded} · "
        f"held (contradiction): {run_summary.held_contradiction}"
    )
    lines.append("")

    for breakdown in breakdowns:
        verdict = _verdict_for(breakdown, held_claims)
        lines.append(f"### [{verdict}] {breakdown.candidate.summary}")
        lines.append("")
        lines.append("| signal | score |")
        lines.append("|---|---|")
        lines.append(f"| relevance | {_fmt(breakdown.relevance)} |")
        lines.append(f"| frequency | {_fmt(breakdown.frequency)} |")
        lines.append(f"| recency | {_fmt(breakdown.recency)} |")
        lines.append(f"| importance | {_fmt(breakdown.importance)} |")
        lines.append(f"| confidence | {_fmt(breakdown.confidence)} |")
        lines.append(f"| novelty | {_fmt(breakdown.novelty)} |")
        lines.append(f"| **weighted total** | **{_fmt(breakdown.weighted_total)}** |")
        lines.append("")
        lines.append(
            "Gates — relevance: "
            f"{_passfail(breakdown.gate_relevance_pass)}, "
            f"confidence: {_passfail(breakdown.gate_confidence_pass)}, "
            f"corroboration: {_passfail(breakdown.gate_corroboration_pass)}"
            + (f" (failed: {', '.join(breakdown.failed_gates)})" if breakdown.failed_gates else "")
        )
        lines.append("")
        lines.append(breakdown.candidate.claim)
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def append_dreams(path: Path, text: str) -> None:
    """Append a rendered audit section to ``dreams/DREAMS.md`` at EOF.

    Creates the parent ``dreams/`` directory and the file on first run; a
    blank line separates appended sections. Append-only — never rewrites an
    earlier run's bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        existing = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(existing + "\n\n" + text, encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8")


def _verdict_for(breakdown: SignalBreakdown, held_claims: frozenset[str]) -> str:
    """Resolve the displayed verdict, honoring the held-for-contradiction set."""
    if breakdown.candidate.claim in held_claims:
        return "HELD"
    return breakdown.verdict


def _fmt(value: float) -> str:
    """Render a signal float to three decimals for the audit table."""
    return f"{value:.3f}"


def _passfail(value: bool) -> str:
    return "pass" if value else "fail"
