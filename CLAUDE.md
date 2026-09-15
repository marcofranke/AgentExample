# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Was dieses Projekt ist

Ein deutschsprachiges Lehrprojekt zum **A2A-Protokoll** (Agent-to-Agent, `a2a-sdk`): mehrere Agenten laufen auf einem gemeinsamen Server, ein LLM-Orchestrator entdeckt sie über ihre AgentCards und routet Anfragen an den passenden Skill. Das Original-Tutorial liegt als `A2A-Tutorial-Orchestrator-LLM.pdf` bei, die ausführliche Erklärung in `README.md`.

**Sprache:** Code, Docstrings, Kommentare, Bezeichner und Ausgaben sind durchgehend deutsch (`beim_start`, `Gedaechtnis`, `auftrag_aus_text`, `indexieren`). Neuer Code folgt dieser Konvention – nur A2A-/MCP-SDK-Schnittstellen behalten ihre englischen Namen (`invoke`, `execute`, `agent_card`, `SKILL`). Weil das Repo Lehrmaterial ist, sind ausführliche erklärende Docstrings erwünscht, nicht minimalistischer Code.

## Befehle

Es gibt keine Tests, keinen Linter und kein Build. Alles läuft direkt über Python (Windows, Python 3.12+, entwickelt mit 3.14):

```powershell
.venv\Scripts\activate
pip install -r requirements.txt

python agents_server.py            # Terminal 1: alle Agenten auf 127.0.0.1:9999
python "Agent A.py"                # Terminal 2: einfacher Client (Greeter + Adder)
python agent_c_orchestrator.py     # Terminal 2: LLM-Orchestrator, interaktiv
python agent_c_orchestrator.py "Wie ist das Wetter in Bremen?"   # oder direkt als Argument
```

Einen einzelnen Agenten prüfen, ohne Client: `GET http://127.0.0.1:9999/<PATH>/.well-known/agent-card.json`.

MCP-Server isoliert testen: `python biba_mcp_server.py` (wartet auf stdio) oder `mcp dev biba_mcp_server.py` (Inspector im Browser).

Der erste Start des Orchestrators lädt ~3 GB Modellgewichte (`Qwen/Qwen2.5-1.5B-Instruct`, CPU, float32).

## Architektur

### Das durchgehende Muster: Agent + Executor

Jeder Agent besteht aus zwei Dateien, und dieses Muster wiederholt sich für alle sieben Agenten nahezu Zeile für Zeile:

* **`<Name>Agent.py`** – Fachlogik *und* Selbstbeschreibung. Klassenattribute `PATH` (z. B. `/adder`) und `SKILL` bzw. `SKILLS`, dazu `agent_card(base_url)` und `async invoke(user_request) -> str`. Ungültige Eingaben werfen `ValueError` mit einer Nachricht, die der Nutzer zu sehen bekommt.
* **`<Name>AgentExecutor.py`** – reine A2A-Anbindung, kennt keine Fachlogik. Immer derselbe Ablauf: Task holen/anlegen → Status `WORKING` → `self.agent.invoke(...)` → `ValueError` wird zu Status `FAILED` → Ergebnis als Artefakt → Status `COMPLETED`. Die Executoren der externen Agenten fangen zusätzlich `httpx.HTTPError` ab, der Research-Executor `RuntimeError` (MCP-Fehler).

Diese Trennung ist der didaktische Kern – beim Erweitern nicht aufweichen.

### Registrierung eines Agenten

`agents_server.py` weiß **nichts** über die einzelnen Agenten: `mount_agent()` fragt jeden Executor nur nach `executor.agent.PATH` und `agent_card()` und erzeugt daraus die Karten- und JSON-RPC-Routen. Ein neuer Agent kostet deshalb genau drei Änderungen:

