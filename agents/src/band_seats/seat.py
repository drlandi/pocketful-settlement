"""Shared runner: one process per seat, its own credentials, its mandate as prompt."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from band import Agent, configure_logging
from band.adapters.anthropic import AnthropicAdapter
from band.config import load_agent_config
from band.runtime.custom_tools import CustomToolDef

logger = logging.getLogger(__name__)

DEFAULT_MANDATES_DIR = "~/dev/hackathons/pocketful-settlement/mandates"
DEFAULT_MODEL = "claude-sonnet-5-5"


def load_mandate(seat: str) -> str:
    """Read the mandate verbatim. It is never edited or templated here."""
    mandates_dir = Path(os.environ.get("MANDATES_DIR", DEFAULT_MANDATES_DIR)).expanduser()
    return (mandates_dir / f"{seat}.md").read_text(encoding="utf-8")


async def _run(seat: str, tools: list[CustomToolDef]) -> None:
    load_dotenv()
    configure_logging(root_level="INFO")

    agent_id, api_key = load_agent_config(seat)
    mandate = load_mandate(seat)
    logger.info("[%s] agent %s, mandate %d chars, tools: %s",
                seat, agent_id, len(mandate), [m.__name__ for m, _ in tools])

    # prompt= (not system_prompt=) keeps the SDK base instructions, which teach
    # the model to reply through band_send_message; the mandate follows them.
    adapter = AnthropicAdapter(
        model=os.environ.get("SEAT_MODEL", DEFAULT_MODEL),
        prompt=mandate,
        additional_tools=tools,
    )
    agent = Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)
    await agent.run()


def run_seat(seat: str, tools: list[CustomToolDef]) -> None:
    asyncio.run(_run(seat, tools))
