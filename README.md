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

## Transport and trust boundary

The plugin sends requests to an operator-configured `/alpha/search` endpoint. That endpoint may be a reverse proxy or gateway, such as New API or CLIProxyAPI, rather than a direct OpenAI endpoint. The gateway operator controls authentication, upstream account routing, logging, retention, billing, rate limits, request rewriting, and availability.

Use only an HTTPS endpoint you trust and are authorized to access. Installing this plugin does not create an OpenAI or Codex entitlement, prove that an endpoint is first-party, or bypass any provider controls.

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
- `context`
- `search_context_size`
- `allowed_domains`
- `blocked_domains`
- `image_settings`
- `external_web_access`
- `user_location`
- `max_output_tokens`

Rules enforced by the adapter:

- At most four `search_query` items per call.
- More than three queries require `response_length: medium` or `long`.
- Sports payloads receive `tool: "sports"` internally. The model does not need to supply it.
- `open[].ref_id` accepts an HTTPS or HTTP URL, or a reference returned by an earlier call in the same Hermes conversation.
- PDF screenshot `pageno` is zero-based, and the PDF must be opened first.

The lower-level Python payload builder additionally accepts `input` and `reasoning` for endpoint compatibility. Those two fields remain deliberately absent from the model-facing schema.

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
- `max_refs_per_session`, default `256`
- `max_ref_length`, default `512`
- `max_retry_after_seconds`, default `60`

Retries cover HTTP 408, 429, and temporary 5xx responses. `Retry-After` is honored up to the configured cap. State is in-process only, bounded, TTL-evicted, thread-safe, and lost on process restart.

## Stateful continuation

The current Hermes runtime path is verified in the installed source:

```text
agent/tool_executor.py
  -> model_tools.handle_function_call(..., session_id=agent.session_id)
    -> tools.registry.dispatch(..., session_id=session_id)
      -> plugin handler(params, session_id=session_id)
```

`AIAgent` creates or receives a durable Hermes `session_id` for CLI and gateway runs. The gateway also tracks a stable per-chat `gateway_session_key`, but the tool execution contract passes the canonical agent `session_id`, which is the key used here.

The adapter maps one Hermes conversation to one upstream request `id`. It records bounded reference provenance and rejects missing, cross-session, expired, post-restart, non-PDF, or not-yet-opened screenshot references with machine-readable errors. It does not persist page context or references to disk.

`encrypted_output` is retained internally in the bounded conversation state when returned by the endpoint. It is never exposed in the model-facing tool result and is **not replayed** on follow-up requests. Opt-in live testing verified that a search result can be opened successfully without sending the previous `encrypted_output`; this is an observed endpoint result, not a claim about every future deployment.

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

Errors are machine-readable and distinguish `validation`, `configuration`, `authentication`, `quota`, `rate_limit`, `upstream`, `timeout`, `malformed_response`, `response_too_large`, and `reference_expired` where applicable. Upstream response bodies are not returned. Credentials, authorization headers, endpoint URLs, and request bodies are not returned.

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
| Native request controls | Yes | Yes | Yes | Yes | Yes | Context, search size, filters, image settings, access mode, location, and output limit are forwarded; whether each changes results is endpoint-dependent. |
| Reference continuation | N/A | Yes | Yes | Yes | Yes | In-process only; TTL, eviction, and restart reset are explicit. |
| `encrypted_output` | N/A | Yes | Yes | Yes | Yes | Retained internally only; hidden from the model and not replayed. |
| Research policy | N/A | Yes | N/A | Opt-in | No | Registered as `codex-web:codex-web-research`. |

Live rows mean the configured endpoint returned successful results during the opt-in live suite on the maintainer's environment. They do not prove every upstream account, gateway, league, or future backend behaves identically.

## Observed native Codex behavior and intentional differences

Independent native-tool testing on 2026-08-30 used only the first-party callable tool. It observed:

Official references:

- [Codex search command types](https://github.com/openai/codex/blob/main/codex-rs/codex-api/src/search.rs)
- [Codex web tool usage guidance](https://github.com/openai/codex/blob/main/codex-rs/ext/web-search/web_run_description.md)

- Continuation exposed only returned references to the model. No request ID, session ID, thread ID, prior response, or `encrypted_output` parameter was exposed.
- Search → open and open → find succeeded. Click was page-dependent: one documentation-page link failed while a simple IANA-page link succeeded.
- Opening a public PDF then screenshotting page `0` succeeded. An out-of-range page, unopened PDF URL, and opened non-PDF page were rejected.
- Native continuation survived 30+ intervening tool calls in one conversation. Its internal keying, TTL, and eviction behavior remain hidden.
- The published guidance says four search queries maximum and `medium` or `long` above three queries, but the tested runtime accepted five queries and four queries with `short`.
- Empty command arrays were rejected. Empty `q` values were accepted. Empty `domains` arrays were accepted.
- Unknown root and nested fields were accepted and discarded.
- `sports.tool` appeared optional in callable metadata, but omitting it failed in the tested resolver. Supplying `tool: "sports"` succeeded.
- The callable schema exposed `context`, `search_context_size`, domain filters, `image_settings`, `external_web_access`, `user_location`, and `max_output_tokens` to the model.
- Native output was rendered text. The adapter's structured JSON envelope and derived `sources` list are adapter contracts, not native raw-response parity.

An authenticated maintainer test also sent those request controls through a multi-hop reverse-proxy deployment. The endpoint accepted the combined request and returned output. This verifies transport compatibility for that deployment, not that every gateway forwards the fields unchanged or that every field materially affects ranking.

The adapter intentionally remains stricter than those observations: it rejects empty `q`, unknown fields, more than four search queries, invalid response-length combinations, non-exact response-length casing, and integers above unsigned 64-bit range. These are deliberate safety and predictability policies, not claims about native runtime enforcement. `encrypted_output` is retained internally only and never exposed to Hermes.

## Hermes CLI/gateway verification boundary

The real installed Hermes `AIAgent` and tool-executor path is covered by `tests/test_hermes_e2e.py`, including direct assertion that the plugin receives the runtime-generated `agent.session_id`.

A full `hermes chat` CLI turn was attempted against temporary local model and HTTPS web stubs. The exact blocker was the temporary model stub's Responses SSE function-call event sequence: Hermes reached the model and received a final response, but the stub event sequence was not accepted as an executable function call, so no CLI-level continuation claim is made. This is a test-harness limitation, not a claim that the installed provider cannot execute tools. Telegram and Discord were not live-tested because doing so would require operating connected gateway sessions and external credentials. No production gateway was changed.

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
- Reverse proxies may inspect, rewrite, ignore, reject, log, retain, or bill request controls differently.
- The adapter does not provide `web_extract`; use Hermes `web_extract` for general extraction.
- `sources` is an adapter normalization, not a claim that native Codex returns this exact shape.

## Responsible use

This project is an independent compatibility adapter for endpoints that users are authorized to access. It does not provide credentials, bypass access controls or rate limits, or grant access to any upstream service. Requests and credentials are handled by the configured endpoint and any intermediaries behind it. Users are responsible for trusting their gateway operator, understanding its data and billing practices, complying with provider terms and applicable laws, and keeping credentials out of source control.

This project is not affiliated with or endorsed by OpenAI or Nous Research.

## Public-repository rule

Do not commit production domains, internal IDs, customer data, credentials, tokens, or secret-bearing logs.

## License

MIT
