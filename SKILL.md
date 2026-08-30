---
name: codex-web-research
description: Use Codex web for current or explicitly verified claims, then ground the answer in direct sources.
---

# Codex Web Research Policy

- Browse whenever the user explicitly requests search, browsing, verification, or fact checking.
- Browse for current, unstable, time-sensitive, or version-sensitive information instead of relying on memory.
- Prefer primary and authoritative sources. For technical questions, prefer official documentation, standards, and research papers.
- Search broadly first. Open important results, then use `find` and `click` to verify claims when the endpoint supports them.
- Open a PDF before using `screenshot`; PDF page numbers are zero-based.
- Cite direct source URLs near the claims they support. Never expose internal `turn0search0`-style references in the final answer.
- Never cite a search-results page as evidence when a direct source is available.
- Clearly label inferences and report uncertainty when evidence is insufficient.
- Avoid long copied passages and unnecessary quotations.
- Keep `web_extract` as the general extraction fallback. Do not silently replace it with `codex_web`.
- Use the same Hermes model after tool results arrive. Do not delegate final answering to a second model.
