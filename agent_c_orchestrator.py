"""Agent C – der LLM-Orchestrator (Teil 2 des A2A-Tutorials), mit LOKALEM LLM.

Agent C hat selbst keine Fachlogik. Er
  1. holt sich beim Start die AgentCards aller bekannten Agenten,
  2. lässt ein kleines, lokal laufendes LLM von Hugging Face anhand der
     Skill-Beschreibungen entscheiden, welcher Agent zuständig ist,
  3. ruft genau diesen Agenten per A2A auf und zeigt dessen Antwort.

Anpassung gegenüber dem PDF:
  * Greeter und Adder laufen hier auf EINEM Server (agents_server.py, Port 9999)
    unter eigenen Pfaden, deshalb steht in AGENTS Basis-URL + Pfad.
  * Statt der Anthropic-API wird ein ~2B-Modell über `transformers` direkt auf
    dem eigenen Rechner ausgeführt. Kein API-Key, keine Cloud, aber: der erste
    Start lädt ca. 3 GB herunter, und ein so kleines Modell ist weniger
    zuverlässig – deshalb ist das Parsen der Antwort bewusst tolerant gebaut.

Voraussetzungen:
    pip install torch transformers accelerate
    python agents_server.py                  # Terminal 1
    python agent_c_orchestrator.py           # Terminal 2
"""

import asyncio
import json
import os
import re
import sys

import httpx
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_message_text, new_text_message
from a2a.types import AgentCard, Role, SendMessageRequest, TaskState
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH

SERVER_URL = os.environ.get(
    "A2A_BASE_URL", f"http://127.0.0.1:{os.environ.get('A2A_PORT', '9999')}"
).rstrip("/")

# Alle Agenten, die der Orchestrator kennt. Neue Agenten: einfach anhängen.
AGENTS = [
    {"base_url": SERVER_URL, "path": "/greeter"},  # Agent B – Greeter
    {"base_url": SERVER_URL, "path": "/adder"},    # Adder (im PDF "Agent D")
    {"base_url": SERVER_URL, "path": "/subtractor"},  # Agent E – Subtractor
    {"base_url": SERVER_URL, "path": "/weather"},     # Agent F – Weather
    {"base_url": SERVER_URL, "path": "/ontology"},    # Agent G – Ontology Search
    {"base_url": SERVER_URL, "path": "/mediator"},    # Agent H – Semantic Mediator
    {"base_url": SERVER_URL, "path": "/research"},    # Agent I – Research (MCP)
]

# Lokales Modell von Hugging Face. Alternativen (gleiche Größenklasse):
#   "google/gemma-2-2b-it"                  -> gated, braucht `huggingface-cli login`
#   "HuggingFaceTB/SmolLM2-1.7B-Instruct"   -> frei
#   "Qwen/Qwen3-1.7B"                       -> frei, braucht enable_thinking=False
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")

# float32 braucht rund 6 GB Arbeitsspeicher für ein 1,5-B-Modell. Auf einem
# kleinen Server halbiert LLM_DTYPE=bfloat16 den Bedarf (etwas langsamer auf
# CPUs ohne AVX-512/AMX, sonst unauffällig).
LLM_DTYPE = os.environ.get("LLM_DTYPE", "float32")


