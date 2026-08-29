# Hermes Codex Web

A standalone Hermes Agent plugin that exposes the Codex-compatible `/alpha/search` web command surface as one model tool: `codex_web`.

## What this is

```text
Hermes model
  -> codex_web
  -> configured /alpha/search endpoint
  -> structured output and results
  -> same Hermes model continues
```

This plugin does not modify Hermes core, replace `web_search`, run a second answer model, or extract page content. Keep `web_extract` on a separate extraction backend such as Firecrawl.

## Commands

`codex_web` supports the complete public `SearchCommands` surface:

| Field | Use |
| --- | --- |
| `search_query` | Search the web. Each item accepts `q`, optional `recency`, and optional `domains`. |
| `image_query` | Search for images. Same query fields as `search_query`. |
| `open` | Open a URL or a returned reference ID. Optional `lineno`. |
| `click` | Open a numbered link from an opened page using `ref_id` and link `id`. |
| `find` | Find a text pattern in a URL or returned reference ID. |
| `screenshot` | Screenshot a PDF page using `ref_id` and zero-based `pageno`. |
| `finance` | Look up `equity`, `fund`, `crypto`, or `index` assets. |
| `weather` | Forecast by location, with optional start date and duration. |
| `sports` | NBA, WNBA, NFL, NHL, MLB, EPL, NCAAMB, NCAAWB, or IPL schedules/standings. |
| `time` | Get time for UTC offsets such as `+07:00`. |

### Request controls

| Field | Values / behavior |
| --- | --- |
| `response_length` | `short`, `medium`, or `long`. More than three `search_query` items require `medium` or `long`. |
| `search_context_size` | `low`, `medium`, or `high`. |
| `allowed_domains` | Restrict web results to these domains. |
| `blocked_domains` | Exclude these domains. |
| `image_settings` | `max_results` and/or `caption`. |
| `external_web_access` | `true`, `false`, `cached`, `indexed`, or `live`. |
| `user_location` | Approximate `country`, `region`, `city`, and `timezone`. |
| `max_output_tokens` | Positive output budget. |
| `context` | Optional focused text input for the request. |

The tool automatically sends `allowed_callers: ["direct"]` because Hermes invokes it directly. The routing model is configuration, not a tool argument.

## Installation

```bash
hermes plugins install https://github.com/duu261/hermes-codex-web.git --enable
```

Restart the Hermes surface that will use the plugin:

```bash
hermes gateway restart
```

For a current gateway session, restart from a separate terminal because a gateway cannot safely restart itself from inside its own turn.

## Configuration

Store credentials in Hermes' private environment file, never in this repository:

```text
CODEX_WEB_BASE_URL=https://gateway.example/v1
CODEX_WEB_API_KEY=replace-me
```

The base URL must include `/v1`; the plugin appends `/alpha/search`.

Set the non-secret routing model in `config.yaml` through Hermes CLI:

```bash
hermes config set web.codex_web.model gpt-5.4-mini
```

If `web.codex_web.model` is unset, the default is `gpt-5.4-mini`.

For migration from the basic `hermes-codex-search` plugin, `CODEX_SEARCH_BASE_URL` and `CODEX_SEARCH_API_KEY` are accepted as fallbacks. Dedicated `CODEX_WEB_*` values take precedence.

## Choosing the tool

Use the normal Hermes tool for ordinary provider-neutral search:

```text
web_search
```

Use `codex_web` when the task needs Codex-specific operations:

```text
codex_web with search_query
codex_web with open
codex_web with find
codex_web with click
codex_web with screenshot
codex_web with finance, weather, sports, or time
```

For PDF screenshots, call `open` first, then call `screenshot` with the returned `ref_id` in the same conversation. The plugin reuses the Codex request ID per Hermes session so the upstream page context is preserved.

The model can discover the tool from its schema, but explicit wording is safest when a specific command is required:

```text
Use codex_web. Search for the official Python 3.13 release notes,
open the official result, and find the section about free-threaded mode.
```

## Examples

### Web search

```json
{
  "search_query": [
    {
      "q": "official Python 3.13 release notes",
      "recency": 30,
      "domains": ["python.org"]
    }
  ],
  "response_length": "short"
}
```

### Open and inspect a page

```json
{
  "open": [
    {"ref_id": "https://www.python.org/downloads/release/python-3130/"}
  ],
  "find": [
    {
      "ref_id": "https://www.python.org/downloads/release/python-3130/",
      "pattern": "free-threaded"
    }
  ]
}
```

### Specialized lookup

```json
{
  "finance": [
    {"ticker": "BTC", "type": "crypto", "market": ""}
  ],
  "weather": [
    {"location": "Vietnam, Ho Chi Minh City", "duration": 1}
  ],
  "time": [
    {"utc_offset": "+07:00"}
  ]
}
```

### Response shape

```json
{
  "success": true,
  "output": "human-readable endpoint output",
  "results": [
    {
      "type": "text_result",
      "ref_id": "turn0search0",
      "title": "Example",
      "url": "https://example.com"
    }
  ]
}
```

Specialized commands can return useful text in `output` with an empty `results` list. HTTP 200 error text, missing result lists, malformed responses, and transport failures are returned as `success: false` instead of fake success.

## Verified capability matrix

The adapter has been exercised through the live Hermes tool path:

| Capability | Status | Note |
| --- | --- | --- |
| `search_query` | Verified | Returns structured web results. |
| `image_query` | Verified | Returns image findings in endpoint output. |
| `open` | Verified | Opens a URL and returns page content. |
| `find` | Verified | Finds text in a URL. |
| `finance` | Verified | Specialized text output; `results` may be empty. |
| `weather` | Verified | Specialized text output; `results` may be empty. |
| `sports` | Verified | Adapter supplies the required `tool: "sports"` value. |
| `time` | Verified | Specialized text output; `results` may be empty. |
| `click` | Endpoint-dependent | Requires a valid reference ID and numbered link in the endpoint's request context. Stateless standalone calls may be rejected. |
| `screenshot` | Endpoint-dependent | Open the PDF first, then reuse its `ref_id` in the same Hermes conversation; deployments without PDF screenshot support reject it. |

A `success: true` response is not claimed for an upstream error string. The adapter converts recognized HTTP-200 endpoint errors into `success: false`.

## Boundaries

- This is an adapter around the endpoint, not the native Codex runtime.
- Hermes does not provide native conversation-item input to plugin tools, so `context` is plain text only.
- `reasoning` and native runtime headers are intentionally not exposed.
- The plugin does not own a browser session; it preserves a bounded in-process Alpha Search request context per Hermes conversation for follow-up reference IDs. Context is lost after eviction or process restart.
- Endpoint availability still depends on the configured gateway and its upstream account/pool permissions.
- Command support is verified against the current endpoint path, but an upstream deployment may reject a command it has disabled.

## Development and verification

The project uses only the Python standard library:

```bash
python -m unittest discover -s tests -v
python -m py_compile provider.py __init__.py
git diff --check
hermes plugins doctor . --ci
```

The test suite covers payload construction, command validation, response normalization, HTTP failures, manifest requirements, and public schema parity.

## Public-repository rule

Do not commit production domains, internal channel IDs, customer data, credentials, tokens, or secret-bearing logs. Use placeholders in documentation and keep secrets in Hermes' private environment file.

## License

MIT