1. `<Name>Agent.py` + `<Name>AgentExecutor.py` anlegen (Vorlage: `AdderAgent.py`)
2. eine Zeile in `AGENTEN` in `agents_server.py`
3. eine Zeile in `AGENTS` in `agent_c_orchestrator.py`

Am Routing-Prompt muss nichts geändert werden – die **Skill-Beschreibung und die `examples` sind die einzige Information, die das Routing steuert**. Bei einem kleinen Modell entscheiden dort platzierte Stichwörter („Wetter“, „Ontologie“, „transformieren“) über den Erfolg.

### Optionaler Lebenszyklus

Hat ein Agent `beim_start()` / `beim_stopp()`, ruft der Starlette-Lifespan in `agents_server.py` sie automatisch auf – duck-typed über `hasattr`, ohne dass der Server den Agenten kennt. Aktuell nutzt das nur `ResearchAgent` (Gedächtnis laden, MCP-Kindprozess starten, Indexierung im Hintergrund).

### Orchestrator (`agent_c_orchestrator.py`)

Drei Phasen: **Discovery** (AgentCards aller `AGENTS` holen) → **Routing** (lokales HF-Modell wählt eine `skill_id`, Generierung läuft via `asyncio.to_thread`, damit die Event-Loop frei bleibt) → **Delegation** (A2A-Aufruf wie in `Agent A.py`). `parse_entscheidung()` ist bewusst tolerant (JSON-Objekt suchen, sonst bekannte Skill-ID als Wort), weil ein 1,5-B-Modell das Format oft verfehlt; `finde_card()` ist das Sicherheitsnetz gegen halluzinierte URLs.

### Research-Agent + MCP (`ResearchAgent.py`, `biba_mcp_server.py`)

Der einzige Agent mit Gedächtnis, und der einzige, der einen **MCP-Server als Kindprozess** (stdio) startet. Wichtige Grenze: *Der Agent ruft selbst keine Webseite auf.* Jede Datenbeschaffung – BIBA-Mitarbeitendenliste, ORCID-Abgleich, Publikationssuche (Google Scholar mit OpenAlex als Ausweichquelle), PDF-Download, Zusammenfassung – ist ein MCP-Tool. Im Agenten bleiben nur Gedächtnis, TF-IDF-ähnliches Ranking und Sprachausgabe. Neue Datenquellen gehören folglich in `biba_mcp_server.py`.

**ORCID-Abgleich (`find_orcid`).** Der Name von der BIBA-Webseite ist als Suchschlüssel mehrdeutig („Michael Freitag“, „Marco Franke“ gibt es mehrfach), und eine Namensverwechslung verfälscht direkt die Reviewer-Auswahl. Deshalb wird zu jedem Namen die ORCID-iD gesucht und an `search_publications` weitergereicht. Drei Punkte, die beim Ändern zu beachten sind:

* **orcid.org lässt sich nicht parsen.** Sowohl die Trefferliste als auch jede Profilseite liefern denselben leeren Angular-Rumpf (`<app-root>`), ohne Namen, Einrichtung oder JSON-LD. Abgefragt wird deshalb `pub.orcid.org/v3.0/expanded-search/` – die Adresse, die die Webseite selbst benutzt. Ein BeautifulSoup-Parser auf dem HTML fände nichts; das ist nachgemessen.
* **Name UND BIBA müssen passen.** Die ORCID-Suche ist großzügig: „Karl Hribernik“ liefert auch eine Person namens „Subrat Kumar Dang“, die sogar am BIBA ist. `_name_passt()` prüft deshalb zuerst Nach- und Vornamen, `_ist_biba()` danach die Einrichtung.
* **`orcid` ist nur bei BIBA-Bestätigung gesetzt.** Profile ohne jede Einrichtungsangabe werden verworfen, auch wenn der Name eindeutig ist. Eine falsche iD wäre schlimmer als keine: Die Publikationssuche fiele ohne sie auf den Namen zurück, mit falscher iD lieferte sie nichts.

