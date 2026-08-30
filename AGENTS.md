# Hermes Codex Web - Agent Instructions

## Scope

This repository is a standalone Hermes plugin. It registers one model-facing
`codex_web` tool and an opt-in `codex-web-research` skill. The tool sends
Codex-compatible `/alpha/search` requests to an operator-configured endpoint.

## Repository map

- `provider.py` - schema, validation, payload construction, HTTP handling,
  response normalization, and bounded continuation state
- `__init__.py` - Hermes tool and skill registration
- `SKILL.md` - opt-in research policy
- `tests/test_provider.py` - deterministic unit and contract tests
- `tests/test_hermes_e2e.py` - installed Hermes runtime integration test
- `tests/test_live.py` - opt-in authenticated endpoint tests
- `README.md` - public user documentation and disclaimers

## Implementation contracts

- Keep the model-facing schema aligned with confirmed native Codex callable
  behavior unless a stricter difference is deliberate and documented.
- Keep `input` and `reasoning` as lower-level compatibility fields only; do not
  expose them in the model-facing schema without an explicit product decision.
- Treat domain filters and `external_web_access` as advisory. They are not a
  security, network, or privacy boundary unless the configured endpoint proves
  otherwise.
- Do not claim that an endpoint-honored field materially changes retrieval
  without a matched behavioral probe.
- Preserve bounded, thread-safe, process-local continuation state. Never expose
  credentials, authorization headers, request bodies, encrypted output, or raw
  upstream error bodies.
- Do not add local filtering, caching, browser automation, or policy machinery
  to compensate for endpoint behavior without a concrete requirement.

## Hermes runtime path

The installed Hermes path must pass the canonical agent session ID to the tool:

```text
AIAgent
  -> tool executor
  -> model tool dispatch(session_id=agent.session_id)
  -> plugin handler(session_id=session_id)
```

The plugin maps one Hermes conversation to one upstream request `id`. State is
in-process, bounded, TTL-evicted, and lost on process restart.

## Verification

Run from the repository root:

```bash
python -m unittest discover -s tests -v
python -m py_compile provider.py __init__.py tests/test_live.py tests/test_provider.py
git diff --check
hermes plugins doctor . --ci
```

Authenticated tests may consume upstream quota. Run only when intended:

```bash
CODEX_WEB_LIVE_TESTS=1 python -m unittest tests.test_live.CodexWebLiveTests -v
```

Live tests may retry one transient `upstream` or `timeout` result, then must
fail. They must not silently convert deterministic endpoint errors into skips.
Never print credentials, request bodies, raw output, or secret-bearing logs.

## Documentation rules

- README claims about native behavior or live support require direct evidence.
- Label deployment-specific observations as deployment-specific.
- Keep the reverse-proxy trust, logging, retention, billing, authorization,
  and non-affiliation disclaimers in README.
- Do not name private production hosts, account IDs, customer data, credentials,
  tokens, or internal secret-bearing logs.
- Keep internal test-harness details and contributor commands here, not in
  README.

## Git and operations

- Prefer the smallest correct change. No speculative abstractions or new
  dependencies.
- Use Conventional Commits with an imperative subject of 50 characters or
  fewer.
- Run tests and an independent review before committing code changes.
- Do not restart the Hermes gateway from this repository. Tell the maintainer
  to run the restart command separately, then verify after the gateway returns.
- Do not mutate API gateways, production services, databases, DNS, firewall,
  or credentials from this repository.
