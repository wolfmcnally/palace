---
title: "Toolchain and repository layout"
date: 2026-09-30
status: implemented
scope: "Public technical design and limitations; no private corpus or deployment evidence is included."
---

# Toolchain and repository layout

## Decision

Python 3.12, uv, Ruff, mypy and pytest form the repository toolchain. The binding pins are in `pyproject.toml`, `.python-version` and `uv.lock`. `palace/`, tests and metadata live at repository root. Hatchling builds the package.

## Entry points

Use `./bin/setup`, `./bin/test [args...]`, `./bin/python` and `./bin/check all`. The full gate includes lint, formatting, types, retained tests, CLI smoke and policy checks. The repository wrappers enforce locked runtime selection and refuse unresolved prerequisites rather than claiming an ambient interpreter is qualified.

## Contributor boundary

The public contributor instructions describe standalone setup. Machine-specific launchers, model services and daemon supervision are optional operational dependencies, not evidence that a fresh checkout works. Runtime probes and full gates establish only the environment actually exercised. See the root contributor and release guides.