In `_openalex_suche()` wird die iD **zusätzlich** zum BIBA-Institutionsfilter gesetzt, nicht statt seiner. OpenAlex hat an manchen ORCID-iDs fremde Arbeiten hängen (bei `0000-0003-1570-0168` Chemie-Aufsätze eines Namensvetters): 80 Arbeiten nur über ORCID, 27 mit beidem.

Das Ergebnis wird im Gedächtnis vermerkt – **auch ein negatives** (`orcid_geprueft_am` ohne `orcid`). Sonst fragte jeder Serverstart erneut für alle ~86 Personen bei ORCID an. Der Abgleich läuft *vor* dem `continue` für bereits indexierte Personen, sonst bliebe das eingecheckte Gedächtnis für immer ohne ORCID-Angaben.

**`find_reviewer` liefert immer zwei Vorschläge** (Erst- und Zweitgutachten, `RESEARCH_REVIEWER`). Reichen die inhaltlichen Überschneidungen nur für einen, wird das ausgeschrieben statt aufgefüllt.

`MCPVerbindung` hält die Sitzung in einem eigenen Hintergrund-Task offen, weil die Kontextmanager von `stdio_client`/`ClientSession` im selben Task betreten und verlassen werden müssen – diese Konstruktion nicht zu „vereinfachen“ versuchen.

`auftrag_aus_text()` verteilt intern per Regex auf die Skills (`reviewer`, `profil`, `frage`, `wer`); der Orchestrator routet nur *zum Agenten*, nicht zum einzelnen Skill.

Das Gedächtnis `memory/forschungsindex.json` ist **absichtlich eingecheckt** (`downloads/` dagegen nicht), damit ein Neustart ohne Netz funktioniert.

### Semantic-Mediator-Agent (`SemanticMediatorAgent.py`, `mediator/`)

Der zweite Agent mit mehreren Skills – und der einzige, dessen Gegenstück ein **eigener Container** ist. Der Semantische Mediator ist eine Spring-Boot-Anwendung (Java 8) aus `C:\Users\fma\git\semanticmediator`; `docker-compose.yml` startet ihn als Dienst `mediator` neben den Agenten.

Rollenteilung wie beim Research-Agenten: *Der Agent macht keine Datenintegration.* Föderation, Reasoning über die Ontologie und Export sind Sache des Mediators. Im Agenten bleiben Textverständnis, Endpunktwahl und Sprachausgabe.

`MediatorClient` bündelt **alle** REST-Endpunkte als `PFAD_*`-Klassenattribute; weicht eine Installation ab, wird ausschließlich dort angepasst. Die Endpunkte entsprechen `semanticfederationmoduleServiceController` und `ImportWrapperController` im Java-Projekt:

| Bereich | Endpunkte |
|---|---|
| Zustand | `/hello`, `/version`, `/getCurrentDataSources`, `/getAppliedTripleStore` |
| Abfragen | `/query` (SPARQL, GET), `/queryPost` (SPARQL → CSV), `/queryGraphQL` |
| Export | `/forwardRealDataSource`, `/forwardVirtualDataSource2`, `/createVirtualDataSource` |
| Wrapper | `/import/importWrapperConfiguration`, `/getOntologyFromDataSourceConfiguration`, `/getMappingFromDataSourceConfiguration`, `/requestDataPathesFromWrapper` |
| Verwaltung | `/reloadServices`, `/reloadSingleService/{id}`, `/setAppliedTripleStore`, `/activate*Upload`, `/deactivate*Upload`, `/publishOnKafka` |

Zwei Eigenheiten des Mediators, die der Client abfängt:

* `/query` erwartet Anfrage **und** Namensraum als ein JSON-Objekt *innerhalb* des `query`-Parameters, nicht als zwei Parameter.
* `/query` und `/queryGraphQL` liefern im Fehlerfall `null` mit Status 200, keinen 500er. Der Client macht daraus einen `ValueError` mit lesbarer Erklärung.

