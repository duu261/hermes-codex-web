# Hermes Codex Web

A standalone Hermes plugin that exposes a Codex-compatible web adapter as one model tool: `codex_web`.

```text
same Hermes model
  -> codex_web
  -> configured /alpha/search endpoint
  -> evidence, results, and sources
  -> same Hermes model continues answering
```

This is **not native Codex** and does not replace Hermes with another answering model. It is a Codex-compatible Hermes adapter plus a Codex-style research policy. Keep `web_search` for ordinary provider-neutral search and `web_extract` for general page extraction.

## Public tool surface

The model-facing schema contains only:

- `search_query`
- `image_query`
- `open`
- `click`
- `find`
- `screenshot`
- `finance`
- `weather`
- `sports`
- `time`
- `response_length`

Rules enforced by the adapter:

- At most four `search_query` items per call.
- More than three queries require `response_length: medium` or `long`.
- Sports payloads receive `tool: "sports"` internally. The model does not need to supply it.
- `open[].ref_id` accepts an HTTPS or HTTP URL, or a reference returned by an earlier call in the same Hermes conversation.
- PDF screenshot `pageno` is zero-based, and the PDF must be opened first.

The lower-level Python payload builder keeps legacy endpoint controls such as `input`, `reasoning`, `search_context_size`, filters, `external_web_access`, `image_settings`, `user_location`, and `max_output_tokens` for compatibility. They are deliberately absent from the default model-facing schema.

## Install

```bash
hermes plugins install https://github.com/duu261/hermes-codex-web.git --enable
```

Restart the Hermes surface using the plugin after installation.

## Configuration

Credentials belong in Hermes' private environment file or protected process environment, never in this repository:

```text
CODEX_WEB_BASE_URL=https://gateway.example/v1
CODEX_WEB_API_KEY=replace-me
```

`CODEX_SEARCH_BASE_URL` and `CODEX_SEARCH_API_KEY` remain accepted as migration fallbacks. Dedicated `CODEX_WEB_*` values win.

Set non-secret settings in Hermes config, not `.env`:

```bash
hermes config set web.codex_web.model gpt-5.4-mini
```

Optional `web.codex_web` settings:

- `request_timeout_seconds`, default `60`
- `max_retries`, default `2`, capped at `5`
- `retry_base_seconds`, default `0.25`
- `max_response_bytes`, default `4194304`
- `session_ttl_seconds`, default `1800`
- `max_sessions`, default `1024`

Retries cover HTTP 408, 429, and temporary 5xx responses. `Retry-After` is honored. State is in-process only, bounded, TTL-evicted, thread-safe, and lost on process restart.

## Stateful continuation

The current Hermes runtime path is verified in the installed source:

```text
agent/tool_executor.py
  -> model_tools.handle_function_call(..., session_id=agent.session_id)
    -> tools.registry.dispatch(..., session_id=session_id)
      -> plugin handler(params, session_id=session_id)
```

`AIAgent` creates or receives a durable Hermes `session_id` for CLI and gateway runs. The gateway also tracks a stable per-chat `gateway_session_key`, but the tool execution contract passes the canonical agent `session_id`, which is the key used here.

The adapter maps one Hermes conversation to one upstream request `id`. It records returned `ref_id` values and rejects missing, cross-session, expired, or post-restart references with a machine-readable `reference_expired` error. It does not persist page context or references to disk.

`encrypted_output` is retained in successful adapter results when returned by the endpoint. It is **not replayed** on follow-up requests. Opt-in live testing verified that a search result can be opened successfully without sending the previous `encrypted_output`; this is an observed endpoint result, not a claim about every future deployment.

## Research skill

The plugin registers the opt-in skill `codex-web:codex-web-research`. It teaches Hermes to browse for explicit verification and unstable facts, prefer primary sources, search then open/find/click important claims, open PDFs before screenshots, cite direct URLs, hide internal refs, label inferences, report uncertainty, and retain `web_extract` as the general extraction fallback.

Load it explicitly when wanted:

```text
/skill codex-web:codex-web-research
```

## Response shape

