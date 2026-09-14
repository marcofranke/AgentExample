"""Ein Server, der alle Agenten gemeinsam bereitstellt.

Der Server weiß NICHTS über die einzelnen Agenten. Jeder Agent bringt seinen
Pfad, seinen Skill und seine AgentCard selbst mit (siehe z. B. AdderAgent.py).
Der Server fragt nur: "Wo willst du hin, und wie sieht deine Visitenkarte aus?"

Erreichbar sind die Agenten unter:
    http://127.0.0.1:9999/<PATH>                            -> JSON-RPC-Endpunkt
    http://127.0.0.1:9999/<PATH>/.well-known/agent-card.json -> Visitenkarte

Einen neuen Agenten anschließen = eine Zeile in AGENTEN ergänzen.
"""

import os
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH

from AdderAgentExecutor import AdderAgentExecutor
from GreenAgentExecuter import GreeterAgentExecutor
from OntologySearchAgentExecutor import OntologySearchAgentExecutor
from ResearchAgentExecutor import ResearchAgentExecutor
from SemanticMediatorAgentExecutor import SemanticMediatorAgentExecutor
from SubtractorAgentExecutor import SubtractorAgentExecutor
from WeatherAgentExecutor import WeatherAgentExecutor

# Im Container muss der Server auf 0.0.0.0 lauschen, damit Anfragen von außen
# ankommen. BASE_URL bleibt davon getrennt: Diese Adresse steht in den
# Visitenkarten und wird vom Orchestrator im selben Prozess aufgerufen.
HOST = os.environ.get("A2A_HOST", "127.0.0.1")
PORT = int(os.environ.get("A2A_PORT", "9999"))
BASE_URL = os.environ.get("A2A_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

# Alle Agenten, die dieser Server hosten soll. Jeder Executor hält in `.agent`
# die Fachlogik, und die kennt ihren eigenen PATH und ihre agent_card().
AGENTEN: list[AgentExecutor] = [
    GreeterAgentExecutor(),
    AdderAgentExecutor(),
    SubtractorAgentExecutor(),
    WeatherAgentExecutor(),
    OntologySearchAgentExecutor(),
    SemanticMediatorAgentExecutor(),
    ResearchAgentExecutor(),
]


def mount_agent(executor: AgentExecutor) -> list[Route]:
    """Erzeugt Visitenkarten- und JSON-RPC-Routen für einen Agenten.

    Pfad und Visitenkarte holt sich der Server vom Agenten selbst.
    """
    agent = executor.agent
    path = agent.PATH
    agent_card = agent.agent_card(BASE_URL)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    routes: list[Route] = []
    routes.extend(create_agent_card_routes(agent_card, card_url=f"{path}{AGENT_CARD_WELL_KNOWN_PATH}"))
    routes.extend(create_jsonrpc_routes(handler, rpc_url=path))
    print(f"[Server] {agent_card.name:24} -> {BASE_URL}{path}  (Skill: {agent.SKILL.id})")
    return routes


def build_app(extras: list = ()) -> Starlette:
    """Baut die Anwendung aus allen Agenten und optionalen Zusatz-Bausteinen.

    Ein Extra ist alles, was `routen(base_url)` anbietet – zum Beispiel die
    Weboberfläche aus web_ui.py. Der Server kennt sie so wenig wie die Agenten.
    """
    routes: list[Route] = []
    for executor in AGENTEN:
        routes.extend(mount_agent(executor))
    for extra in extras:
        routes.extend(extra.routen(BASE_URL))

    # Bausteine mit eigenem Lebenszyklus (z. B. Gedächtnis aufbauen, MCP-Server
    # starten, Modell laden) bieten optional `beim_start()` und `beim_stopp()` an.
    # Der Server ruft sie auf, ohne zu wissen, was dahintersteckt.
    bausteine = [executor.agent for executor in AGENTEN] + list(extras)
    starts = [b.beim_start for b in bausteine if hasattr(b, "beim_start")]
    stopps = [b.beim_stopp for b in bausteine if hasattr(b, "beim_stopp")]

    @asynccontextmanager
    async def lebenszyklus(app: Starlette):
        for start in starts:
            await start()
        yield
        for stopp in stopps:
            await stopp()

    return Starlette(routes=routes, lifespan=lebenszyklus)


if __name__ == "__main__":
    uvicorn.run(build_app(), host=HOST, port=PORT)