# ---------------------------------------------------------------------------
# Das lokale LLM – ersetzt den Anthropic-Client aus dem PDF
# ---------------------------------------------------------------------------
class LokalesLLM:
    """Lädt ein Hugging-Face-Chatmodell einmal in den Speicher und beantwortet Prompts."""

    def __init__(self, model_name: str = LLM_MODEL) -> None:
        print(f"[LLM] Lade {model_name} (erster Start lädt das Modell herunter)...")
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(
            model_name,
            dtype=getattr(torch, LLM_DTYPE),   # CPU-sicher; auf GPU zusätzlich device_map="auto"
        )
        self.model.eval()
        print(f"[LLM] Bereit ({sum(p.numel() for p in self.model.parameters()) / 1e9:.2f} Mrd. Parameter)")

    def generate(self, system: str, user: str, max_new_tokens: int = 80) -> str:
        """Ein Chat-Turn: System-Anweisung + Nutzer-Prompt rein, Modelltext raus."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Das Chat-Template formatiert die Nachrichten so, wie das Modell es beim
        # Training gesehen hat (Rollen-Marker usw.). Ohne das antwortet es Unsinn.
        inputs = self.tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        )
        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,          # deterministisch: immer die wahrscheinlichste Fortsetzung
                pad_token_id=self.tokenizer.eos_token_id,
            )
        # Nur die NEU erzeugten Tokens dekodieren, nicht den Prompt.
        new_tokens = output_ids[0, inputs["input_ids"].shape[1]:]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


# ---------------------------------------------------------------------------
# Schritt 1: Discovery – AgentCards einsammeln
# ---------------------------------------------------------------------------
async def lade_agent_cards(httpx_client: httpx.AsyncClient) -> list[AgentCard]:
    """Holt die Visitenkarte (AgentCard) jedes bekannten Agenten."""
    cards: list[AgentCard] = []
    for agent in AGENTS:
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url=agent["base_url"],
            agent_card_path=f"{agent['path']}{AGENT_CARD_WELL_KNOWN_PATH}",
        )
        card = await resolver.get_agent_card()
        cards.append(card)
        skills = ", ".join(s.id for s in card.skills)
        print(f"[Orchestrator] Gefunden: {card.name} – Skills: {skills}")
    return cards


def card_url(card: AgentCard) -> str:
    """Die URL, unter der der Agent JSON-RPC-Anfragen entgegennimmt."""
    return card.supported_interfaces[0].url


def skill_index(cards: list[AgentCard]) -> dict[str, AgentCard]:
    """skill_id -> AgentCard. Das LLM muss nur eine Skill-ID nennen, die URL leiten wir ab."""
    return {skill.id: card for card in cards for skill in card.skills}


def cards_als_dicts(cards: list[AgentCard]) -> list[dict]:
    """Reduziert die AgentCards auf das, was das LLM zum Entscheiden braucht."""
    return [
        {
            "agent": card.name,
            "skills": [
                {"skill_id": s.id, "description": s.description, "examples": list(s.examples)}
                for s in card.skills
            ],
        }
        for card in cards
    ]


# ---------------------------------------------------------------------------
# Schritt 2: Routing – das lokale LLM entscheidet
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Du bist ein Router für Agenten-Anfragen. Du antwortest ausschließlich mit "
    'einem JSON-Objekt der Form {"skill_id": "..."} und sonst nichts.'
)


def baue_routing_prompt(nutzer_text: str, cards: list[AgentCard]) -> str:
    skill_uebersicht = json.dumps(cards_als_dicts(cards), ensure_ascii=False, indent=2)
    erlaubt = ", ".join(f'"{sid}"' for sid in skill_index(cards))
    return f"""Verfügbare Agenten und ihre Fähigkeiten:
{skill_uebersicht}

Nutzeranfrage: "{nutzer_text}"

