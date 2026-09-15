"""Agent I – Research: BIBA-Forschungsprofile, Reviewer-Auswahl und Fragen zu Mitarbeitenden.

Der Agent hat drei Skills und ein Gedächtnis:

  * Gedächtnis (Memory): Beim Hochfahren holt der Agent über den MCP-Server
    (biba_mcp_server.py) alle Mitarbeitenden von der BIBA-Webseite, sucht zu
    jedem Namen die ORCID-iD, sucht damit die Veröffentlichungen, lädt PDFs
    herunter, lässt sie zusammenfassen und legt pro Person ein Forschungsprofil
    (Keywords) ab. Das Ergebnis wird als JSON gespeichert, damit ein Neustart
    schnell ist.
  * Skill "find_reviewer": Zu einer Review-Anfrage (Titel, Abstract oder
    PDF-Pfad) IMMER ZWEI Gutachter:innen aus dem Gedächtnis auswählen –
    Erst- und Zweitgutachten, wie im Peer-Review üblich.

Warum der Umweg über ORCID: Der Name von der BIBA-Webseite ist als Suchschlüssel
mehrdeutig. "Michael Freitag" oder "Marco Franke" gibt es in der Wissenschaft
mehrfach, und eine reine Namenssuche mischt deren Arbeiten zusammen – was direkt
die Reviewer-Auswahl verfälscht. Das MCP-Tool `find_orcid` sucht den Namen
deshalb bei ORCID und nimmt nur das Profil, in dem das BIBA als Einrichtung
steht. Erst diese iD identifiziert die Person.
  * Skill "staff_profile": Forschungsprofil einer Person zusammenstellen.
  * Skill "staff_question": Weitere Fragen zu einer Person beantworten
    (Kontakt, Themen, Veröffentlichungen, Ko-Autor:innen ...).

Der Agent ruft KEINE Webseite selbst auf. Alles, was Daten beschafft, läuft
über die MCP-Tools. Der Agent enthält nur Gedächtnis, Ranking und Sprache.

Umgebungsvariablen (alle optional):
    RESEARCH_INDEX_AT_STARTUP  "1" (Standard) baut das Gedächtnis beim Start auf, "0" nur laden
    RESEARCH_MAX_STAFF         0 = alle Mitarbeitenden (Standard), sonst nur die ersten N
    RESEARCH_MAX_PUBS          Veröffentlichungen pro Person (Standard 3)
    RESEARCH_SOURCE            "auto" (Standard), "scholar" oder "openalex"
    RESEARCH_MEMORY_FILE       Pfad der Gedächtnis-Datei (Standard memory/forschungsindex.json)
    RESEARCH_DOWNLOAD_DIR      Ordner für PDFs (Standard downloads/)
    RESEARCH_ORCID             "1" (Standard) gleicht Namen gegen ORCID ab, "0" schaltet das ab
    RESEARCH_REVIEWER          Anzahl der Reviewer-Vorschläge (Standard 2)
"""

import asyncio
import difflib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol

MCP_SERVER_SKRIPT = Path(__file__).with_name("biba_mcp_server.py")


# ---------------------------------------------------------------------------
# MCP-Verbindung: startet den Server als Kindprozess und hält die Sitzung offen
# ---------------------------------------------------------------------------
class MCPVerbindung:
    """Hält eine MCP-Sitzung zum Kindprozess offen und bietet `call(tool, **args)`.

    Die Kontextmanager von `stdio_client`/`ClientSession` müssen in demselben
    Task betreten und verlassen werden. Deshalb läuft die Verbindung in einem
    eigenen Hintergrund-Task, der auf ein Stopp-Signal wartet.
    """

    def __init__(self, skript: Path = MCP_SERVER_SKRIPT) -> None:
        self.skript = skript
        self.session: ClientSession | None = None
        self._bereit = asyncio.Event()
        self._stopp = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._fehler: BaseException | None = None

    async def starten(self) -> None:
        if self._task:
            return
        self._task = asyncio.create_task(self._laufen(), name="mcp-verbindung")
        await self._bereit.wait()
        if self._fehler:
            raise RuntimeError(f"MCP-Server konnte nicht gestartet werden: {self._fehler}")

    async def _laufen(self) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self.skript)],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            cwd=str(self.skript.parent),
        )
        try:
            async with stdio_client(params) as (lesen, schreiben):
                async with ClientSession(lesen, schreiben) as session:
                    await session.initialize()
                    self.session = session
                    self._bereit.set()
                    await self._stopp.wait()
        except BaseException as err:  # noqa: BLE001 – Fehler an starten() melden
            self._fehler = err
            self._bereit.set()
        finally:
            self.session = None

    async def stoppen(self) -> None:
        self._stopp.set()
        if self._task:
            await self._task

    async def call(self, tool: str, **argumente) -> dict | list:
        if not self.session:
            raise RuntimeError("MCP-Verbindung ist nicht aufgebaut")
        ergebnis = await self.session.call_tool(tool, argumente, read_timeout_seconds=300)
        if ergebnis.is_error:
            texte = [c.text for c in ergebnis.content if getattr(c, "text", None)]
            raise RuntimeError(f"MCP-Tool {tool} meldet Fehler: {' '.join(texte)}")
        daten = ergebnis.structured_content
        if isinstance(daten, dict) and set(daten) == {"result"}:
            return daten["result"]  # MCP packt Listen in {"result": [...]}
        if daten is not None:
            return daten
        return json.loads(ergebnis.content[0].text)

    async def tools(self) -> list[str]:
        if not self.session:
            return []
        return [t.name for t in (await self.session.list_tools()).tools]


