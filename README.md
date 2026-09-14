# A2A-Tutorial: Agenten, die miteinander reden

Dieses Projekt zeigt Schritt für Schritt, wie mehrere Agenten über das
**A2A-Protokoll (Agent-to-Agent)** zusammenarbeiten. Es besteht aus vier Teilen:

| Teil | Was passiert | Dateien |
|------|--------------|---------|
| 1 | Drei einfache Agenten laufen auf einem Server. Ein Client (Agent A) findet sie über ihre „Visitenkarte“ und schickt ihnen Nachrichten. | `agents_server.py`, `Agent A.py`, `*Agent.py`, `*AgentExecutor.py` |
| 2 | Ein Orchestrator (Agent C) hat selbst keine Fachlogik. Ein **lokales LLM** entscheidet anhand der Skill-Beschreibungen, welcher Agent zuständig ist, und der Orchestrator delegiert die Anfrage per A2A. | `agent_c_orchestrator.py` |
| 3 | Drei Agenten, die **externe Dienste** anbinden: aktuelles Wetter (Open-Meteo), Ontologie-Suche (OLS4, LOV) und der **Semantische Mediator des BIBA**. | `WeatherAgent.py`, `OntologySearchAgent.py`, `SemanticMediatorAgent.py` |
| 4 | Ein **komplexer Agent mit MCP-Server und Gedächtnis**: kennt die BIBA-Mitarbeitenden und ihre Veröffentlichungen, schlägt Reviewer vor, erstellt Forschungsprofile und beantwortet Fragen zu Personen. | `biba_mcp_server.py`, `ResearchAgent.py` |

Das Tutorial folgt der PDF `A2A-Tutorial-Orchestrator-LLM.pdf`, weicht aber an
zwei Stellen bewusst ab:

* Alle Agenten laufen auf **einem** Server (Port 9999) unter eigenen Pfaden, nicht auf getrennten Ports.
* Statt der Anthropic-API wird ein kleines Modell von Hugging Face (`Qwen/Qwen2.5-1.5B-Instruct`) lokal per `transformers` ausgeführt. Kein API-Key, keine Cloud.

---

## Inhalt

