import asyncio

import httpx

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_message_text, new_text_message
from a2a.types import Role, SendMessageRequest, TaskState
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH

# Agent B und Agent C laufen auf demselben Server, jeweils unter einem eigenen Pfad
SERVER_URL = "http://127.0.0.1:9999"
AGENT_B_PATH = "/greeter"
AGENT_C_PATH = "/adder"


async def ask_agent(httpx_client: httpx.AsyncClient, agent_path: str, text: str) -> None:
    """Holt die Visitenkarte eines Agenten, schickt ihm einen Text und druckt die Antwort."""
    # 1. Visitenkarte holen – sie liegt unter <server>/<agent_path>/.well-known/agent-card.json
    resolver = A2ACardResolver(
        httpx_client=httpx_client,
        base_url=SERVER_URL,
        agent_card_path=f"{agent_path}{AGENT_CARD_WELL_KNOWN_PATH}",
    )
    card = await resolver.get_agent_card()
    print(f"\nGefunden: {card.name} – {card.description}")
    print("Fähigkeiten:", ", ".join(s.name for s in card.skills))

    # 2. Client erzeugen (hier ohne Streaming, einfacher Anfang)
    #    Die Ziel-URL für Anfragen steht in der Visitenkarte (supported_interfaces)
    config = ClientConfig(streaming=False, httpx_client=httpx_client)
    client = await create_client(agent=card, client_config=config)

    # 3. Nachricht bauen und senden
    message = new_text_message(text, role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)

    print(f"Agent A sendet: {text}")
    print(f"Antwort von {card.name}:")
    async for response in client.send_message(request):
        if not response.HasField("task"):
            print("  ", response)
            continue
        task = response.task
        if task.status.state == TaskState.TASK_STATE_FAILED:
            print("   FEHLER:", get_message_text(task.status.message))
            continue
        for artifact in task.artifacts:
            print("  ", get_artifact_text(artifact))


async def main() -> None:
    async with httpx.AsyncClient() as httpx_client:
        await ask_agent(httpx_client, AGENT_B_PATH, "Ich bin Agent A und möchte dir Hallo sagen")
        await ask_agent(httpx_client, AGENT_C_PATH, "Bitte addiere 17 und 25")
        await ask_agent(httpx_client, AGENT_C_PATH, "Was ist 3,5 + 4?")
        await ask_agent(httpx_client, AGENT_C_PATH, "Addiere 1, 2 und 3")  # absichtlich ungültig


if __name__ == "__main__":
    asyncio.run(main())