# ---------------------------------------------------------------------------
# Gedächtnis: Mitarbeitende + Veröffentlichungen + Forschungsprofile
# ---------------------------------------------------------------------------
_WORT = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\-]{2,}")
_STOPP = set(
    "the a an and or of to in on for with by from as at is are was were be this that which who und oder der die das "
    "den dem des ein eine einer eines für mit bei aus nach über durch von zu zur zum im am ist sind wird werden auch nicht "
    "based using approach paper study results analysis new towards abstract zusammenfassung erste ersten zeigt zeigen "
    "wurde wurden dabei jedoch bereits sowie sowohl insbesondere hierbei anhand mittels zudem zwischen however therefore "
    "thus moreover furthermore within respectively significant different various several hat haben kann können soll "
    "welche welcher welches wie was wer wo wann warum bitte gibt aufgrund studie deutsche deutschen rahmen form trotz "
    "ziel ziele hierzu darüber hinaus ergebnisse ergebnis ansatz ansätze beitrag".split()
)
# OpenAlex hängt sehr allgemeine Fachgebiete als Keywords an. Die helfen beim Ranking nicht.
_ALLGEMEINE_KEYWORDS = set(
    "political science philosophy computer science engineering business mathematics epistemology sociology "
    "economics biology medicine physics chemistry psychology geography art law history".split()
)


def tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORT.findall(text) if w.lower() not in _STOPP]


# Wörter, die eine Frage formulieren, aber kein Fachthema sind
_FRAGEWOERTER = set(
    "veröffentlichung veröffentlichungen publikation publikationen paper papers artikel aufsatz aufsätze geschrieben "
    "forscht forschung thema themen schwerpunkt schwerpunkte interessen arbeitet beschäftigt kennst weißt sag nenne "
    "liste zeige welche welcher welches wie viele anzahl kontakt profil forschungsprofil biba mitarbeiter über zum".split()
)


def keyword_bereinigen(kw: str) -> str:
    """'Context (archaeology)' -> 'context'; sehr allgemeine Gebiete -> ''."""
    kw = re.sub(r"\s*\(.*?\)\s*", " ", kw).strip().lower()
    return "" if not kw or kw in _ALLGEMEINE_KEYWORDS or all(w in _ALLGEMEINE_KEYWORDS for w in kw.split()) else kw


