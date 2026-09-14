"""Weboberfläche für die Agenten: Discovery ansehen und den Orchestrator fragen.

Die UI ist ein Extra im Sinne von `agents_server.build_app(extras=[...])`: Sie
bringt ihre Routen selbst mit, und der Server weiß nicht, was dahintersteckt –
dasselbe Prinzip wie bei den Agenten.

    GET  /              die Seite (web/index.html)
    GET  /api/status    Ist das Sprachmodell schon geladen?
    GET  /api/agents    Discovery: alle Visitenkarten mit ihren Skills
    POST /api/query     {"text": "..."} -> Routing durch das LLM + Antwort des Agenten

Wichtig: Die Discovery läuft auch hier über HTTP gegen die eigenen
`/.well-known/agent-card.json`-Endpunkte. Die Oberfläche sieht die Agenten also
genauso, wie ein fremder Client sie sähe – sie liest nicht intern in den
Objekten nach.
"""

import asyncio
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from a2a.client import A2ACardResolver
from a2a.types import AgentCard
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH

from agent_c_orchestrator import AGENTS, LLM_MODEL, LokalesLLM, beantworte

SEITE = Path(__file__).with_name("web") / "index.html"


def card_als_dict(card: AgentCard, basis: str) -> dict:
    """Reduziert eine Visitenkarte auf das, was die Oberfläche anzeigt."""
    url = card.supported_interfaces[0].url
    return {
        "name": card.name,
        "beschreibung": card.description,
        "pfad": url[len(basis):] if url.startswith(basis) else url,
        "url": url,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "beschreibung": s.description,
                "beispiele": list(s.examples),
                "tags": list(s.tags),
            }
            for s in card.skills
        ],
    }


class WebUI:
    """Hält Modell, HTTP-Client und die gefundenen Visitenkarten."""

    def __init__(self) -> None:
        self.basis = ""
        self.llm: LokalesLLM | None = None
        self.llm_fehler = ""
        self._llm_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None
        self._cards: list[AgentCard] = []
        # Das Modell kann immer nur eine Anfrage auf einmal bearbeiten.
        self._llm_sperre = asyncio.Lock()

    # ---- Routen und Lebenszyklus ------------------------------------------
    def routen(self, base_url: str) -> list[Route]:
        self.basis = base_url.rstrip("/")
        return [
            Route("/", self.seite),
            Route("/api/status", self.api_status),
            Route("/api/agents", self.api_agents),
            Route("/api/query", self.api_query, methods=["POST"]),
        ]

    async def beim_start(self) -> None:
        self._http = httpx.AsyncClient(timeout=60.0)
        # Das Laden dauert Minuten. Im Hintergrund, damit die Seite sofort da ist
        # und der Fortschritt über /api/status sichtbar wird.
        self._llm_task = asyncio.create_task(self._llm_laden(), name="web-llm-laden")
        print(f"[WebUI] Oberfläche unter {self.basis}/")

    async def _llm_laden(self) -> None:
        try:
            self.llm = await asyncio.to_thread(LokalesLLM)
        except asyncio.CancelledError:
            raise
        except BaseException as err:  # noqa: BLE001 – der UI melden statt abstürzen
            self.llm_fehler = f"{type(err).__name__}: {err}"
            print(f"[WebUI] Modell konnte nicht geladen werden: {self.llm_fehler}")

    async def beim_stopp(self) -> None:
        if self._llm_task and not self._llm_task.done():
            self._llm_task.cancel()
        if self._http:
            await self._http.aclose()

    # ---- Discovery ---------------------------------------------------------
    async def _karten(self, neu: bool = False) -> tuple[list[AgentCard], list[str]]:
        """Visitenkarten holen (einmal, dann gemerkt). Liefert auch die Ausfälle."""
        if self._cards and not neu:
            return self._cards, []

        cards: list[AgentCard] = []
        fehler: list[str] = []
        for agent in AGENTS:
            pfad = agent["path"]
            try:
                resolver = A2ACardResolver(
                    httpx_client=self._http,
                    base_url=self.basis,
                    agent_card_path=f"{pfad}{AGENT_CARD_WELL_KNOWN_PATH}",
                )
                cards.append(await resolver.get_agent_card())
            except Exception as err:  # noqa: BLE001 – ein Ausfall darf die Liste nicht kippen
                fehler.append(f"{pfad} ({type(err).__name__})")
        self._cards = cards
        return cards, fehler

    # ---- Endpunkte ---------------------------------------------------------
    async def seite(self, request: Request) -> FileResponse:
        return FileResponse(SEITE, media_type="text/html; charset=utf-8")

    async def api_status(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "llm_bereit": self.llm is not None,
            "llm_fehler": self.llm_fehler,
            "modell": LLM_MODEL,
        })

    async def api_agents(self, request: Request) -> JSONResponse:
        cards, fehler = await self._karten(neu=request.query_params.get("neu") == "1")
        return JSONResponse({
            "agenten": [card_als_dict(c, self.basis) for c in cards],
            "fehler": fehler,
        })

    async def api_query(self, request: Request) -> JSONResponse:
        try:
            daten = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"fehler": "Ungültiges JSON im Rumpf."}, status_code=400)

        text = str(daten.get("text", "")).strip()
        if not text:
            return JSONResponse({"fehler": "Leere Anfrage."}, status_code=400)
        if self.llm_fehler:
            return JSONResponse({"fehler": f"Sprachmodell nicht verfügbar: {self.llm_fehler}"}, status_code=503)
        if self.llm is None:
            return JSONResponse({"fehler": "Das Sprachmodell wird noch geladen. Bitte kurz warten."}, status_code=503)

        cards, _ = await self._karten()
        if not cards:
            return JSONResponse({"fehler": "Keine Agenten gefunden."}, status_code=503)

        try:
            async with self._llm_sperre:
                ergebnis = await beantworte(self._http, self.llm, cards, text)
        except ValueError as err:  # LLM nannte keine bekannte Skill-ID
            return JSONResponse({"fehler": str(err)}, status_code=502)
        except httpx.HTTPError as err:
            return JSONResponse({"fehler": f"Agent nicht erreichbar: {err}"}, status_code=502)
        return JSONResponse(ergebnis)