Welcher Skill passt am besten zur Nutzeranfrage? Erlaubte Werte für skill_id: {erlaubt}.
Antworte nur mit dem JSON-Objekt."""


def parse_entscheidung(antwort: str, bekannte_skills: list[str]) -> str:
    """Tolerantes Parsen: kleine Modelle halten sich nicht immer exakt ans Format.

    1. Versuch: ein JSON-Objekt im Text finden und "skill_id" lesen.
    2. Fallback: irgendeine bekannte Skill-ID als ganzes Wort im Text.
    """
    match = re.search(r"\{.*?\}", antwort, re.DOTALL)
    if match:
        try:
            skill_id = json.loads(match.group(0)).get("skill_id")
            if skill_id in bekannte_skills:
                return skill_id
        except json.JSONDecodeError:
            pass
    for skill_id in bekannte_skills:
        if re.search(rf"\b{re.escape(skill_id)}\b", antwort):
            return skill_id
    raise ValueError(f"Konnte keine bekannte Skill-ID aus der LLM-Antwort lesen: {antwort!r}")


async def entscheide_zustaendigen_agenten(
    llm: LokalesLLM, nutzer_text: str, cards: list[AgentCard]
) -> dict:
    """Fragt das lokale LLM und liefert {"skill_id", "url", "roh"} zurück."""
    prompt = baue_routing_prompt(nutzer_text, cards)
    # Die Generierung ist reine CPU-Arbeit und würde die asyncio-Schleife blockieren,
    # deshalb läuft sie in einem Hintergrund-Thread.
    antwort = await asyncio.to_thread(llm.generate, SYSTEM_PROMPT, prompt)
    index = skill_index(cards)
    skill_id = parse_entscheidung(antwort, list(index))
    return {"skill_id": skill_id, "url": card_url(index[skill_id]), "roh": antwort}


def finde_card(entscheidung: dict, cards: list[AgentCard]) -> AgentCard:
    """Sicherheitsnetz: nur Agenten akzeptieren, die wir wirklich kennen."""
    for card in cards:
        if card_url(card) == entscheidung["url"]:
            return card
    bekannte = [card_url(c) for c in cards]
    raise ValueError(f"Unbekannte URL: {entscheidung['url']!r}. Bekannt: {bekannte}")


# ---------------------------------------------------------------------------
# Schritt 3: Delegation – den gewählten Agenten per A2A aufrufen
# ---------------------------------------------------------------------------
async def agent_antwort(
    httpx_client: httpx.AsyncClient, card: AgentCard, nutzer_text: str
) -> dict:
    """Ruft den Agenten auf und liefert das Ergebnis als Daten – ohne Ausgabe.

    Dieselben Schritte wie in Agent A.py, nur dass hier nichts gedruckt wird.
    Deshalb kann auch die Weboberfläche (web_ui.py) darauf aufsetzen.
    """
    config = ClientConfig(streaming=False, httpx_client=httpx_client)
    client = await create_client(agent=card, client_config=config)

    message = new_text_message(nutzer_text, role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)

    antworten: list[str] = []
    fehler = ""
    async for response in client.send_message(request):
        if not response.HasField("task"):
            continue
        task = response.task
        if task.status.state == TaskState.TASK_STATE_FAILED:
            fehler = get_message_text(task.status.message)
            continue
        for artifact in task.artifacts:
            antworten.append(get_artifact_text(artifact))
    return {"antworten": antworten, "fehler": fehler}


async def frage_agent(httpx_client: httpx.AsyncClient, card: AgentCard, nutzer_text: str) -> None:
    """Wie `agent_antwort`, gibt das Ergebnis aber im Terminal aus."""
    print(f"[Orchestrator] Sende an {card.name} weiter...\n")
    ergebnis = await agent_antwort(httpx_client, card, nutzer_text)
    if ergebnis["fehler"]:
        print("   FEHLER:", ergebnis["fehler"])
    for antwort in ergebnis["antworten"]:
        print(f"   Antwort von {card.name}: {antwort}")


async def beantworte(
    httpx_client: httpx.AsyncClient, llm: LokalesLLM, cards: list[AgentCard], nutzer_text: str
) -> dict:
    """Routing + Delegation in einem Rutsch, als Daten für die Web-API.

    Enthält bewusst auch die Rohantwort des LLM: In der Oberfläche soll
    sichtbar sein, *warum* ein Agent gewählt wurde.
    """
    entscheidung = await entscheide_zustaendigen_agenten(llm, nutzer_text, cards)
    ziel_card = finde_card(entscheidung, cards)
    ergebnis = await agent_antwort(httpx_client, ziel_card, nutzer_text)
    return {
        "eingabe": nutzer_text,
        "skill_id": entscheidung["skill_id"],
        "agent": ziel_card.name,
        "url": entscheidung["url"],
        "roh": entscheidung["roh"],
        **ergebnis,
    }


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------
async def verarbeite(
    httpx_client: httpx.AsyncClient,
    llm: LokalesLLM,
    cards: list[AgentCard],
    nutzer_text: str,
) -> None:
    entscheidung = await entscheide_zustaendigen_agenten(llm, nutzer_text, cards)
    print(f"\n[Orchestrator] LLM-Rohantwort: {entscheidung['roh']!r}")
    print(f"[Orchestrator] Entscheidung: skill_id={entscheidung['skill_id']} -> {entscheidung['url']}")
    ziel_card = finde_card(entscheidung, cards)
    await frage_agent(httpx_client, ziel_card, nutzer_text)


async def orchestriere(texte: list[str] | None = None, llm: LokalesLLM | None = None) -> None:
    """Der komplette Orchestrator-Ablauf, auch von main.py aus aufrufbar.

    Mit `texte`: jeden Text einmal verarbeiten. Ohne: interaktiv, bis "exit".
    Ein bereits geladenes `llm` kann übergeben werden (main.py lädt es parallel
    zum Serverstart), sonst wird es hier geladen.
    """
    # Das Laden dauert lange und ist reine CPU-Arbeit: in einen Thread auslagern,
    # damit ein im selben Prozess laufender Server weiter Anfragen annehmen kann.
    if llm is None:
        llm = await asyncio.to_thread(LokalesLLM)

    async with httpx.AsyncClient() as httpx_client:
        cards = await lade_agent_cards(httpx_client)  # 1. Discovery

        if texte:
            for text in texte:
                print(f"\n=== Eingabe: {text}")
                await verarbeite(httpx_client, llm, cards, text)  # 2. Routing + 3. Delegation
            return

        while True:
            # input() blockiert; im Thread bleibt die Event-Loop (und damit der
            # Server im selben Prozess) währenddessen ansprechbar.
            frage = "\nWas möchtest du senden? (exit zum Beenden) "
            text = (await asyncio.to_thread(input, frage)).strip()
            if text.lower() in ("exit", "quit", ""):
                break
            await verarbeite(httpx_client, llm, cards, text)


async def main() -> None:
    await orchestriere(sys.argv[1:])


if __name__ == "__main__":
    asyncio.run(main())
