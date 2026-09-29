import asyncio
import logging
from dotenv import load_dotenv
from band import Agent, configure_logging
from band.config import load_agent_config
from band.core import AgentToolsProtocol, SimpleAdapter
from band.core.types import PlatformMessage

logger = logging.getLogger(__name__)

# Top-level keys in agent_config.yaml.
SEATS = ("planner", "executor", "reconciler")


class EchoAdapter(SimpleAdapter[list]):
    """Smallest adapter `Agent.create` accepts. Your adapter tutorial replaces it."""

    async def on_message(
        self,
        msg: PlatformMessage,
        tools: AgentToolsProtocol,
        history: list,
        participants_msg: str | None,
        contacts_msg: str | None,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        await tools.send_message(f"echo: {msg.content}")


async def verify_seat(seat: str) -> None:
    agent_id, api_key = load_agent_config(seat)
    logger.info("[%s] Loaded agent: %s", seat, agent_id)

    agent = Agent.create(
        adapter=EchoAdapter(),
        agent_id=agent_id,
        api_key=api_key,
    )

    # start() makes the first authenticated call to the platform.
    await agent.start()
    logger.info("[%s] Connected as: %s", seat, agent.agent_name)
    await agent.stop()


async def verify_setup() -> None:
    load_dotenv()
    configure_logging(root_level="INFO")

    for seat in SEATS:
        await verify_seat(seat)
    logger.info("Setup verified for %d seats.", len(SEATS))


asyncio.run(verify_setup())
