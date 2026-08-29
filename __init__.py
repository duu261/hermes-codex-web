"""Hermes plugin exposing the Codex web command surface as one tool."""

from .provider import CODEX_WEB_SCHEMA, handle_codex_web, is_available


def register(ctx) -> None:
    ctx.register_tool(
        name="codex_web",
        toolset="web",
        schema=CODEX_WEB_SCHEMA,
        handler=handle_codex_web,
        check_fn=is_available,
        description=CODEX_WEB_SCHEMA["description"],
        emoji="🌐",
    )
