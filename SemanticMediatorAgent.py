"""Agent H – Semantic Mediator: übersetzt Daten zwischen Datenmodellen über den
Semantischen Mediator des BIBA (Bremer Institut für Produktion und Logistik).

Der Semantische Mediator löst Interoperabilitätsprobleme zwischen heterogenen
Datenquellen: Wrapper bilden die lokalen Sichten der Quellen (SQL, CSV, XML,
REST, Kafka ...) auf eine globale Ontologie ab, und der Mediator transformiert
Daten zwischen diesen Sichten.

Der Mediator ist kein öffentlicher Dienst. Dieser Agent spricht deshalb eine
Instanz an, deren Adresse per Umgebungsvariable gesetzt wird:

    SEMANTIC_MEDIATOR_URL      Basis-URL, z. B. http://localhost:8080
    SEMANTIC_MEDIATOR_TOKEN    optional, wird als Bearer-Token mitgeschickt

Die Endpunkte und das Nachrichtenformat stehen gebündelt in `MediatorClient`.
Weicht die eigene Mediator-Installation davon ab, muss NUR diese Klasse
angepasst werden. Der Rest des Agenten (Textverständnis, A2A-Anbindung)
bleibt unverändert.

Eingabeformat für den Agenten (Beispiele siehe SKILL):
    "Transformiere von <Quellmodell> nach <Zielmodell>: <JSON>"
    "Welche Modelle kennt der Mediator?"
"""

import json
import os
import re

import httpx

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


class MediatorClient:
    """Dünne REST-Hülle um den Semantischen Mediator. Hier die Endpunkte anpassen."""

    # ---- ANPASSEN, falls die eigene Installation andere Pfade nutzt ----------
    PFAD_MODELLE = "/models"          # GET  -> Liste der bekannten Datenmodelle
    PFAD_TRANSFORM = "/transform"     # POST -> Daten von Quell- in Zielmodell übersetzen
    # ---------------------------------------------------------------------------

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("SEMANTIC_MEDIATOR_URL", "")).rstrip("/")
        self.token = token or os.environ.get("SEMANTIC_MEDIATOR_TOKEN")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _pruefe_konfiguration(self) -> None:
        if not self.base_url:
            raise ValueError(
                "SEMANTIC_MEDIATOR_URL ist nicht gesetzt. Beispiel: "
                "set SEMANTIC_MEDIATOR_URL=http://localhost:8080"
            )

    async def modelle(self, http: httpx.AsyncClient) -> list[str]:
        """Liefert die Namen der Datenmodelle, die der Mediator kennt."""
        self._pruefe_konfiguration()
        antwort = await http.get(f"{self.base_url}{self.PFAD_MODELLE}", headers=self._headers())
        antwort.raise_for_status()
        daten = antwort.json()
        # Tolerant: entweder eine Liste von Strings oder Objekte mit "name"/"id"
        if isinstance(daten, dict):
            daten = daten.get("models") or daten.get("items") or list(daten.values())
        return [m if isinstance(m, str) else (m.get("name") or m.get("id") or str(m)) for m in daten]

    async def transformiere(
        self, http: httpx.AsyncClient, quelle: str, ziel: str, daten: dict | list
    ) -> dict | list:
        """Schickt Daten im Quellmodell an den Mediator und erhält sie im Zielmodell zurück."""
        self._pruefe_konfiguration()
        nutzlast = {"sourceModel": quelle, "targetModel": ziel, "data": daten}
        antwort = await http.post(
            f"{self.base_url}{self.PFAD_TRANSFORM}", headers=self._headers(), json=nutzlast
        )
        antwort.raise_for_status()
        ergebnis = antwort.json()
        return ergebnis.get("data", ergebnis) if isinstance(ergebnis, dict) else ergebnis


class SemanticMediatorAgent:
    """Die 'Intelligenz' des Mediator-Agenten: Auftrag aus dem Text lesen, Mediator rufen."""

    PATH = "/mediator"

    SKILL = AgentSkill(
        id="semantic_mediation",
        name="Semantic Mediator",
        description=(
            "Nutzt den Semantischen Mediator des BIBA, um Daten von einem Quell-Datenmodell "
            "in ein Ziel-Datenmodell zu übersetzen (transformieren, mappen, Interoperabilität) "
            "oder die bekannten Datenmodelle aufzulisten."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "biba", "semantic-mediator", "interoperability"],
        examples=[
            'Transformiere von ERP nach AAS: {"artikelnummer": "4711", "menge": 3}',
            "Welche Datenmodelle kennt der Mediator?",
            'Übersetze von CSV-Auftrag nach EDIFACT: {"order_id": 12}',
        ],
    )

    # Modellnamen: Buchstaben, Ziffern, Bindestrich, Punkt. Der Doppelpunkt vor dem JSON gehört nicht dazu.
    _VON_NACH = re.compile(
        r"\bvon\s+([\w\-]+(?:\.[\w\-]+)*)\s+(?:nach|in|zu)\s+([\w\-]+(?:\.[\w\-]+)*)", re.IGNORECASE
    )
    _JSON = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)
    _MODELLE_FRAGE = re.compile(r"\b(modelle|datenmodelle|models|kennt|liste|welche)\b", re.IGNORECASE)

    def __init__(self, client: MediatorClient | None = None) -> None:
        self.client = client or MediatorClient()

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent H – Semantic Mediator",
            description="Ein Agent, der Daten über den Semantischen Mediator des BIBA zwischen Datenmodellen übersetzt.",
            version="0.1.0",
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[
                AgentInterface(
                    protocol_binding=TransportProtocol.JSONRPC,
                    url=f"{base_url}{self.PATH}",
                    protocol_version="1.0",
                )
            ],
            skills=[self.SKILL],
        )

    def auftrag_aus_text(self, text: str) -> dict:
        """Zerlegt den Text in {"art": "modelle"} oder {"art": "transform", "quelle", "ziel", "daten"}."""
        text = text.strip()
        json_match = self._JSON.search(text)
        von_nach = self._VON_NACH.search(text)

        if von_nach and json_match:
            try:
                daten = json.loads(json_match.group(1))
            except json.JSONDecodeError as err:
                raise ValueError(f"Die Daten sind kein gültiges JSON: {err}") from err
            return {"art": "transform", "quelle": von_nach.group(1), "ziel": von_nach.group(2), "daten": daten}

        if self._MODELLE_FRAGE.search(text) and not json_match:
            return {"art": "modelle"}

        raise ValueError(
            "Ich verstehe den Auftrag nicht. Erwartet: "
            '"Transformiere von <Quelle> nach <Ziel>: {json}" oder "Welche Modelle kennt der Mediator?"'
        )

    async def invoke(self, user_request: str) -> str:
        auftrag = self.auftrag_aus_text(user_request)

        async with httpx.AsyncClient(timeout=30) as http:
            if auftrag["art"] == "modelle":
                modelle = await self.client.modelle(http)
                if not modelle:
                    return "Der Mediator kennt derzeit keine Datenmodelle."
                return "Bekannte Datenmodelle des Mediators:\n" + "\n".join(f"  • {m}" for m in modelle)

            ergebnis = await self.client.transformiere(
                http, auftrag["quelle"], auftrag["ziel"], auftrag["daten"]
            )
        return (
            f"Transformation {auftrag['quelle']} -> {auftrag['ziel']}:\n"
            + json.dumps(ergebnis, ensure_ascii=False, indent=2)
        )
