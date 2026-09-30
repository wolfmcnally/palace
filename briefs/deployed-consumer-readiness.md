---
title: "External-consumer readiness"
status: reference
scope: "Generic deployment requirements and current release boundaries."
---

# External-consumer readiness

Palace provides store-parametric indexing and search. Callers choose their own
corpora, stores, scheduling, and access policy. This brief records capability
requirements; it contains no inventory of actual consumer deployments.

## Inference and data boundaries

Embedding identity is recorded in each store and incompatible query providers
are refused. Local inference, OpenRouter, and private endpoint adapters have
different disclosure boundaries; follow the provider documentation and use
remote inference only for corpora whose disclosure is authorized. OpenRouter
is intended for published corpora. Private endpoints require explicit caller
configuration and credentials. Provider failures must not silently select a
different venue. AWS Bedrock is not an implemented provider.

## Multiple stores

Multi-store search preserves store-of-origin and merges ranks across stores.
Raw vector scores from different embedding identities are not comparable.
Callers remain responsible for selecting permitted stores. Reranking and its
failure status are explicit; see the search and provider guides for the current
API and configuration.

## Platform and release boundaries

Synchronous indexing and search support Linux and macOS. File watching and
LaunchAgent supervision remain macOS-specific. Contributors run the complete
repository gate; hosted CI checks both supported platforms. Synthetic checks do
not establish availability of a particular remote endpoint or local model.

Version 0.1.0 is the first public release target, under the root MIT license.
The release checklist defines package, installation, documentation, and smoke
checks. No package-registry publication or deployment is implied by a Git tag.

## Related references

- [Store-parametric consumers](store-parametric-external-consumers.md)
- [Embedding identity](embedding-provider-and-index-identity.md)
- [Reranking](reranker-defaults-and-providers.md)
- [Release checklist](../RELEASE.md)
