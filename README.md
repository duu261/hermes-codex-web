# Hermes Codex Web

A standalone Hermes plugin exposing the Codex-compatible `/alpha/search` command surface as `codex_web`.

## Scope

The tool supports:

- `search_query` and `image_query`
- `open`, `click`, and `find`
- PDF `screenshot`
- `finance`, `weather`, `sports`, and `time`
- response length, search context size, domain filters, image settings, location, and external web access mode

It calls the configured endpoint directly and returns its structured `output` and `results`. It does not patch Hermes core, replace `web_search`, or provide page extraction.

## Install

```bash
hermes plugins install <repository-url> --enable
```

Set secrets in Hermes' private environment file:

```text
CODEX_WEB_BASE_URL=https://gateway.example/v1
CODEX_WEB_API_KEY=replace-me
```

The base URL must include `/v1`; the plugin appends `/alpha/search`. For migration from the basic `hermes-codex-search` plugin, `CODEX_SEARCH_BASE_URL` and `CODEX_SEARCH_API_KEY` are accepted as fallbacks.

Set the non-secret routing model in `config.yaml` through Hermes CLI:

```bash
hermes config set web.codex_web.model gpt-5.4-mini
```

The default is `gpt-5.4-mini`.

## Tool behavior

The model calls one tool with explicit command fields:

```json
{
  "search_query": [{"q": "official Python 3.13 release notes"}],
  "response_length": "short"
}
```

Use `web_search` for ordinary provider-neutral search. Use `codex_web` when navigation or a specialized Codex web command is needed. Keep `web_extract` on a separate extraction backend such as Firecrawl.

## Development

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
```

Public-repository rule: no production domains, internal channel IDs, customer data, credentials, or secret-bearing logs belong in this repository.