`auftrag_aus_text()` verteilt per Regex auf die sechs Skills (`mediator_status`, `mediator_query`, `mediator_graphql`, `mediator_transform`, `mediator_schema`, `mediator_admin`); der Orchestrator routet nur *zum Agenten*. Die Reihenfolge der Prüfungen ist entscheidend: erst das eindeutig Erkennbare (SPARQL-Text, JSON mit `mappingXml`), zuletzt das Allgemeine (Status). Die `configurationID` aus der `config.xml` ist der rote Faden – ohne sie funktioniert weder Export noch Reload, deshalb gibt der Status-Skill sie prominent aus.

`dienste_aus_config()` parst die `config.xml` des Mediators; `MAX_ZEICHEN` deckelt Antworten, weil ein CSV-Export megabytegroß sein kann.

Der Ordner `mediator/` enthält das Dockerfile, eine lauffähige `config/config.xml` und die Beispiel-Datenquelle `config/SCADA/` (CSV mit Sensormesswerten plus Ontologie und Mapping). Das ~110 MB große `semanticmediator.jar` wird **nicht** eingecheckt – es entsteht im Mediator-Repo mit `mvn clean package` (dort ist `clean` Pflicht) und wird nach `mediator/` kopiert. Ein Build im Container scheitert außerhalb des BIBA-Netzes an internen SNAPSHOT-Artefakten.

`mediator/config/config.xml` ist **absichtlich eingecheckt** – aus demselben Grund wie `memory/forschungsindex.json`. Der Mediator *schreibt* hinein: Ein Wrapper-Import hängt einen weiteren `<Service>` an (mit `id = Anzahl + 1`).

### Externe Dienste

| Agent | Dienst | Schlüssel nötig |
|---|---|---|
| `WeatherAgent` | Open-Meteo (Geocoding + Forecast) | nein |
| `OntologySearchAgent` | OLS4 (EMBL-EBI) und LOV | nein |
| `SemanticMediatorAgent` | Semantic Mediator, Dienst `mediator` im Compose-Stack | `SEMANTIC_MEDIATOR_URL`, optional `SEMANTIC_MEDIATOR_TOKEN` |

Ohne gesetzte `SEMANTIC_MEDIATOR_URL` meldet Agent H jeden Task als `FAILED` – mit einer Meldung, die die Variable nennt.

## Konfiguration über Umgebungsvariablen

Alle optional, alle mit Standardwert im Code:

| Variable | Standard | Bedeutung |
|---|---|---|
| `RESEARCH_INDEX_AT_STARTUP` | `1` | `0` = Gedächtnis nur laden, nicht nachindexieren |
| `RESEARCH_MAX_STAFF` | `0` (alle ~86) | Nur die ersten N Personen indexieren – zum schnellen Ausprobieren |
| `RESEARCH_MAX_PUBS` | `3` | Veröffentlichungen pro Person |
| `RESEARCH_SOURCE` | `auto` | `scholar`, `openalex` oder `auto` |
| `RESEARCH_ORCID` | `1` | `0` = kein ORCID-Abgleich; Publikationssuche läuft dann nur über den Namen |
| `RESEARCH_REVIEWER` | `2` | Anzahl der Reviewer-Vorschläge |
| `ORCID_BIBA_PATTERN` | `\bbiba\b\|bremer institut` | Regex, an der im ORCID-Profil die BIBA-Zugehörigkeit erkannt wird |
| `RESEARCH_MEMORY_FILE` | `memory/forschungsindex.json` | Gedächtnis-Datei |
| `RESEARCH_DOWNLOAD_DIR` | `downloads` | Ordner für PDFs |
| `OPENALEX_MAILTO` / `OPENALEX_INSTITUTION_ID` | leer / `I4387156409` | OpenAlex „polite pool“ bzw. BIBA-Institution |
| `SCHOLAR_TIMEOUT` | `40` | Sekunden pro Scholar-Versuch |
| `SEMANTIC_MEDIATOR_URL` | leer | Basis-URL des Mediators; im Compose-Stack `http://mediator:8081` |
| `SEMANTIC_MEDIATOR_TOKEN` | leer | optionales Bearer-Token für den Mediator |
| `SEMANTIC_MEDIATOR_NAMESPACE` | `http://www.levelup-project.eu/ontologies` | Namensraum für SPARQL-Anfragen ohne eigene Angabe |
| `SEMANTIC_MEDIATOR_TIMEOUT` | `120` | Sekunden pro Mediator-Aufruf (Föderation über viele Quellen dauert) |
| `HF_TOKEN` | leer | Hugging-Face-Token für den Orchestrator (nötig für gated Modelle wie `google/gemma-2-2b-it`; das Standardmodell ist frei) |