Successful responses are normalized to:

```json
{
  "success": true,
  "output": "endpoint text",
  "results": [
    {
      "type": "text_result",
      "ref_id": "turn0search0",
      "title": "Example",
      "url": "https://example.com",
      "future_fields": "preserved"
    }
  ],
  "sources": [
    {
      "ref_id": "turn0search0",
      "title": "Example",
      "url": "https://example.com",
      "type": "web"
    }
  ]
}
```

`sources` is derived only from actual endpoint result objects containing a URL. The adapter never reconstructs URLs from reference IDs or memory. Unknown fields inside endpoint results are preserved.

Errors are machine-readable and distinguish `validation`, `configuration`, `authentication`, `quota`, `rate_limit`, `upstream`, `timeout`, `malformed_response`, `response_too_large`, and `reference_expired` where applicable. Upstream messages are bounded and sanitized. Credentials, authorization headers, endpoint URLs, and request bodies are not returned.

## Capability matrix

| Capability | Schema-supported | Unit-tested | Live endpoint-tested | Fully stateful in real Hermes | Endpoint-dependent | Status / caveat |
| --- | --- | --- | --- | --- | --- | --- |
| `search_query` | Yes | Yes | Yes | Yes | Yes | Four-query and length rules enforced. |
| `image_query` | Yes | Yes | Yes | Yes | Yes | Endpoint result shape may vary. |
| `open` | Yes | Yes | Yes | Yes | Yes | URL and returned refs supported. |
| `find` | Yes | Yes | Yes | Yes | Yes | Requires an opened or returned ref for continuation. |
| `click` | Yes | Yes | Yes | Yes | Yes | Requires a valid numbered link in endpoint context. |
| `screenshot` | Yes | Yes | Yes | Yes | Yes | Tested with a direct public PDF; deployment support may vary. |
| `finance` | Yes | Yes | Yes | Yes | Yes | Supported asset types are endpoint/schema constrained. |
| `weather` | Yes | Yes | Yes | Yes | Yes | Specialized output may have no result URLs. |
| `sports` | Yes | Yes | Yes | Yes | Yes | Adapter injects `tool: "sports"`; league support may vary. |
| `time` | Yes | Yes | Yes | Yes | Yes | Specialized output may have no result URLs. |
| `response_length` | Yes | Yes | Yes | Yes | Yes | Length is sent in `commands`. |
| Reference continuation | N/A | Yes | Yes | Yes | Yes | In-process only; TTL, eviction, and restart reset are explicit. |
| `encrypted_output` | N/A | Yes | Yes | Yes | Yes | Retained when returned; not replayed. |
| Research policy | N/A | Yes | N/A | Opt-in | No | Registered as `codex-web:codex-web-research`. |

Live rows mean the configured endpoint returned successful results during the opt-in live suite on the maintainer's environment. They do not prove every upstream account, gateway, league, or future backend behaves identically.

## Verification

Unit and static checks:

```bash
python -m unittest discover -s tests -v
python -m py_compile provider.py __init__.py
git diff --check
hermes plugins doctor . --ci
```

The live suite is opt-in and skips safely otherwise:

```bash
CODEX_WEB_LIVE_TESTS=1 python -m unittest tests/test_live.py -v
```

It covers search → open → find → click, PDF open → screenshot, image search, finance, weather, sports, time, encrypted-output continuation, and concurrent session isolation. Live tests never print credentials or response bodies.

## Known differences from native Codex

- The adapter calls a configured `/alpha/search` endpoint instead of Hermes' or Codex' native internal web runtime.
- Hermes exposes one function tool, not native Codex web-search call items.
- Continuation state is bounded in process memory and is lost after restart or eviction.
- Endpoint permissions and implementation determine whether a command actually succeeds.
- The adapter does not provide `web_extract`; use Hermes `web_extract` for general extraction.
- `sources` is an adapter normalization, not a claim that native Codex returns this exact shape.

## Public-repository rule

Do not commit production domains, internal IDs, customer data, credentials, tokens, or secret-bearing logs.

## License

MIT
