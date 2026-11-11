---
slug: vague-warthog
title: Decide the Bedrock brief's authentication wording after bearer-key admission
status: pending
category: product-review
urgency: low
blocks: []
filed: 2026-11-11
needed_at: "After the 0.4.1 release"
source: kickoff
refs:
  - briefs/bedrock-embedding-provider.md
  - docs/bedrock.md
  - CHANGELOG.md
---

Palace 0.4.1 admits a Bedrock API key from the AWS SDK's `AWS_BEARER_TOKEN_BEDROCK` variable, as you asked when requesting the authentication fix and release. The operator guide and changelog describe that contract. The design brief still reads "The AWS SDK credential chain only (instance role, named profile, environment)", which a future reader could take as a reason to restore the old refusal. Brief wording is yours, so it was not edited during the release.

Proposed replacement for the brief's Authentication bullet: "**Authentication.** Whatever the AWS SDK resolves: SigV4 credentials from its chain (instance role, named profile, environment), or a Bedrock API key in the SDK's bearer-token variable, which the SDK prefers when present. The selector may name a profile; it never carries a secret. Missing credentials, a blank token or a token outside the RFC 6750 bearer syntax refuse at `probe()` without showing the value. A bearer token never weakens the retention check: the key's identity must be allowed to read the account's retention mode, and server-side acceptance of a real key is not proved by Palace's offline tests."

Accept, amend or reject the wording; no live service, credential or cost is involved.