Zugangsdaten stehen in `.env` (per `.gitignore` ausgeschlossen, Vorlage: `.env.example`) und zusätzlich als dauerhafte User-Umgebungsvariable. Das Projekt lädt `.env` **nicht** selbst ein – die Datei dient als Ablage und für die PyCharm-Run-Konfiguration; wirksam ist die Umgebungsvariable.

Beim Arbeiten am Research-Agenten lohnt sich `RESEARCH_MAX_STAFF=5` und `RESEARCH_SOURCE=openalex` – eine volle Indexierung dauert lange und Scholar blockt Skripte häufig.

## Hinweise

* `main.py` ist der Schnellstart: Server und Orchestrator laufen dort in **einem Prozess** und teilen sich eine Event-Loop (uvicorn als Hintergrund-Task, Orchestrator im Vordergrund). Alles Blockierende – Modell laden, `input()` – liegt in Threads, sonst nimmt der Server keine Anfragen mehr an, solange auf eine Eingabe gewartet wird. Wiederverwendbarer Einstieg im Orchestrator: `orchestriere(texte, llm)`; `agent_c_orchestrator.main()` ist nur noch ein Aufruf davon. Die Einzelstarts (`agents_server.py` + `agent_c_orchestrator.py` in zwei Terminals) bleiben unverändert möglich und sind zum Erklären die bessere Variante.
* `bibtexparser` muss auf Version 1 bleiben (`bibtexparser<2`), sonst startet `scholarly` nicht.
* Port und Host stehen in `agents_server.py`; eine Änderung erfordert auch `SERVER_URL` in `Agent A.py` und `agent_c_orchestrator.py`.
* `docker-compose.yml` startet zwei Dienste im Netz `a2a-netz`: `a2a` (Agenten + Weboberfläche, Port 9999) und `mediator` (Semantischer Mediator, Port 8081). Nur im gemeinsamen Netz löst `mediator` als DNS-Name auf – deshalb kein `network_mode: bridge` mehr. Nur die Agenten: `docker compose up -d --no-deps a2a`.
* Der Mediator hört im Image auf Port 3053 (`application.yml`). Das Compose setzt `SERVER_PORT=8081`; wird das geändert, müssen `SEMANTIC_MEDIATOR_URL`, das Port-Mapping und `ENV SERVER_PORT` in `mediator/Dockerfile` (dort hängt auch der Healthcheck dran) mitgezogen werden.
* Mediator-Eigenschaften werden im Compose **doppelt** gesetzt – einmal in Punktschreibweise (`influx.upload`), einmal in Großbuchstaben (`INFLUX_UPLOAD`). Nur die zweite Form kommt zuverlässig als Umgebungsvariable bei Spring an; die erste steht dort, weil sie so im Java-Quellcode als `@Value` auftaucht und damit auffindbar bleibt.
* Wer README-Inhalte ändert (neuer Agent, neue Variable), hält Projektstruktur, Konfigurationstabelle und die Übung in `README.md` mit.
