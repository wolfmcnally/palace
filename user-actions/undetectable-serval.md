---
slug: undetectable-serval
title: Qualify the Bedrock embedding provider live and settle its engineering defaults
status: pending
category: product-review
urgency: medium
blocks:
  - Live acceptance of the Bedrock embedding provider and composed settings (Phase 22)
  - A tagged release carrying the provider
filed: 2026-11-07
needed_at: after delivery
source: kickoff
refs:
  - plan/phase-22.md
  - docs/bedrock.md
  - briefs/bedrock-embedding-provider.md
---

Phase 22 delivered the `bedrock` embedding provider, the composed `palace` settings section and per-store vector width with offline, hermetic proofs only. Three things remain yours.

**Run the User Demo and read the operator guide.** The protocol is in `plan/phase-22.md` and the Phase 22 END block in `LOG.md`; it makes no network request and reads no credential. Judge whether the refusal texts and `docs/bedrock.md` are clear enough to hand to an operator.

**Live qualification is a paid, operator-approved action.** One build and one search against the real `cohere.embed-v4:0` in one Region (direct in-Region access is documented today only in `us-east-1`, `eu-west-1` and `ap-northeast-1`), from a workstation profile and from an instance role, with the Region's retention mode set to `none` and the IAM grants `bedrock:InvokeModel` and `bedrock:GetAccountDataRetention` in place. Expected: the retention check passes, the returned dimension equals the selector's `dim`, and `events/cloud-egress/` shows one record per attempt without text or credentials. State the cost before running. Record the result against the brief's dated observations.

**Settle two unmeasured defaults** noted as owner decisions in the plan: build concurrency ceiling 4 (`BEDROCK_EMBED_MAX_CONCURRENCY`) and the 120,000-unit aggregate request budget (`BEDROCK_REQUEST_BUDGET`); and decide whether a production `AWS_BEARER_TOKEN_BEDROCK` in the environment should be refused rather than left to botocore's default precedence. The model allowlist is exactly `cohere.embed-v4:0`; a later revision needs a new phase.

The editable system-wide uv tool needs `uv tool install --editable . --reinstall` because Conric is a new dependency.