class Gedaechtnis:
    """Alles, was der Agent über die Mitarbeitenden weiß, plus TF-IDF-Ranking darüber."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei
        self.personen: dict[str, dict] = {}  # name -> {stammdaten, publikationen, keywords}
        self.status = {"phase": "leer", "indexiert": 0, "gesamt": 0, "hinweise": []}
        self._idf: dict[str, float] = {}
        self._vektoren: dict[str, dict[str, float]] = {}

    # ---- Persistenz --------------------------------------------------------
    def laden(self) -> None:
        if self.datei.exists():
            daten = json.loads(self.datei.read_text(encoding="utf-8"))
            self.personen = daten.get("personen", {})
            for p in self.personen.values():
                p["publikationen"] = self._ohne_duplikate(p.get("publikationen", []))
            self.status["indexiert"] = sum(1 for p in self.personen.values() if p.get("indexiert_am"))
            self._index_neu_bauen()

    def speichern(self) -> None:
        self.datei.parent.mkdir(parents=True, exist_ok=True)
        self.datei.write_text(
            json.dumps({"gespeichert_am": datetime.now().isoformat(timespec="seconds"), "personen": self.personen},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    # ---- Aufbau ------------------------------------------------------------
    @staticmethod
    def _ohne_duplikate(publikationen: list[dict]) -> list[dict]:
        """Dieselbe Arbeit taucht in Repositorien oft mehrfach auf (z. B. Zenodo-Versionen)."""
        gesehen: set[str] = set()
        eindeutig = []
        for pub in publikationen:
            schluessel = " ".join(tokens(pub.get("titel", "")))
            if schluessel and schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            eindeutig.append(pub)
        return eindeutig

    def person_anlegen(self, stamm: dict) -> dict:
        eintrag = self.personen.setdefault(stamm["name"], {"publikationen": [], "keywords": {}})
        eintrag.update({k: v for k, v in stamm.items()})
        return eintrag

    def publikationen_setzen(self, name: str, publikationen: list[dict], hinweis: str = "") -> None:
        person = self.personen[name]
        publikationen = self._ohne_duplikate(publikationen)
        person["publikationen"] = publikationen
        person["quelle_hinweis"] = hinweis
        gewichte: Counter = Counter()
        for pub in publikationen:
            for kw in pub.get("keywords", []):
                if kw := keyword_bereinigen(kw):
                    gewichte[kw] += 3          # explizite Keywords zählen stark
            for kw in pub.get("zusammenfassung_keywords", []):
                if kw := keyword_bereinigen(kw):
                    gewichte[kw] += 2
            for t in tokens(pub.get("titel", "")):
                gewichte[t] += 2
            for t in tokens(pub.get("zusammenfassung", ""))[:200]:
                gewichte[t] += 1
        person["keywords"] = dict(gewichte.most_common(60))
        person["indexiert_am"] = datetime.now().isoformat(timespec="seconds")
        self._index_neu_bauen()

    # ---- Suche -------------------------------------------------------------
    def finde_person(self, text: str) -> dict | None:
        """Findet eine Person im Text: voller Name, Nachname als Wort, oder ähnlich geschrieben."""
        klein = text.lower()
        for name, p in self.personen.items():
            if name.lower() in klein:
                return p
        woerter = set(re.findall(r"[A-Za-zÄÖÜäöüß\-]+", klein))
        kandidaten = [p for p in self.personen.values() if p.get("nachname", "").lower() in woerter]
        if len(kandidaten) == 1:
            return kandidaten[0]
        if len(kandidaten) > 1:  # mehrere gleiche Nachnamen: Vorname entscheidet
            for p in kandidaten:
                if p.get("vorname", "").split()[:1] and p["vorname"].split()[0].lower() in woerter:
                    return p
            return kandidaten[0]
        nachnamen = {p.get("nachname", "").lower(): p for p in self.personen.values()}
        for wort in woerter:
            if len(wort) < 4:
                continue
            aehnlich = difflib.get_close_matches(wort, list(nachnamen), n=1, cutoff=0.85)
            if aehnlich:
                return nachnamen[aehnlich[0]]
        return None

    def _dokument(self, p: dict) -> str:
        teile = [" ".join([kw] * int(w)) for kw, w in p.get("keywords", {}).items()]
        for pub in p.get("publikationen", []):
            teile.append(pub.get("titel", ""))
            teile.append(pub.get("zusammenfassung", ""))
            teile.append(pub.get("abstract", "")[:1000])
        return " ".join(teile)

    def _index_neu_bauen(self) -> None:
        docs = {name: Counter(tokens(self._dokument(p))) for name, p in self.personen.items() if p.get("publikationen")}
        n = len(docs) or 1
        df: Counter = Counter()
        for c in docs.values():
            df.update(c.keys())
        self._idf = {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}
        self._vektoren = {}
        for name, c in docs.items():
            v = {t: (1 + math.log(f)) * self._idf[t] for t, f in c.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1
            self._vektoren[name] = {t: x / norm for t, x in v.items()}

    def rangliste(self, anfrage: str, top: int = 3, ausschluss: set[str] = frozenset()) -> list[dict]:
        """Cosinus-Ähnlichkeit (TF-IDF) zwischen Anfragetext und jedem Forschungsprofil."""
        q = Counter(tokens(anfrage))
        if not q or not self._vektoren:
            return []
        # Deutsche Anfragewörter auf englische Indexbegriffe abbilden, sofern sie
        # einen langen gemeinsamen Anfang haben: "interoperabilität" -> "interoperability"
        for t in list(q):
            if t in self._idf or len(t) < 8:
                continue
            stamm = t[:8]
            for begriff in self._idf:
                if begriff.startswith(stamm):
                    q[begriff] += q[t]
        qv = {t: (1 + math.log(f)) * self._idf.get(t, 1.0) for t, f in q.items()}
        norm = math.sqrt(sum(x * x for x in qv.values())) or 1
        qv = {t: x / norm for t, x in qv.items()}
        treffer = []
        for name, v in self._vektoren.items():
            if name in ausschluss:
                continue
            gemeinsame = {t: qv[t] * v[t] for t in qv if t in v}
            score = sum(gemeinsame.values())
            if score > 0:
                beitrag = sorted(gemeinsame, key=gemeinsame.get, reverse=True)[:6]
                treffer.append({"name": name, "score": round(score, 3), "begruendung": beitrag, "person": self.personen[name]})
        treffer.sort(key=lambda t: t["score"], reverse=True)
        return treffer[:top]

    def passende_publikationen(self, person: dict, anfrage: str, top: int = 3) -> list[dict]:
        """Veröffentlichungen der Person, die zur Anfrage passen. Der Name der Person
        und Frageworte zählen nicht als Suchbegriffe; bleibt nichts übrig, kommt []."""
        name_tokens = set(tokens(person.get("name", ""))) | set(tokens(person.get("anzeige", "")))
        q = set(tokens(anfrage)) - name_tokens - _FRAGEWOERTER
        if not q:
            return []
        # Vergleich über Wortanfänge, damit "Interoperabilität" auch "interoperability" trifft
        q_stamm = {t[:8] for t in q}
        bewertet = []
        for pub in person.get("publikationen", []):
            d = set(tokens(" ".join([pub.get("titel", ""), pub.get("abstract", ""), pub.get("zusammenfassung", "")])))
            d.update(k.lower() for k in pub.get("keywords", []))
            score = len(q_stamm & {t[:8] for t in d})
            if score:
                bewertet.append((score, pub))
        bewertet.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in bewertet[:top]]


# ---------------------------------------------------------------------------
# Der Agent
# ---------------------------------------------------------------------------
class ResearchAgent:
    PATH = "/research"

    SKILLS = [
        AgentSkill(
            id="find_reviewer",
            name="Find Reviewer",
            description=(
                "Wählt zu einer Review-Anfrage (Titel, Abstract oder PDF-Pfad eines Papers) immer "
                "genau ZWEI Gutachter:innen (Reviewer) unter den BIBA-Mitarbeitenden aus, basierend "
                "auf deren Veröffentlichungen. Zwei Meinungen sind im Peer-Review der Normalfall; "
                "Autor:innen des Papers werden ausgeschlossen."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "review", "reviewer", "peer-review"],
            examples=[
                "Review-Anfrage: Semantic interoperability for predictive maintenance in wind turbines",
                "Wer sollte dieses Paper reviewen? C:/papers/eingereicht.pdf",
                "Finde zwei Reviewer für ein Paper über digitale Zwillinge in der Logistik",
            ],
        ),
        AgentSkill(
            id="staff_profile",
            name="Staff Profile",
            description=(
                "Stellt das Forschungsprofil einer BIBA-Mitarbeiterin oder eines Mitarbeiters zusammen: "
                "Themen, Keywords, Veröffentlichungen, Kontakt."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "profil", "forschungsprofil", "mitarbeiter"],
            examples=["Erstelle das Forschungsprofil von Marco Franke", "Profil von Hribernik"],
        ),
        AgentSkill(
            id="staff_question",
            name="Staff Question",
            description=(
                "Beantwortet Fragen zu einer BIBA-Mitarbeiterin oder einem Mitarbeiter: Kontakt, Abteilung, "
                "Forschungsthemen, Anzahl und Titel der Veröffentlichungen, Ko-Autor:innen, oder wer im BIBA "
                "an einem Thema arbeitet."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "mitarbeiter", "frage", "publikationen"],
            examples=[
                "Woran forscht Marco Franke?",
                "Wie erreiche ich Karl Hribernik?",
                "Welche Veröffentlichungen hat Franke zu Interoperabilität?",
                "Wer im BIBA arbeitet an Predictive Maintenance?",
            ],
        ),
    ]
    SKILL = SKILLS[0]  # für agents_server.py, das `agent.SKILL.id` ausgibt

    _REVIEW = re.compile(r"\b(peer.?review\w*|review\w*|gutachter\w*|gutachten|begutacht\w*)\b", re.IGNORECASE)
    _PROFIL = re.compile(r"\b(profil|forschungsprofil|steckbrief|portrait|porträt)\b", re.IGNORECASE)
    _WER = re.compile(r"\b(wer|welche(r|s)? (mitarbeiter\w*|person\w*|kolleg\w*))\b", re.IGNORECASE)
    _PDF = re.compile(r"(?:[A-Za-z]:[\\/]|/|\.{0,2}[\\/])?[^\s\"']+\.pdf\b", re.IGNORECASE)

    def __init__(self) -> None:
        self.mcp = MCPVerbindung()
        self.gedaechtnis = Gedaechtnis(Path(os.environ.get("RESEARCH_MEMORY_FILE", "memory/forschungsindex.json")))
        self.max_staff = int(os.environ.get("RESEARCH_MAX_STAFF", "0"))
        self.max_pubs = int(os.environ.get("RESEARCH_MAX_PUBS", "3"))
        self.quelle = os.environ.get("RESEARCH_SOURCE", "auto")
        # ORCID-Abgleich beim Indexieren. `0` spart einen HTTP-Aufruf pro Person,
        # macht die Publikationssuche aber wieder anfällig für Namensvettern.
        self.orcid_suchen = os.environ.get("RESEARCH_ORCID", "1") == "1"
        # Wie viele Reviewer vorgeschlagen werden. Zwei ist der Normalfall im
        # Peer-Review: eine Zweitmeinung, ohne dass der Vorschlag ausufert.
        self.anzahl_reviewer = int(os.environ.get("RESEARCH_REVIEWER", "2"))
        self._index_task: asyncio.Task | None = None

    # ---- AgentCard ---------------------------------------------------------
    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent I – Research",
            description=(
                "Kennt die BIBA-Mitarbeitenden und ihre Veröffentlichungen (über einen MCP-Server), "
                "schlägt Reviewer vor, erstellt Forschungsprofile und beantwortet Fragen zu Personen."
            ),
            version="0.1.0",
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=True),
            supported_interfaces=[
                AgentInterface(protocol_binding=TransportProtocol.JSONRPC, url=f"{base_url}{self.PATH}", protocol_version="1.0")
            ],
            skills=self.SKILLS,
        )

    # ---- Lebenszyklus: Gedächtnis beim Hochfahren aufbauen -----------------
    async def beim_start(self) -> None:
        """Wird von agents_server.py beim Serverstart aufgerufen."""
        self.gedaechtnis.laden()
        print(f"[Research] Gedächtnis geladen: {len(self.gedaechtnis.personen)} Personen, "
              f"{self.gedaechtnis.status['indexiert']} indexiert ({self.gedaechtnis.datei})")
        await self.mcp.starten()
        print(f"[Research] MCP-Server verbunden, Tools: {', '.join(await self.mcp.tools())}")
        if os.environ.get("RESEARCH_INDEX_AT_STARTUP", "1") == "1":
            # Im Hintergrund, damit der Server sofort Anfragen annimmt.
            self.gedaechtnis.status["phase"] = "geplant"
            self._index_task = asyncio.create_task(self.indexieren(), name="research-indexieren")
        else:
            self.gedaechtnis.status["phase"] = "nur geladen"

    async def beim_stopp(self) -> None:
        if self._index_task and not self._index_task.done():
            self._index_task.cancel()
        await self.mcp.stoppen()

    async def indexieren(self) -> None:
        status = self.gedaechtnis.status
        status["phase"] = "läuft"
        try:
            personen = await self.mcp.call("list_biba_staff")
            if self.max_staff:
                personen = personen[: self.max_staff]
            # Personen, die nicht mehr auf der Webseite stehen (oder umbenannt wurden), vergessen
            aktuelle = {p["name"] for p in personen}
            entfernt = [n for n in self.gedaechtnis.personen if n not in aktuelle]
            for alt in entfernt:
                del self.gedaechtnis.personen[alt]
            if entfernt:
                # Der TF-IDF-Index zeigt sonst weiter auf die gelöschten Namen. Das
                # fällt erst bei der nächsten Rangliste auf – und auch nur dann,
                # wenn danach niemand mehr neu indexiert wird (denn das baut den
                # Index ohnehin neu). Tritt z. B. mit RESEARCH_MAX_STAFF auf.
                self.gedaechtnis._index_neu_bauen()
            status["gesamt"] = len(personen)
            status["indexiert"] = sum(1 for p in personen if self.gedaechtnis.personen.get(p["name"], {}).get("indexiert_am"))
            quelle = self.quelle
            orcid_nachgetragen = False
            for i, stamm in enumerate(personen, 1):
                eintrag = self.gedaechtnis.person_anlegen(stamm)
                # Die ORCID-iD wird auch für längst indexierte Personen nachgetragen.
                # Sie kostet einen einzigen Aufruf und wird im Gedächtnis vermerkt;
                # stünde sie erst hinter dem `continue`, bliebe das mitgelieferte
                # Gedächtnis für immer ohne diese Angabe.
                if await self._orcid_holen(eintrag):
                    orcid_nachgetragen = True
                if eintrag.get("indexiert_am"):
                    continue  # schon aus der Datei bekannt
                print(f"[Research] ({i}/{len(personen)}) {stamm['name']} ...")
                try:
                    ergebnis = await self.mcp.call(
                        "search_publications", autor=stamm["name"], max_results=self.max_pubs,
                        quelle=quelle, orcid=eintrag.get("orcid", ""),
                    )
                except RuntimeError as err:
                    status["hinweise"].append(f"{stamm['name']}: {err}")
                    ergebnis = {"publikationen": [], "hinweis": str(err)}
                if quelle == "auto" and "Google Scholar nicht nutzbar" in ergebnis.get("hinweis", ""):
                    print("[Research] Google Scholar blockt – für diesen Lauf direkt OpenAlex verwenden.")
                    quelle = "openalex"
                publikationen = ergebnis.get("publikationen", [])
                for pub in publikationen:
                    await self._zusammenfassen(pub)
                self.gedaechtnis.publikationen_setzen(stamm["name"], publikationen, ergebnis.get("hinweis", ""))
                status["indexiert"] += 1
                orcid_nachgetragen = False  # ist mitgespeichert worden
                self.gedaechtnis.speichern()
            if orcid_nachgetragen:
                # Alle waren schon indexiert, es gab also kein Speichern in der
                # Schleife – die neu gefundenen ORCID-iDs sollen trotzdem bleiben.
                self.gedaechtnis.speichern()
            status["phase"] = "fertig"
            mit_orcid = sum(1 for p in self.gedaechtnis.personen.values() if p.get("orcid"))
            print(f"[Research] Indexierung fertig: {status['indexiert']} Personen mit Veröffentlichungen "
                  f"geprüft, {mit_orcid} davon mit ORCID-iD am BIBA.")
        except asyncio.CancelledError:
            status["phase"] = "abgebrochen"
            raise
        except Exception as err:  # noqa: BLE001
            status["phase"] = f"Fehler: {err}"
            print(f"[Research] Indexierung abgebrochen: {err}")

    async def _orcid_holen(self, eintrag: dict) -> bool:
        """Trägt die ORCID-iD einer Person nach. True, wenn dabei etwas Neues entstand.

        Der Name von der BIBA-Webseite ist als Suchschlüssel unzuverlässig –
        "Michael Freitag" gibt es in der Wissenschaft mehrfach. Das MCP-Tool
        `find_orcid` sucht den Namen deshalb bei ORCID und nimmt nur das Profil,
        in dem das BIBA als Einrichtung steht. Diese iD identifiziert dann in
        `search_publications` die Person statt bloß ihren Namen.

        Das Ergebnis wird im Gedächtnis vermerkt – auch ein *negatives*
        (`orcid_geprueft_am` ohne `orcid`). Sonst würde bei jedem Serverstart
        erneut für alle ~86 Personen bei ORCID angefragt.
        """
        if not self.orcid_suchen or eintrag.get("orcid_geprueft_am"):
            return False
        try:
            gefunden = await self.mcp.call(
                "find_orcid",
                name=eintrag["name"],
                vorname=eintrag.get("vorname", ""),
                nachname=eintrag.get("nachname", ""),
            )
        except RuntimeError as err:
            # Kein Abbruch: Ohne ORCID läuft die Suche über den Namen weiter.
            self.gedaechtnis.status["hinweise"].append(f"ORCID {eintrag['name']}: {err}")
            return False
        eintrag["orcid"] = gefunden.get("orcid", "")
        eintrag["orcid_url"] = gefunden.get("url", "")
        eintrag["orcid_hinweis"] = gefunden.get("hinweis", "")
        eintrag["orcid_geprueft_am"] = datetime.now().isoformat(timespec="seconds")
        if eintrag["orcid"]:
            print(f"[Research] ORCID {eintrag['name']}: {eintrag['orcid']} ({gefunden.get('institution', '')})")
        return True

    async def _zusammenfassen(self, pub: dict) -> None:
        """PDF bevorzugt, sonst Abstract. Ergebnis landet in der Publikation selbst."""
        pub.setdefault("zusammenfassung", "")
        pub.setdefault("zusammenfassung_keywords", [])
        try:
            if pub.get("pdf_pfad"):
                z = await self.mcp.call("summarize_pdf", pfad=pub["pdf_pfad"])
                pub["zusammenfassung_quelle"] = "pdf"
            elif pub.get("abstract"):
                z = await self.mcp.call("summarize_text", text=pub["abstract"])
                pub["zusammenfassung_quelle"] = "abstract"
            else:
                return
            pub["zusammenfassung"] = z.get("zusammenfassung", "")
            pub["zusammenfassung_keywords"] = z.get("keywords", [])
        except RuntimeError as err:
            pub["zusammenfassung_quelle"] = f"fehler: {err}"

    # ---- Anfragen verstehen ------------------------------------------------
    def auftrag_aus_text(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("Leere Anfrage.")
        pdf = self._PDF.search(text)
        if self._REVIEW.search(text) or (pdf and not self.gedaechtnis.finde_person(text)):
            return {"art": "reviewer", "pdf": pdf.group(0) if pdf else "", "text": text}
        person = self.gedaechtnis.finde_person(text)
        if person and self._PROFIL.search(text):
            return {"art": "profil", "person": person}
        if person:
            return {"art": "frage", "person": person, "text": text}
        if self._WER.search(text):
            return {"art": "wer", "text": text}
        raise ValueError(
            "Ich habe keine bekannte Person im Text gefunden. Beispiele: "
            "'Profil von Marco Franke', 'Woran forscht Hribernik?', "
            "'Review-Anfrage: <Titel oder Abstract>', 'Wer arbeitet an Predictive Maintenance?' "
            + self._index_hinweis()
        )

    async def invoke(self, user_request: str) -> str:
        auftrag = self.auftrag_aus_text(user_request)
        if auftrag["art"] == "reviewer":
            return await self._reviewer(auftrag)
        if auftrag["art"] == "profil":
            return self._profil(auftrag["person"])
        if auftrag["art"] == "wer":
            return self._wer_arbeitet_an(auftrag["text"])
        return self._frage(auftrag["person"], auftrag["text"])

    # ---- Skill 1: Reviewer -------------------------------------------------
    async def _reviewer(self, auftrag: dict) -> str:
        anfrage = auftrag["text"]
        kopf = []
        if auftrag["pdf"]:
            pfad = auftrag["pdf"].strip("\"'")
            if not os.path.exists(pfad):
                raise ValueError(f"PDF nicht gefunden: {pfad}")
            z = await self.mcp.call("summarize_pdf", pfad=pfad)
            anfrage = " ".join([z["zusammenfassung"]] + z["keywords"] * 2)
            autoren_text = z.get("anfang", "")  # Titelseite: hier stehen die Verfasser
            kopf.append(f"Paper: {pfad} ({z['seiten']} Seiten)")
            kopf.append(f"Keywords des Papers: {', '.join(z['keywords'])}")
        else:
            titel = re.sub(r"^\W*(peer.?review|reviewer|review|gutachter|gutachten)[- ]?(anfrage|request)?\W*", "", anfrage, flags=re.IGNORECASE)
            titel = re.sub(r"^(wer|welche\w*)\s+(sollte|soll|kann|könnte)\s+(dieses|das|ein|dieses)?\s*(paper|manuskript|artikel)?\s*(über|zu)?\s*", "", titel, flags=re.IGNORECASE)
            titel = re.sub(r"\s*\b(reviewen|begutachten|review)\b\W*$", "", titel, flags=re.IGNORECASE)
            kopf.append("Paper: " + titel.strip(" :-–,?"))
            autoren_text = anfrage

        # Autor:innen des Papers sollen es nicht selbst begutachten
        ausschluss = self._genannte_personen(autoren_text)

        # Immer genau zwei Vorschläge – eine Erst- und eine Zweitbegutachtung.
        treffer = self.gedaechtnis.rangliste(anfrage, top=self.anzahl_reviewer, ausschluss=ausschluss)
        zeilen = kopf + ["", self._index_hinweis()]
        if not treffer:
            zeilen.append("Keine passenden Reviewer im Gedächtnis gefunden.")
            return "\n".join(zeilen)

        rollen = ["Erstgutachten", "Zweitgutachten"]
        zeilen.append(f"Vorgeschlagene Reviewer ({len(treffer)} von {self.anzahl_reviewer}):")
        for i, t in enumerate(treffer, 1):
            p = t["person"]
            rolle = f" – {rollen[i - 1]}" if i <= len(rollen) else ""
            orcid = f", ORCID {p['orcid']}" if p.get("orcid") else ""
            zeilen.append(
                f"  {i}. {p['name']} (Abt. {p.get('abteilung', '?')}, {p.get('email', '')}{orcid})"
                f" – Score {t['score']}{rolle}"
            )
            zeilen.append(f"     passende Begriffe: {', '.join(t['begruendung'])}")
            for pub in self.gedaechtnis.passende_publikationen(p, anfrage, top=2):
                zeilen.append(f"     • {pub.get('jahr', '?')}: {pub.get('titel', '')}")

        if len(treffer) < self.anzahl_reviewer:
            # Ehrlich bleiben: lieber eine Person nennen und das sagen, als eine
            # zweite ohne inhaltliche Überschneidung dazuzuerfinden.
            fehlend = self.anzahl_reviewer - len(treffer)
            zeilen.append(
                f"Nur {len(treffer)} statt {self.anzahl_reviewer} Vorschläge: Für {fehlend} weitere "
                "gibt es im Gedächtnis keine Person mit inhaltlicher Überschneidung zum Paper."
            )
        if ausschluss:
            zeilen.append(f"Ausgeschlossen (als Autor:in erkannt): {', '.join(sorted(ausschluss))}")
        return "\n".join(zeilen)

    def _genannte_personen(self, text: str) -> set[str]:
        """Mitarbeitende, deren Nachname UND Vorname (oder Initial) im Text vorkommen."""
        klein = re.sub(r"\s+", " ", text.lower())
        gefunden = set()
        for name, p in self.gedaechtnis.personen.items():
            nachname = p.get("nachname", "").lower()
            vorname = (p.get("vorname", "").split() or [""])[0].lower().rstrip(".")
            if not nachname or not re.search(rf"\b{re.escape(nachname)}\b", klein):
                continue
            if name.lower() in klein or (vorname and re.search(rf"\b{re.escape(vorname)}\b", klein)) \
                    or (vorname and re.search(rf"\b{re.escape(vorname[0])}\.\s*{re.escape(nachname)}\b", klein)):
                gefunden.add(name)
        return gefunden

    # ---- Skill 2: Profil ---------------------------------------------------
    def _profil(self, p: dict) -> str:
        pubs = p.get("publikationen", [])
        keywords = list(p.get("keywords", {}).items())[:12]
        zeilen = [
            f"Forschungsprofil: {p.get('titel', '')} {p['name']}".replace("  ", " ").strip(),
            f"Abteilung {p.get('abteilung', '?')} · Raum {p.get('raum', '?')} · {p.get('telefon', '')} · {p.get('email', '')}",
            f"Homepage: {p.get('homepage', '')}",
        ]
        if p.get("orcid_url"):
            zeilen.append(f"ORCID: {p['orcid_url']}")
        elif p.get("orcid_hinweis"):
            zeilen.append(f"ORCID: keine am BIBA gefunden ({p['orcid_hinweis']})")
        zeilen.append("")
        if not pubs:
            zeilen.append("Noch keine Veröffentlichungen im Gedächtnis. " + self._index_hinweis())
            if p.get("quelle_hinweis"):
                zeilen.append(f"Hinweis der Suche: {p['quelle_hinweis']}")
            return "\n".join(zeilen)
        jahre = sorted({str(pub.get("jahr")) for pub in pubs if pub.get("jahr")})
        zeitraum = "?" if not jahre else jahre[0] if len(jahre) == 1 else f"{jahre[0]}–{jahre[-1]}"
        zeilen.append(f"Forschungsschwerpunkte (aus {len(pubs)} Veröffentlichungen, {zeitraum}):")
        zeilen.append("  " + ", ".join(kw for kw, _ in keywords))
        zeilen.append("")
        zeilen.append("Veröffentlichungen:")
        for pub in pubs:
            zeilen.append(f"  • {pub.get('jahr', '?')}: {pub.get('titel', '')}")
            if pub.get("venue"):
                zeilen.append(f"    in: {pub['venue']}")
            if pub.get("url"):
                zeilen.append(f"    {pub['url']}")
            if pub.get("zusammenfassung"):
                zeilen.append(f"    Kurz: {pub['zusammenfassung'][:300]}{'…' if len(pub['zusammenfassung']) > 300 else ''}")
        koautoren = self._koautoren(p)
        if koautoren:
            zeilen.append("")
            zeilen.append("Häufige Ko-Autor:innen: " + ", ".join(f"{n} ({c})" for n, c in koautoren[:6]))
        zeilen.append("")
        zeilen.append(f"Quelle der Publikationsdaten: {pubs[0].get('quelle', '?')}" + (f" ({p['quelle_hinweis']})" if p.get("quelle_hinweis") else ""))
        return "\n".join(zeilen)

    def _koautoren(self, p: dict) -> list[tuple[str, int]]:
        z: Counter = Counter()
        eigener = p.get("nachname", "").lower()
        for pub in p.get("publikationen", []):
            for a in pub.get("autoren", []):
                if a and eigener not in a.lower():
                    z[a] += 1
        return z.most_common()

    # ---- Skill 3: Fragen ---------------------------------------------------
    def _frage(self, p: dict, text: str) -> str:
        t = text.lower()
        pubs = p.get("publikationen", [])
        name = p["name"]

        if re.search(r"\b(e-?mail|telefon\w*|nummer|erreich\w*|kontakt\w*|raum|büro|buero|abteilung|wo sitzt|durchwahl)\b", t):
            return (f"{p.get('titel', '')} {name}".strip() + f"\n  Abteilung: {p.get('abteilung', '?')}\n  Raum: {p.get('raum', '?')}"
                    f"\n  Telefon: {p.get('telefon', '?')}\n  E-Mail: {p.get('email', '?')}\n  Homepage: {p.get('homepage', '')}")

        if re.search(r"\b(wie viele|anzahl|wieviele)\b", t):
            return f"{name} hat {len(pubs)} Veröffentlichungen im Gedächtnis (max. {self.max_pubs} pro Person indexiert). " + self._index_hinweis()

        if re.search(r"\b(ko-?autor\w*|co-?autor\w*|zusammen mit|mit wem)\b", t):
            ko = self._koautoren(p)
            if not ko:
                return f"Zu {name} sind keine Ko-Autor:innen im Gedächtnis."
            return f"Ko-Autor:innen von {name}: " + ", ".join(f"{n} ({c})" for n, c in ko[:10])

        if re.search(r"\b(forscht|forschung|thema|themen|schwerpunkt\w*|forschungsgebiet\w*|interess\w*|arbeitet an|beschäftigt)\b", t):
            kws = list(p.get("keywords", {}))[:10]
            if not kws:
                return f"Zu {name} sind noch keine Themen bekannt. " + self._index_hinweis()
            neueste = max(pubs, key=lambda x: x.get("jahr") or 0) if pubs else None
            antwort = f"{name} forscht laut Veröffentlichungen zu: {', '.join(kws)}."
            if neueste:
                antwort += f"\nNeueste Arbeit ({neueste.get('jahr')}): {neueste.get('titel')}"
                if neueste.get("zusammenfassung"):
                    antwort += f"\n  {neueste['zusammenfassung'][:400]}"
            return antwort

        if re.search(r"\b(veröffentlichung\w*|publikation\w*|paper|papers|artikel|aufsatz|aufsätze|geschrieben)\b", t):
            passend = self.gedaechtnis.passende_publikationen(p, text, top=5)
            liste = passend or pubs
            titel = "passende" if passend and len(passend) < len(pubs) else "bekannte"
            if not liste:
                return f"Zu {name} sind keine Veröffentlichungen im Gedächtnis. " + self._index_hinweis()
            zeilen = [f"{len(liste)} {titel} Veröffentlichungen von {name}:"]
            for pub in liste:
                zeilen.append(f"  • {pub.get('jahr', '?')}: {pub.get('titel', '')}" + (f"  {pub['url']}" if pub.get("url") else ""))
            return "\n".join(zeilen)

        # Freie Frage: passende Publikationen + Kurzprofil
        passend = self.gedaechtnis.passende_publikationen(p, text, top=3)
        zeilen = [f"Zu {name} weiß ich Folgendes:",
                  f"  Abteilung {p.get('abteilung', '?')}, Themen: {', '.join(list(p.get('keywords', {}))[:8]) or 'noch keine'}"]
        if passend:
            zeilen.append("  Zur Frage passende Veröffentlichungen:")
            for pub in passend:
                zeilen.append(f"    • {pub.get('jahr', '?')}: {pub.get('titel', '')}")
                if pub.get("zusammenfassung"):
                    zeilen.append(f"      {pub['zusammenfassung'][:250]}")
        else:
            zeilen.append("  Keine Veröffentlichung passt direkt zur Frage. Frag z. B. nach Kontakt, Themen, Veröffentlichungen oder Ko-Autor:innen.")
        return "\n".join(zeilen)

    def _wer_arbeitet_an(self, text: str) -> str:
        thema = self._WER.sub("", text)
        thema = re.sub(r"\b(im|am|beim|bei|biba|arbeitet|arbeiten|forscht|forschen|an|zu|über|mit|sich|beschäftigt)\b", " ", thema, flags=re.IGNORECASE)
        treffer = self.gedaechtnis.rangliste(thema, top=5)
        if not treffer:
            return f"Niemand im Gedächtnis passt zu '{thema.strip()}'. " + self._index_hinweis()
        zeilen = [f"Zu '{thema.strip(' ?.')}' passen im BIBA:"]
        for t in treffer:
            p = t["person"]
            zeilen.append(f"  • {p['name']} (Abt. {p.get('abteilung', '?')}) – {', '.join(t['begruendung'][:4])}")
        return "\n".join(zeilen)

    def _index_hinweis(self) -> str:
        s = self.gedaechtnis.status
        return f"[Gedächtnis: {s['indexiert']}/{s['gesamt'] or len(self.gedaechtnis.personen)} Personen indexiert, Phase: {s['phase']}]"