1. [Die Idee hinter A2A in 60 Sekunden](#1-die-idee-hinter-a2a-in-60-sekunden)
2. [Voraussetzungen und Installation](#2-voraussetzungen-und-installation)
3. [Projektstruktur](#3-projektstruktur)
4. [Teil 1: Agenten bereitstellen und ansprechen](#4-teil-1-agenten-bereitstellen-und-ansprechen)
5. [Teil 2: Der LLM-Orchestrator](#5-teil-2-der-llm-orchestrator)
6. [Teil 3: Agenten mit externen Diensten](#6-teil-3-agenten-mit-externen-diensten)
7. [Teil 4: Research-Agent mit MCP-Server und Gedächtnis](#7-teil-4-research-agent-mit-mcp-server-und-gedächtnis)
8. [Übung: Einen eigenen Agenten anschließen](#8-übung-einen-eigenen-agenten-anschließen)
9. [Fehlersuche](#9-fehlersuche)

---

## 1. Die Idee hinter A2A in 60 Sekunden

A2A ist ein offenes Protokoll, mit dem sich Agenten gegenseitig finden und
Aufgaben zuschieben können. Drei Begriffe reichen für dieses Tutorial:

* **AgentCard („Visitenkarte“)**: Ein JSON-Dokument, das ein Agent unter
  `<URL>/.well-known/agent-card.json` veröffentlicht. Darin stehen Name,
  Beschreibung, die Endpunkt-URL und die **Skills** des Agenten.
* **Skill**: Beschreibt, was der Agent kann, inklusive Beispielen. Das ist die
  „Werbung“ des Agenten. Genau diesen Text liest später das LLM im
  Orchestrator, um zu entscheiden, wer zuständig ist.
* **Task**: Eine Anfrage an einen Agenten wird zu einem Task mit Zuständen
  (`WORKING`, `COMPLETED`, `FAILED`). Das Ergebnis kommt als **Artefakt** zurück.

Der Ablauf ist immer derselbe:

```
Client                          Server (Agent)
  │  GET /adder/.well-known/agent-card.json
  │ ─────────────────────────────────────────▶
  │ ◀───────────── AgentCard (Name, Skills, URL)
  │
  │  POST /adder   (JSON-RPC: SendMessage "Addiere 17 und 25")
  │ ─────────────────────────────────────────▶
  │ ◀───────────── Task: WORKING → Artefakt "17 + 25 = 42" → COMPLETED
```

---

## 2. Voraussetzungen und Installation

* Python 3.12 oder neuer (das Projekt wurde mit 3.14 entwickelt)
* Etwa 3 GB freier Speicherplatz für das lokale Sprachmodell (nur Teil 2)
* Keine GPU nötig, das Modell läuft auf der CPU
* Internetzugang für die Agenten aus Teil 3 und 4 (Wetter, Ontologie-Suche, Publikationen)

```bash
# 1. Virtuelle Umgebung anlegen und aktivieren
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. Pakete für Teil 1 (Agenten und Client)
pip install a2a-sdk httpx starlette uvicorn

# 3. Zusätzliche Pakete für Teil 2 (lokales LLM)
pip install torch transformers accelerate

# 4. Zusätzliche Pakete für Teil 4 (MCP-Server, PDF, Webseiten, Google Scholar)
pip install "mcp>=2" pypdf beautifulsoup4 scholarly "bibtexparser<2"

# Oder alles auf einmal:
pip install -r requirements.txt
```

Teil 3 braucht keine weiteren Pakete. Der Wetter-Agent und die Ontologie-Suche
nutzen `httpx`, das mit dem A2A-SDK bereits installiert ist. Für Teil 4 muss
`bibtexparser` in Version 1 bleiben, weil `scholarly` mit Version 2 nicht startet.

Auf Windows ohne GPU installiert `pip install torch` automatisch die CPU-Variante.
Wer eine NVIDIA-GPU nutzen möchte, folgt der Anleitung auf https://pytorch.org.

---

## 3. Projektstruktur

```
AgentExample/
├── agents_server.py           # Startet EINEN Server, der alle Agenten hostet
├── Agent A.py                 # Einfacher Client: ruft Greeter und Adder direkt auf
├── agent_c_orchestrator.py    # LLM-Orchestrator: lässt ein lokales LLM routen
│
├── GreeterAgent.py            # Agent B: Fachlogik + AgentCard
├── GreenAgentExecuter.py      # Agent B: A2A-Anbindung (Executor)
├── AdderAgent.py              # Agent C/D: addiert zwei Zahlen
├── AdderAgentExecutor.py      # Adder: A2A-Anbindung
├── SubtractorAgent.py         # Agent E: subtrahiert zwei Zahlen
├── SubtractorAgentExecutor.py # Subtractor: A2A-Anbindung
│
├── WeatherAgent.py            # Agent F: aktuelles Wetter über Open-Meteo
├── WeatherAgentExecutor.py    # Weather: A2A-Anbindung
├── OntologySearchAgent.py     # Agent G: Ontologien in OLS4 und LOV suchen
├── OntologySearchAgentExecutor.py
├── SemanticMediatorAgent.py   # Agent H: Semantischer Mediator des BIBA
├── SemanticMediatorAgentExecutor.py
│
├── biba_mcp_server.py         # MCP-Server: Mitarbeitende, Publikationen, PDF-Zusammenfassung
├── ResearchAgent.py           # Agent I: MCP-Client, Gedächtnis, Reviewer/Profil/Fragen
├── ResearchAgentExecutor.py   # Research: A2A-Anbindung
├── memory/                    # (wird erzeugt) Gedächtnis-Datei des Research-Agenten
├── downloads/                 # (wird erzeugt) heruntergeladene PDFs
│
├── requirements.txt           # Alle Pakete für Teil 1 bis 3
├── main.py                    # PyCharm-Beispieldatei, nicht Teil des Tutorials
└── A2A-Tutorial-Orchestrator-LLM.pdf   # Das Original-Tutorial
```

### Das Muster: Agent + Executor

Jeder Agent besteht aus **zwei Klassen**, und dieses Muster wiederholt sich
dreimal identisch:

**1. Die `*Agent`-Klasse** (z. B. `AdderAgent.py`) enthält die Fachlogik und
weiß alles über sich selbst:

```python
class AdderAgent:
    PATH = "/adder"                        # Unter diesem Pfad ist er erreichbar

    SKILL = AgentSkill(                    # Die "Werbung" für das LLM
        id="add",
        name="Adder",
        description="Addiert zwei Zahlen, die im Text der Nachricht stehen.",
        examples=["Addiere 17 und 25", "Was ist 3,5 + 4?"],
        ...
    )

    def agent_card(self, base_url: str) -> AgentCard:
        ...                                # Baut die Visitenkarte

    async def invoke(self, user_request: str) -> str:
        ...                                # Die eigentliche Arbeit
```

**2. Die `*AgentExecutor`-Klasse** (z. B. `AdderAgentExecutor.py`) verbindet
den Agenten mit dem A2A-Protokoll. Sie kennt keine Mathematik, sondern nur den
Lebenszyklus eines Tasks:

```python
async def execute(self, context, event_queue):
    # 1. Task holen oder neu anlegen
    # 2. Status auf WORKING setzen
    # 3. self.agent.invoke(...) aufrufen
    #    -> bei ValueError: Status FAILED und zurück
    # 4. Ergebnis als Artefakt anhängen
    # 5. Status auf COMPLETED setzen
```

Diese Trennung ist der Kern des Tutorials: **Fachlogik und Protokoll bleiben
getrennt.** Der Executor ist bei allen Agenten fast Zeile für Zeile gleich. Die
Executoren aus Teil 3 fangen zusätzlich Netzwerkfehler (`httpx.HTTPError`) ab
und melden sie als `FAILED`, weil ihre Agenten externe Dienste aufrufen.

---

## 4. Teil 1: Agenten bereitstellen und ansprechen

### Schritt 1: Server starten

```bash
python agents_server.py
```

Erwartete Ausgabe:

```
[Server] Agent B – Greeter        -> http://127.0.0.1:9999/greeter  (Skill: greet)
[Server] Agent C – Adder          -> http://127.0.0.1:9999/adder  (Skill: add)
[Server] Agent E – Subtractor     -> http://127.0.0.1:9999/subtractor  (Skill: subtract)
[Server] Agent F – Weather        -> http://127.0.0.1:9999/weather  (Skill: weather)
[Server] Agent G – Ontology Search -> http://127.0.0.1:9999/ontology  (Skill: ontology_search)
[Server] Agent H – Semantic Mediator -> http://127.0.0.1:9999/mediator  (Skill: semantic_mediation)
[Server] Agent I – Research       -> http://127.0.0.1:9999/research  (Skill: find_reviewer)
INFO:     Uvicorn running on http://127.0.0.1:9999
[Research] Gedächtnis geladen: 0 Personen, 0 indexiert (memory\forschungsindex.json)
[Research] MCP-Server verbunden, Tools: list_biba_staff, search_publications, summarize_pdf, summarize_text
[Research] (1/86) Michael Freitag ...
```

Der Server weiß nichts über die einzelnen Agenten. Er fragt jeden Executor nur:
„Wo willst du hin, und wie sieht deine Visitenkarte aus?“ Die Funktion
`mount_agent` erzeugt daraus zwei Routen pro Agent:

| Route | Zweck |
|-------|-------|
| `GET /<PATH>/.well-known/agent-card.json` | Visitenkarte abrufen |
| `POST /<PATH>` | JSON-RPC-Endpunkt für Nachrichten |

### Schritt 2: Visitenkarte im Browser ansehen

Öffne http://127.0.0.1:9999/adder/.well-known/agent-card.json. Du siehst genau
das JSON, das `AdderAgent.agent_card()` erzeugt, inklusive Skill-Beschreibung
und Beispielen. Probiere auch `/greeter` und `/subtractor`.

### Schritt 3: Agent A als Client starten

In einem **zweiten Terminal** (der Server läuft weiter):

```bash
python "Agent A.py"
```

Agent A führt vier Anfragen aus, die letzte ist absichtlich ungültig:

```
Gefunden: Agent B – Greeter – Ein einfacher Begrüßungs-Agent zum Üben von A2A.
Fähigkeiten: Greeter
Agent A sendet: Ich bin Agent A und möchte dir Hallo sagen
Antwort von Agent B – Greeter:
   Hallo! Agent B hat deine Nachricht erhalten:'Ich bin Agent A und möchte dir Hallo sagen'

Gefunden: Agent C – Adder – Ein einfacher Rechen-Agent, der zwei Zahlen addiert.
Fähigkeiten: Adder
Agent A sendet: Bitte addiere 17 und 25
Antwort von Agent C – Adder:
   17 + 25 = 42

...
Agent A sendet: Addiere 1, 2 und 3
Antwort von Agent C – Adder:
   FEHLER: Ich brauche genau zwei Zahlen, habe aber 3 gefunden: [1.0, 2.0, 3.0]
```

### Was in `Agent A.py` passiert

Die Funktion `ask_agent` zeigt den kompletten Client-Ablauf in drei Schritten:

```python
# 1. Visitenkarte holen
resolver = A2ACardResolver(httpx_client=..., base_url=SERVER_URL,
                           agent_card_path="/adder/.well-known/agent-card.json")
card = await resolver.get_agent_card()

# 2. Client erzeugen. Die Ziel-URL steht in der Visitenkarte.
client = await create_client(agent=card, client_config=ClientConfig(streaming=False, ...))

# 3. Nachricht senden und Antwort lesen
request = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))
async for response in client.send_message(request):
    task = response.task
    if task.status.state == TaskState.TASK_STATE_FAILED:
        print("FEHLER:", get_message_text(task.status.message))
    for artifact in task.artifacts:
        print(get_artifact_text(artifact))
```

Wichtig: Der Client kennt die Endpunkt-URL des Agenten **nicht** vorab. Er
kennt nur den Ort der Visitenkarte und liest die URL daraus. Das ist Discovery.

---

## 5. Teil 2: Der LLM-Orchestrator

Bisher hat Agent A selbst entschieden, welchen Agenten er anspricht. Jetzt
übernimmt das ein Sprachmodell. Der Orchestrator in `agent_c_orchestrator.py`
arbeitet in drei Schritten:

```
             ┌────────────────────────────────────────────────┐
             │ Orchestrator (Agent C)                         │
Nutzer ───▶  │ 1. Discovery: AgentCards aller Agenten holen   │
"20 minus 8" │ 2. Routing:   lokales LLM wählt eine skill_id  │──▶ Subtractor
             │ 3. Delegation: gewählten Agenten per A2A rufen │◀── "20 - 8 = 12"
             └────────────────────────────────────────────────┘
```

### Schritt 1: Starten

Server aus Teil 1 muss laufen. Dann im zweiten Terminal:

```bash
# Interaktiver Modus
python agent_c_orchestrator.py

# Oder: Anfragen direkt als Argumente übergeben
python agent_c_orchestrator.py "Was ist 20 minus 8?" "Addiere 3 und 4" "Hallo!"
```

Beim **ersten Start** wird das Modell (ca. 3 GB) von Hugging Face
heruntergeladen. Das dauert je nach Verbindung einige Minuten. Danach liegt es
im Cache (`~/.cache/huggingface`) und der Start dauert nur noch Sekunden.

Erwartete Ausgabe:

```
[LLM] Lade Qwen/Qwen2.5-1.5B-Instruct (erster Start lädt das Modell herunter)...
[LLM] Bereit (1.54 Mrd. Parameter)
[Orchestrator] Gefunden: Agent B – Greeter – Skills: greet
[Orchestrator] Gefunden: Agent C – Adder – Skills: add
[Orchestrator] Gefunden: Agent E – Subtractor – Skills: subtract
[Orchestrator] Gefunden: Agent F – Weather – Skills: weather
[Orchestrator] Gefunden: Agent G – Ontology Search – Skills: ontology_search
[Orchestrator] Gefunden: Agent H – Semantic Mediator – Skills: semantic_mediation
[Orchestrator] Gefunden: Agent I – Research – Skills: find_reviewer, staff_profile, staff_question

=== Eingabe: Was ist 20 minus 8?

[Orchestrator] LLM-Rohantwort: '{"skill_id": "subtract"}'
[Orchestrator] Entscheidung: skill_id=subtract -> http://127.0.0.1:9999/subtractor
[Orchestrator] Sende an Agent E – Subtractor weiter...

   Antwort von Agent E – Subtractor: 20 - 8 = 12
```

### Schritt 2: Discovery verstehen

`lade_agent_cards` holt für jeden Eintrag in `AGENTS` die Visitenkarte, genau
wie Agent A. Danach reduziert `cards_als_dicts` die Karten auf das, was das LLM
zum Entscheiden braucht: Agentname, Skill-ID, Beschreibung, Beispiele.

### Schritt 3: Routing verstehen

Das LLM bekommt einen System-Prompt („Du bist ein Router ... antworte nur mit
`{"skill_id": "..."}`“) und einen Nutzer-Prompt mit der Skill-Übersicht als
JSON plus der Nutzeranfrage. Es soll **nur eine Skill-ID** nennen. Die
Endpunkt-URL leitet der Orchestrator dann selbst über `skill_index` ab.

Warum nur eine Skill-ID und nicht die URL? Zwei Gründe:

* Kleine Modelle vertippen sich bei URLs leichter als bei einem kurzen Wort.
* Das Modell kann keine URL erfinden, die wir nicht kennen. `finde_card` prüft
  zusätzlich, dass die Ziel-URL zu einer bekannten Karte gehört.

`parse_entscheidung` ist bewusst tolerant gebaut, weil ein 1,5-Milliarden-
Parameter-Modell das Format nicht immer exakt einhält:

1. Erst wird ein JSON-Objekt im Text gesucht und `skill_id` gelesen.
2. Klappt das nicht, reicht eine bekannte Skill-ID als ganzes Wort im Text.

### Schritt 4: Delegation verstehen

`frage_agent` ist derselbe Code wie in `Agent A.py`: Client bauen,
`send_message`, Artefakte ausgeben. Der Orchestrator ist aus Sicht des
Zielagenten einfach ein weiterer Client.

### Technische Details zum lokalen LLM

Die Klasse `LokalesLLM` lädt das Modell einmal und hält es im Speicher.

* `apply_chat_template` formatiert System- und Nutzer-Nachricht so, wie das
  Modell es im Training gesehen hat. Ohne dieses Template antwortet es Unsinn.
* `do_sample=False` macht die Antwort deterministisch.
* Die Generierung ist reine CPU-Arbeit und würde die asyncio-Schleife
  blockieren. Deshalb läuft sie über `asyncio.to_thread` in einem Hintergrund-Thread.
* Auf einer GPU: `dtype=torch.bfloat16` und `device_map="auto"` in
  `from_pretrained` setzen.

Alternative Modelle stehen als Kommentar über `LLM_MODEL` in der Datei.

### Probier es aus

Diese Eingaben zeigen, wie das Routing entscheidet:

| Eingabe | Erwartete Entscheidung |
|---------|------------------------|
| `Hallo, wie geht's?` | `greet` |
| `Addiere 17 und 25` | `add` |
| `Was ist 3,5 + 4?` | `add` |
| `Ziehe 5 von 12 ab` | `subtract` (Ergebnis 7, weil „x von y“ als y − x gilt) |
| `10 - 3` | `subtract` |
| `Addiere 1, 2 und 3` | `add`, aber der Agent meldet `FAILED` (drei Zahlen) |
| `Wie ist das Wetter in Bremen?` | `weather` |
| `Welche Ontologien gibt es für Sensoren?` | `ontology_search` |
| `Welche Datenmodelle kennt der Mediator?` | `semantic_mediation` |
| `Wer sollte ein Paper über Predictive Maintenance reviewen?` | `find_reviewer` |
| `Erstelle das Forschungsprofil von Karl Hribernik` | `staff_profile` |
| `Wie erreiche ich Michael Freitag?` | `staff_question` |

Adder und Subtractor sind absichtlich ähnlich beschrieben, damit das LLM
wirklich anhand der Beschreibung entscheiden muss. Bei mehrdeutigen Eingaben
kann ein so kleines Modell danebenliegen. Die Rohantwort wird immer mit
ausgegeben, damit du siehst, was das Modell tatsächlich geantwortet hat.

---

## 6. Teil 3: Agenten mit externen Diensten

Die Agenten aus Teil 1 rechnen nur mit dem Text, den sie bekommen. Die drei
Agenten aus Teil 3 rufen echte Dienste im Internet oder im eigenen Netz auf.
Das Muster Agent + Executor bleibt exakt gleich, nur `invoke()` wird
aufwendiger: Text verstehen, HTTP-Aufruf, Antwort formatieren.

### Agent F: Wetter (`WeatherAgent.py`)

Liefert das aktuelle Wetter für einen Ort. Datenquelle ist
[Open-Meteo](https://open-meteo.com), kostenlos und ohne API-Key.

```
>>> Wie ist das Wetter in Bremen?
Wetter in Bremen (Freie Hansestadt Bremen, Deutschland): bedeckt, 10.7 °C
(gefühlt 9.7 °C), Luftfeuchte 88 %, Wind 4.7 km/h. Stand: 2026-09-14T06:15 (Europe/Berlin)
```

So arbeitet `invoke()`:

1. `ort_aus_text` liest den Ortsnamen: zuerst das Wort nach „in“, „für“,
   „bei“ usw., sonst die letzte Folge großgeschriebener Wörter („New York“),
   sonst der ganze Text.
2. Geocoding-API: Ortsname zu Koordinaten. Kein Treffer bedeutet `ValueError`,
   und der Executor meldet den Task als `FAILED`.
3. Forecast-API mit `current=...`: Temperatur, gefühlte Temperatur,
   Luftfeuchte, WMO-Wettercode, Wind. Der Wettercode wird über die Tabelle
   `WMO_CODES` in Klartext übersetzt.

### Agent G: Ontologie-Suche (`OntologySearchAgent.py`)

Sucht bekannte Ontologien und Vokabulare zu einem Begriff. Zwei Register werden
befragt, beide ohne API-Key:

| Register | Was es ist | Wie wir es nutzen |
|----------|------------|-------------------|
| [OLS4](https://www.ebi.ac.uk/ols4) (EMBL-EBI) | Rund 280 Ontologien, Schwerpunkt Life Sciences, aber auch SOSA/SSN, PROV, Schema.org, QUDT | Klassen-Suche, Treffer nach Ontologie gruppiert, Titel per Detail-Endpunkt nachgeladen |
| [LOV](https://lov.linkeddata.es) (Linked Open Vocabularies) | Klassisches Register für Linked-Data-Vokabulare | Vokabular-Suche. Antwortet die Seite nicht mit JSON (kommt vor), wird sie übersprungen und das steht in der Ausgabe |

```
>>> Welche Ontologien gibt es für Sensoren?
Ontologien zu 'Sensoren':

OLS4 (EMBL-EBI) – 8 Ontologien mit passenden Klassen (gesucht nach 'Sensor'):
  • Allotrope Merged Ontology Suite [AFO]  https://www.ebi.ac.uk/ols4/ontologies/afo
      z. B. sensor <http://purl.allotrope.org/ontologies/equipment#AFE_0002184>
  • The Earth Metabolome Initiative (EMI) ontology [EMI]  https://www.ebi.ac.uk/ols4/ontologies/emi
      z. B. Sensor <http://www.w3.org/ns/sosa/Sensor>
  ...
```

OLS4 ist englisch indexiert. Damit deutsche Eingaben trotzdem Treffer liefern,
probiert `suchvarianten` nacheinander den Begriff selbst, dann ohne typische
Endungen („Sensoren“ wird zu „Sensor“), zuletzt den Wortstamm mit Wildcard
(„Logistik“ wird zu „Logisti*“). Die Ausgabe nennt, welche Variante gezogen hat.

### Agent H: Semantischer Mediator des BIBA (`SemanticMediatorAgent.py`)

Der Semantische Mediator des BIBA (Bremer Institut für Produktion und Logistik)
löst Interoperabilitätsprobleme zwischen heterogenen Datenquellen. Wrapper
bilden die lokalen Sichten der Quellen (SQL, CSV, XML, REST, Kafka) auf eine
globale Ontologie ab, und der Mediator transformiert Daten zwischen diesen
Sichten. Hintergrund: [A Semantic Mediator for Data Integration in Autonomous
Logistics Processes](https://link.springer.com/chapter/10.1007/978-1-84996-257-5_15)
und [Semantic Interoperability for Logistics and Beyond](https://link.springer.com/chapter/10.1007/978-3-030-88662-2_6).

Der Mediator ist **kein öffentlicher Dienst**. Der Agent spricht deshalb eine
eigene Instanz an, deren Adresse per Umgebungsvariable gesetzt wird:

```bash
set SEMANTIC_MEDIATOR_URL=http://localhost:8080      # Windows
set SEMANTIC_MEDIATOR_TOKEN=...                       # optional, Bearer-Token
# export ... unter Linux / macOS
python agents_server.py
```

Ohne gesetzte URL meldet der Agent jeden Task als `FAILED` mit einem
klaren Hinweis. Der Agent versteht zwei Aufträge:

```
>>> Welche Datenmodelle kennt der Mediator?
Bekannte Datenmodelle des Mediators:
  • ERP
  • AAS

>>> Transformiere von ERP nach AAS: {"artikelnummer": "4711", "menge": 3}
Transformation ERP -> AAS:
{ ... Antwort des Mediators im Zielmodell ... }
```

**Anpassen an die eigene Installation.** Die REST-Schnittstelle des Mediators
ist nicht öffentlich dokumentiert. Deshalb steckt alles, was vom konkreten
Endpunkt abhängt, in der Klasse `MediatorClient` am Anfang der Datei:

| Was | Wo | Annahme in diesem Projekt |
|-----|----|---------------------------|
| Modelle auflisten | `PFAD_MODELLE` | `GET /models`, Antwort ist Liste von Strings oder Objekten mit `name` |
| Transformation | `PFAD_TRANSFORM` | `POST /transform` mit `{"sourceModel", "targetModel", "data"}`, Antwort enthält `data` |
| Authentifizierung | `_headers()` | Bearer-Token aus `SEMANTIC_MEDIATOR_TOKEN`, falls gesetzt |

Weicht die eigene Mediator-Instanz davon ab, muss nur diese Klasse geändert
werden. Textverständnis (`auftrag_aus_text`) und A2A-Anbindung bleiben gleich.

### Teil 3 im Orchestrator

Alle drei Agenten sind in `AGENTS` in `agent_c_orchestrator.py` eingetragen.
Das LLM sieht damit sechs Skills. Die Skill-Beschreibungen enthalten bewusst
Stichwörter wie „Wetter“, „Ontologie“ und „transformieren“, damit ein kleines
Modell die Anfragen sauber trennen kann.

---

## 7. Teil 4: Research-Agent mit MCP-Server und Gedächtnis

Bisher hatte jeder Agent genau eine Fähigkeit und kein Gedächtnis. Agent I
ist anders gebaut:

* Er holt sich seine Daten nicht selbst, sondern über einen **MCP-Server**
  (Model Context Protocol), der als Kindprozess läuft und vier Werkzeuge anbietet.
* Er baut **beim Hochfahren ein Gedächtnis** auf: alle Mitarbeitenden des BIBA
  mit ihren Veröffentlichungen, Zusammenfassungen und Keywords.
* Er hat **drei Skills** auf einer Visitenkarte: Reviewer finden, Profil
  erstellen, Fragen zu Personen beantworten.

```
                          A2A                        MCP (stdio)
Orchestrator / Agent A  ───────▶  Agent I – Research  ───────────▶  biba_mcp_server.py
                                  │  Gedächtnis (JSON)              │  list_biba_staff      → biba.uni-bremen.de
                                  │  TF-IDF-Ranking                 │  search_publications  → Google Scholar / OpenAlex
                                  │  Skills: reviewer/profil/frage  │  summarize_pdf        → pypdf, extraktiv
                                  └───────────────────────────────  │  summarize_text
```

### Der MCP-Server (`biba_mcp_server.py`)

MCP ist ein offener Standard, mit dem ein Agent Werkzeuge eines anderen
Prozesses aufrufen kann. Ein Werkzeug ist eine Python-Funktion mit Docstring
und Typangaben, der Rest ist Dekorator:

```python
mcp = MCPServer("biba-research")

@mcp.tool()
async def list_biba_staff(abteilung: str = "") -> list[dict]:
    """Holt die Liste der Mitarbeitenden von der BIBA-Webseite."""
    ...

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

| Werkzeug | Was es tut | Quelle |
|----------|-----------|--------|
| `list_biba_staff` | Liest die Mitarbeitertabelle der BIBA-Webseite: Name, Titel, Rolle, E-Mail, Telefon, Raum, Abteilung, Homepage | https://www.biba.uni-bremen.de/institut/mitarbeiterinnen.html |
| `search_publications` | Sucht Veröffentlichungen einer Person und lädt Open-Access-PDFs herunter | Google Scholar (`scholarly`), bei Sperre automatisch OpenAlex mit Filter auf die BIBA-Institution |
| `summarize_pdf` | Extrahiert den Text einer PDF und erstellt Zusammenfassung plus Keywords | `pypdf`, extraktive Zusammenfassung |
| `summarize_text` | Dasselbe für einen Text, z. B. ein Abstract | |

Zwei Dinge sind bewusst so gelöst:

* **Google Scholar blockt Skripte.** Scholar hat keine offizielle API, und
  `scholarly` bekommt in der Praxis schnell einen Captcha. Der Server versucht
  Scholar mit Zeitlimit und weicht auf [OpenAlex](https://openalex.org) aus.
  OpenAlex ist frei, liefert Abstracts, Keywords und PDF-Fundorte, und der
  Filter auf die BIBA-Institution vermeidet Namensvettern. Der Agent merkt sich
  eine Scholar-Sperre und nutzt für den restlichen Lauf direkt OpenAlex.
  Mit `RESEARCH_SOURCE=openalex` überspringst du Scholar von vornherein.
* **Die Zusammenfassung ist extraktiv, ohne Sprachmodell.** Sie wählt die
  Sätze mit den häufigsten Fachwörtern und liefert die häufigsten Wörter und
  Wortpaare als Keywords. Das ist deterministisch und schnell genug, um beim
  Start Dutzende Dokumente zu verarbeiten. Viele Verlags-PDFs sind hinter
  Bot-Sperren, dann wird das Abstract zusammengefasst.

Server allein testen, ohne Agent:

```bash
mcp dev biba_mcp_server.py      # MCP-Inspector im Browser
```

### Der Agent (`ResearchAgent.py`)

**MCP-Client.** Die Klasse `MCPVerbindung` startet den Server als Kindprozess
und hält die Sitzung offen. Der Agent ruft Werkzeuge so auf:

```python
personen = await self.mcp.call("list_biba_staff")
ergebnis = await self.mcp.call("search_publications", autor="Marco Franke", max_results=3)
```

**Gedächtnis.** Die Klasse `Gedaechtnis` hält pro Person Stammdaten,
Veröffentlichungen (mit Zusammenfassung und Keywords) und ein gewichtetes
Keyword-Profil. Beim Start passiert Folgendes:

1. `beim_start()` lädt die Datei `memory/forschungsindex.json`, falls vorhanden.
2. Der MCP-Server wird gestartet.
3. Ein Hintergrund-Task ruft `list_biba_staff` auf und geht alle Personen durch,
   die noch nicht in der Datei sind: `search_publications`, dann pro
   Veröffentlichung `summarize_pdf` oder `summarize_text`. Nach jeder Person
   wird gespeichert, ein Abbruch verliert also nichts.
4. Der Server nimmt währenddessen schon Anfragen an. Jede Antwort enthält den
   Stand, z. B. `[Gedächtnis: 40/86 Personen indexiert, Phase: läuft]`.

Der erste Start dauert einige Minuten (etwa zwei Sekunden pro Person). Jeder
weitere Start liest nur die Datei. Zum Neuaufbau die Datei löschen.

**Ranking.** Für Reviewer-Auswahl und „Wer arbeitet an ...?“ baut das
Gedächtnis aus Keywords, Titeln und Zusammenfassungen ein TF-IDF-Modell und
vergleicht die Anfrage per Cosinus-Ähnlichkeit mit jedem Profil. Die
Ausgabe nennt die Begriffe, die den Ausschlag gaben.

`agents_server.py` weiß von alldem nichts. Er prüft nur, ob ein Agent
`beim_start()` und `beim_stopp()` anbietet, und hängt sie als Starlette-Hooks ein.

### Die drei Skills

**1. `find_reviewer`: Reviewer für ein Paper vorschlagen**

Eingabe ist ein Titel, ein Abstract oder der Pfad einer PDF. Bei einer PDF
ruft der Agent `summarize_pdf` auf und nutzt Zusammenfassung und Keywords als
Suchanfrage. Personen, die im Anfragetext oder auf der Titelseite der PDF
genannt werden, gelten als Autor:innen und werden ausgeschlossen. Bei
Preprints ohne Autorenzeile im extrahierten Text greift das nicht, dann den
Namen einfach mit in die Anfrage schreiben.

```
>>> Review-Anfrage: Semantic interoperability and data infrastructure for predictive maintenance of wind turbines
Paper: Semantic interoperability and data infrastructure for predictive maintenance of wind turbines
[Gedächtnis: 86/86 Personen indexiert, Phase: fertig]
Vorgeschlagene Reviewer:
  1. Stephan Oelker (Abt. 9, oel@biba.uni-bremen.de) – Score 0.22
     passende Begriffe: wind, maintenance, turbines, predictive, data
     • 2019: Machine learning-based icing prediction on wind turbines
  2. Karl Hribernik (Abt. 2.2, hri@biba.uni-bremen.de) – Score 0.148
     passende Begriffe: infrastructure, interoperability, data, semantic
     • 2026: Data infrastructure approach for information interoperability for the use of AI ...
```

**2. `staff_profile`: Forschungsprofil einer Person**

```
>>> Erstelle das Forschungsprofil von Michael Freitag
Forschungsprofil: Prof. Dr.-Ing. Michael Freitag
Abteilung 1 · Raum 1080 · +49 421 218-50 001 · fre@biba.uni-bremen.de
Forschungsschwerpunkte (aus 3 Veröffentlichungen, 2025):
  entwicklung, digitalisierung, exoskelette, herstellung, biomaterialien, ...
Veröffentlichungen:
  • 2025: Gamification zur Akzeptanzsteigerung industrieller Exoskelette
    in: Zeitschrift für wirtschaftlichen Fabrikbetrieb
    Kurz: Im Rahmen dieser Studie wurde ein gamifiziertes Anreizsystem ...
Häufige Ko-Autor:innen: Michael Lütjen (2), Lars Panter (1), ...
```

**3. `staff_question`: Fragen zu einer Person**

Der Agent erkennt die Person am Namen (voller Name, Nachname, auch leicht
falsch geschrieben) und die Art der Frage an Stichwörtern:

| Frage | Antwort aus dem Gedächtnis |
|-------|----------------------------|
| `Wie erreiche ich Michael Freitag?` | Abteilung, Raum, Telefon, E-Mail, Homepage |
| `Woran forscht Hribernik?` | Top-Keywords und neueste Veröffentlichung mit Kurzfassung |
| `Welche Veröffentlichungen hat Franke zu Interoperabilität?` | Passende Titel mit Link, ohne Thema alle bekannten |
| `Mit wem publiziert Freitag?` | Ko-Autor:innen mit Häufigkeit |
| `Wie viele Paper hat Oelker?` | Anzahl im Gedächtnis |
| `Wer im BIBA arbeitet an Predictive Maintenance?` | Rangliste der passenden Personen |

### Konfiguration

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `RESEARCH_INDEX_AT_STARTUP` | `1` | `0` lädt nur die Datei und indexiert nicht nach |
| `RESEARCH_MAX_STAFF` | `0` (alle) | Nur die ersten N Personen indexieren, praktisch zum Ausprobieren |
| `RESEARCH_MAX_PUBS` | `3` | Veröffentlichungen pro Person |
| `RESEARCH_SOURCE` | `auto` | `scholar`, `openalex` oder `auto` (Scholar, bei Sperre OpenAlex) |
| `RESEARCH_MEMORY_FILE` | `memory/forschungsindex.json` | Gedächtnis-Datei |
| `RESEARCH_DOWNLOAD_DIR` | `downloads` | Ordner für PDFs |
| `OPENALEX_MAILTO` | leer | E-Mail für den schnelleren „polite pool“ von OpenAlex |
| `SCHOLAR_TIMEOUT` | `40` | Sekunden, die ein Scholar-Versuch höchstens dauern darf |

Zum schnellen Ausprobieren:

```bash
set RESEARCH_MAX_STAFF=10
set RESEARCH_SOURCE=openalex
python agents_server.py
```

---

## 8. Übung: Einen eigenen Agenten anschließen

Ein neuer Agent braucht genau drei Änderungen. Als Beispiel ein Multiplizierer.

**1. `MultiplierAgent.py` anlegen.** Kopiere `AdderAgent.py` und passe an:

```python
class MultiplierAgent:
    PATH = "/multiplier"

    SKILL = AgentSkill(
        id="multiply",
        name="Multiplier",
        description="Multipliziert zwei Zahlen (mal, Produkt).",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "math", "multiplication"],
        examples=["Was ist 6 mal 7?", "Multipliziere 3 und 4"],
    )

    # agent_card() wie beim Adder, nur name/description anpassen

    async def invoke(self, user_request: str) -> str:
        numbers = [...]                    # wie beim Adder
        a, b = numbers
        return f"{self._fmt(a)} * {self._fmt(b)} = {self._fmt(a * b)}"
```

**2. `MultiplierAgentExecutor.py` anlegen.** Kopiere `AdderAgentExecutor.py`,
ersetze `AdderAgent` durch `MultiplierAgent` und den Artefaktnamen `"summe"`
durch `"produkt"`. Sonst ändert sich nichts.

**3. Registrieren.** Eine Zeile in `agents_server.py`:

```python
AGENTEN: list[AgentExecutor] = [
    GreeterAgentExecutor(),
    AdderAgentExecutor(),
    SubtractorAgentExecutor(),
    MultiplierAgentExecutor(),   # neu
]
```

und eine Zeile in `agent_c_orchestrator.py`:

```python
AGENTS = [
    ...
    {"base_url": SERVER_URL, "path": "/multiplier"},   # neu
]
```

Server neu starten, Orchestrator starten, `Was ist 6 mal 7?` eingeben. Das LLM
sollte jetzt `multiply` wählen, ohne dass du am Prompt etwas ändern musstest.
Die Skill-Beschreibung ist die einzige Information, die das Routing steuert.

---

## 9. Fehlersuche

**`ConnectError` oder `Connection refused` beim Start von Agent A oder Orchestrator**
Der Server läuft nicht. Starte zuerst `python agents_server.py` in einem
eigenen Terminal und lasse es offen.

**`Address already in use` beim Serverstart**
Port 9999 ist belegt, meist durch einen noch laufenden alten Server. Beende
ihn oder ändere `PORT` in `agents_server.py`. Dann auch `SERVER_URL` in
`Agent A.py` und `agent_c_orchestrator.py` anpassen.

**Erster Start des Orchestrators dauert sehr lange**
Das Modell wird heruntergeladen (ca. 3 GB). Das passiert nur einmal.

**`Konnte keine bekannte Skill-ID aus der LLM-Antwort lesen`**
Das Modell hat weder ein JSON-Objekt noch eine Skill-ID ausgegeben. Die
Rohantwort steht in der Fehlermeldung. Formuliere die Anfrage näher an den
Beispielen im Skill oder probiere ein anderes Modell aus der Liste in
`agent_c_orchestrator.py`.

**Das LLM wählt den falschen Agenten**
Bei einem 1,5B-Modell kommt das vor, besonders bei kurzen Eingaben wie `5 3`.
Verbessere zuerst die `description` und `examples` des Skills, das wirkt am
stärksten. Ein größeres Modell hilft ebenfalls.

**Modell `gemma-2-2b-it` lässt sich nicht laden**
Das Modell ist auf Hugging Face „gated“. Einmal `huggingface-cli login`
ausführen und die Nutzungsbedingungen auf der Modellseite akzeptieren.

**`NotImplementedError: Abbrechen wird hier nicht unterstützt`**
Absichtlich. Die Executor-Methode `cancel` ist im Tutorial nicht implementiert.

**Wetter-Agent: `Ich kenne keinen Ort namens '...'`**
Die Ortserkennung hat ein falsches Wort erwischt oder Open-Meteo kennt den Ort
nicht. Formuliere mit Präposition („Wetter in Bad Homburg“) oder schicke nur
den Ortsnamen.

**Wetter- oder Ontologie-Agent: `... nicht erreichbar`**
Kein Internetzugang, Proxy, oder der Dienst ist gerade nicht erreichbar. Beide
Dienste brauchen keinen API-Key.

**Ontologie-Agent: `LOV ... nicht erreichbar oder keine JSON-Antwort`**
Die LOV-API liefert zeitweise nur HTML. Der Agent überspringt LOV dann und
zeigt nur OLS4-Treffer. Das ist kein Fehler des Agenten.

**Mediator-Agent: `SEMANTIC_MEDIATOR_URL ist nicht gesetzt`**
Die Umgebungsvariable muss im Terminal gesetzt sein, in dem der
**Server** läuft, nicht im Terminal des Clients oder Orchestrators.

**Mediator-Agent: `404` oder unerwartete Antwort**
Die Pfade oder das Nachrichtenformat der eigenen Mediator-Instanz weichen von
den Annahmen ab. Anpassen in `MediatorClient` in `SemanticMediatorAgent.py`.

**Research-Agent: `No module named 'mcp.server.fastmcp'`**
Es ist MCP-SDK Version 1 installiert. Dieses Projekt nutzt Version 2
(`pip install "mcp>=2"`), dort heißt die Serverklasse `MCPServer`.

**Research-Agent: `No module named 'bibtexparser.bibdatabase'`**
`scholarly` braucht `bibtexparser` Version 1: `pip install "bibtexparser<2"`.

**Research-Agent: `Google Scholar nicht nutzbar (MaxTriesExceededException)`**
Scholar blockt die Anfragen. Das ist normal und kein Fehler: Der Agent nutzt
OpenAlex. Wer Scholar gar nicht erst probieren will, setzt `RESEARCH_SOURCE=openalex`.

**Research-Agent: `Ich habe keine bekannte Person im Text gefunden`**
Entweder ist der Name nicht auf der BIBA-Seite, oder das Gedächtnis ist noch
leer (siehe Phase in der Meldung). Nachname reicht, z. B. „Profil von Hribernik“.

**Research-Agent: Antworten enthalten `Phase: läuft`**
Die Indexierung läuft noch im Hintergrund. Antworten sind bereits möglich, aber
unvollständig. Im Server-Terminal steht der Fortschritt.

**Research-Agent: keine PDFs in `downloads/`**
Viele Verlage sperren automatische Downloads. Dann wird das Abstract
zusammengefasst, was für Ranking und Profil ausreicht. PDFs, die du selbst
hast, kannst du direkt mit einer Review-Anfrage übergeben:
`Review-Anfrage: C:/papers/eingereicht.pdf`.
