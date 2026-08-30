"""Hermes plugin exposing the Codex web command surface as one tool."""

from pathlib import Path

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
    ctx.register_skill(
        name="codex-web-research",
        path=Path(__file__).with_name("SKILL.md"),
        description="Codex-style source-grounded research policy.",
    )
