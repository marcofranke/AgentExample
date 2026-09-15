"""Agent H – Semantic Mediator: der volle Funktionsumfang des Semantischen
Mediators des BIBA (Bremer Institut für Produktion und Logistik) als A2A-Agent.

Der Semantische Mediator löst Interoperabilitätsprobleme zwischen heterogenen
Datenquellen. Wrapper bilden die lokale Sicht einer Quelle (CSV, JSON, REST,
SQL, XML, InfluxDB ...) über ein Mapping auf eine gemeinsame Ontologie ab. Der
Mediator führt diese Sichten zu einem virtuellen Schema zusammen, das sich per
SPARQL oder GraphQL abfragen und in Zielformate (CSV, JSON, AAS, Submodell)
exportieren lässt.

Der Mediator selbst ist eine Spring-Boot-Anwendung (Java 8, im Image Port 3053,
im docker-compose.yml dieses Projekts über SERVER_PORT auf 8081 gesetzt). Dieser
Agent ist eine *Hülle* darum: Er versteht deutschen Text, wählt den passenden
REST-Endpunkt und formuliert die Antwort wieder als Text. Die Fachlogik – das
Reasoning über die Ontologie – bleibt im Mediator.

    SEMANTIC_MEDIATOR_URL        Basis-URL, z. B. http://mediator:8081
    SEMANTIC_MEDIATOR_TOKEN      optional, wird als Bearer-Token mitgeschickt
    SEMANTIC_MEDIATOR_NAMESPACE  Vorgabe-Namensraum für SPARQL-Anfragen
    SEMANTIC_MEDIATOR_TIMEOUT    Sekunden pro Aufruf (Vorgabe 120)

Alles Endpunktabhängige steckt in `MediatorClient`. Weicht die eigene
Installation ab, muss NUR diese Klasse angepasst werden – Textverständnis und
A2A-Anbindung bleiben unverändert.

Die sechs Skills im Überblick (Beispielsätze stehen in den AgentSkill-Objekten):

    mediator_status     Läuft er? Welche Version? Welche Konfigurationen kennt er?
    mediator_query      SPARQL über alle föderierten Quellen
    mediator_graphql    dieselbe Föderation, per GraphQL gefragt
    mediator_transform  Daten einer Konfiguration in ein Zielformat überführen
    mediator_schema     Ontologie, Mapping und Datenpfade einer Konfiguration
    mediator_admin      Dienste neu laden, Triple Store setzen, Uploads schalten
"""

import base64
import json
import os
import re
import xml.etree.ElementTree as ET

import httpx

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol

# Wie viele Zeichen einer Mediator-Antwort der Agent höchstens weiterreicht.
# Ein CSV-Export kann Megabyte groß sein; das will weder ein Chatfenster noch
# ein 1,5-B-Modell sehen.
MAX_ZEICHEN = 4000


def _gekuerzt(text: str, grenze: int = MAX_ZEICHEN) -> str:
    """Lange Antworten abschneiden und den Schnitt sichtbar machen."""
    if len(text) <= grenze:
        return text
    return text[:grenze] + f"\n... [gekürzt, insgesamt {len(text)} Zeichen]"


class MediatorClient:
    """Dünne REST-Hülle um den Semantischen Mediator.

    Die Pfade entsprechen `semanticfederationmoduleServiceController` und
    `ImportWrapperController` im Java-Projekt (git/semanticmediator). Hier – und
    nur hier – anpassen, wenn die eigene Installation abweicht.
    """

    # ---- Zustand und Selbstauskunft -----------------------------------------
    PFAD_HELLO = "/hello"                              # GET  ?name=  -> Echo, Lebenszeichen
    PFAD_VERSION = "/version"                          # GET  -> Version der Reasoning-Engine
    PFAD_DATENQUELLEN = "/getCurrentDataSources"       # GET  -> Inhalt der config.xml
    PFAD_TRIPLESTORE_LESEN = "/getAppliedTripleStore"  # GET

    # ---- Abfragen ------------------------------------------------------------
    PFAD_QUERY = "/query"                              # GET  ?query={"namespace":…,"query":…}
    PFAD_QUERY_POST = "/queryPost"                     # POST {query, namespace} -> CSV
    PFAD_GRAPHQL = "/queryGraphQL"                     # POST GraphQL-Dokument als Rohtext

    # ---- Daten holen und exportieren ----------------------------------------
    PFAD_EXPORT = "/forwardRealDataSource"             # POST {configurationID, responseType, referenceModell}
    PFAD_VIRTUELL = "/forwardVirtualDataSource2"       # POST {configurationID, pathToCurrentDataSource}
    PFAD_VIRTUELL_INHALT = "/createVirtualDataSource"  # POST {configurationID, fileContentAsBase64}

    # ---- Wrapper einrichten und inspizieren ---------------------------------
    PFAD_IMPORT = "/import/importWrapperConfiguration"          # POST {mappingXml, ontologyOwl, …}
    PFAD_ONTOLOGIE = "/getOntologyFromDataSourceConfiguration"  # GET  ?id=&subid=
    PFAD_MAPPING = "/getMappingFromDataSourceConfiguration"     # GET  ?id=
    PFAD_DATENPFADE = "/requestDataPathesFromWrapper"           # POST {filePath, wrapperJavaClass, …}

    # ---- Verwaltung ----------------------------------------------------------
    PFAD_RELOAD = "/reloadServices"                     # GET
    PFAD_RELOAD_EINZELN = "/reloadSingleService"        # GET  /{id}
    PFAD_TRIPLESTORE_SETZEN = "/setAppliedTripleStore"  # GET  ?url=
    PFAD_INFLUX_AN = "/activateInfluxUpload"            # GET
    PFAD_INFLUX_AUS = "/deactivateInfluxUpload"         # GET
    PFAD_FUSEKI_AN = "/activateFusekiUpload"            # GET
    PFAD_FUSEKI_AUS = "/deactivateFusekiUpload"         # GET
    PFAD_KAFKA = "/publishOnKafka"                      # POST Rohtext -> Kafka-Topic

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("SEMANTIC_MEDIATOR_URL", "")).rstrip("/")
        self.token = token or os.environ.get("SEMANTIC_MEDIATOR_TOKEN")
        self.timeout = float(os.environ.get("SEMANTIC_MEDIATOR_TIMEOUT", "120"))

    # ---- Grundlagen ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "*/*", "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _pruefe_konfiguration(self) -> None:
        if not self.base_url:
            raise ValueError(
                "SEMANTIC_MEDIATOR_URL ist nicht gesetzt. Im docker-compose.yml dieses "
                "Projekts steht dort http://mediator:8081; lokal z. B. "
                "set SEMANTIC_MEDIATOR_URL=http://127.0.0.1:8081"
            )

    async def _get(self, http: httpx.AsyncClient, pfad: str, **params) -> httpx.Response:
        self._pruefe_konfiguration()
        antwort = await http.get(f"{self.base_url}{pfad}", headers=self._headers(), params=params or None)
        antwort.raise_for_status()
        return antwort

    async def _post(self, http: httpx.AsyncClient, pfad: str, nutzlast: dict | str) -> httpx.Response:
        self._pruefe_konfiguration()
        ziel = f"{self.base_url}{pfad}"
        if isinstance(nutzlast, str):
            # GraphQL und Kafka erwarten den Rohtext, nicht ein JSON-Objekt.
            antwort = await http.post(ziel, headers=self._headers(), content=nutzlast.encode("utf-8"))
        else:
            antwort = await http.post(ziel, headers=self._headers(), json=nutzlast)
        antwort.raise_for_status()
        return antwort

    # ---- Zustand und Selbstauskunft -----------------------------------------
    async def hallo(self, http: httpx.AsyncClient, name: str = "A2A") -> str:
        return (await self._get(http, self.PFAD_HELLO, name=name)).text

    async def version(self, http: httpx.AsyncClient) -> str:
        return (await self._get(http, self.PFAD_VERSION)).text

    async def konfiguration_roh(self, http: httpx.AsyncClient) -> str:
        """Die config.xml, wie der Mediator sie gerade benutzt."""
        return (await self._get(http, self.PFAD_DATENQUELLEN)).text

    async def triplestore(self, http: httpx.AsyncClient) -> str:
        return (await self._get(http, self.PFAD_TRIPLESTORE_LESEN)).text

    # ---- Abfragen ------------------------------------------------------------
    async def sparql(self, http: httpx.AsyncClient, query: str, namespace: str) -> dict:
        """SPARQL über alle föderierten Quellen. Ergebnis: {columnNames, data}.

        Der Mediator erwartet Anfrage und Namensraum als JSON *innerhalb* des
        Query-Parameters – nicht als zwei getrennte Parameter.
        """
        paket = json.dumps({"namespace": namespace, "query": query})
        antwort = await self._get(http, self.PFAD_QUERY, query=paket)
        if not antwort.text.strip() or antwort.text.strip() == "null":
            # Bei einem Fehler liefert der Endpunkt `null` statt eines 500ers.
            raise ValueError(
                "Der Mediator hat kein Ergebnis geliefert. Meist stimmt der Namensraum nicht, "
                "oder ein Konzept aus der Anfrage steht in keiner angeschlossenen Ontologie."
            )
        return antwort.json()

    async def sparql_als_csv(self, http: httpx.AsyncClient, query: str, namespace: str) -> str:
        antwort = await self._post(http, self.PFAD_QUERY_POST, {"query": query, "namespace": namespace})
        return antwort.text

    async def graphql(self, http: httpx.AsyncClient, dokument: str) -> dict:
        antwort = await self._post(http, self.PFAD_GRAPHQL, dokument)
        if not antwort.text.strip() or antwort.text.strip() == "null":
            raise ValueError(
                "Der Mediator hat auf die GraphQL-Anfrage nichts geliefert. Der Namensraum "
                'gehört in das Argument `site:`, z. B. Observation(site: "http://…").'
            )
        return antwort.json()

    # ---- Daten holen und exportieren ----------------------------------------
    async def exportiere(
        self, http: httpx.AsyncClient, konfiguration: int, format: str, referenzmodell: str | None = None
    ) -> str:
        """Führt die in der config.xml hinterlegte Query einer Konfiguration aus und
        gibt das Ergebnis im gewünschten Zielformat zurück (CSV, JSON, AAS, ...)."""
        nutzlast: dict = {"configurationID": konfiguration, "responseType": format}
        if referenzmodell:
            nutzlast["referenceModell"] = referenzmodell
        return (await self._post(http, self.PFAD_EXPORT, nutzlast)).text

    async def virtuelle_quelle(self, http: httpx.AsyncClient, konfiguration: int, inhalt: str) -> str:
        """Schickt *mitgelieferte* Daten durch den Wrapper einer Konfiguration.

        Der Mediator legt den Inhalt als Datei ab und liest ihn mit demselben
        Wrapper, der sonst die echte Quelle liest. So lässt sich eine
        Transformation ausprobieren, ohne die Quelle anzufassen.
        """
        nutzlast = {
            "configurationID": konfiguration,
            "fileContentAsBase64": base64.b64encode(inhalt.encode("utf-8")).decode("ascii"),
        }
        return (await self._post(http, self.PFAD_VIRTUELL_INHALT, nutzlast)).text

    async def virtuelle_quelle_aus_pfad(self, http: httpx.AsyncClient, konfiguration: int, pfad: str) -> str:
        """Wie `virtuelle_quelle`, aber die Datei liegt schon im Mediator-Container."""
        nutzlast = {"configurationID": konfiguration, "pathToCurrentDataSource": pfad}
        return (await self._post(http, self.PFAD_VIRTUELL, nutzlast)).text

    # ---- Wrapper einrichten und inspizieren ---------------------------------
    async def importiere_wrapper(self, http: httpx.AsyncClient, beschreibung: dict) -> dict:
        """Legt eine neue Wrapper-Konfiguration an (Ontologie + Mapping + Properties).

        Der Mediator schreibt die Dateien nach ./wrappers/<UUID>/, trägt den Dienst
        in die config.xml ein und liefert dessen `configurationID` zurück – genau
        die Zahl, die alle anderen Skills als "Konfiguration" brauchen.
        """
        pflicht = ["mappingXml", "ontologyOwl", "configProperties", "namespace", "configType"]
        fehlt = [f for f in pflicht if not beschreibung.get(f)]
        if fehlt:
            raise ValueError("Der Wrapper-Beschreibung fehlen die Felder: " + ", ".join(fehlt))
        if beschreibung["configType"] not in ("CSV", "JSON", "REST", "INFLUX"):
            raise ValueError("configType muss CSV, JSON, REST oder INFLUX sein.")
        return (await self._post(http, self.PFAD_IMPORT, beschreibung)).json()

    async def ontologie(self, http: httpx.AsyncClient, konfiguration: int, datenquelle: int = 0) -> str:
        return (await self._get(http, self.PFAD_ONTOLOGIE, id=konfiguration, subid=datenquelle)).text

    async def mapping(self, http: httpx.AsyncClient, konfiguration: int) -> str:
        return (await self._get(http, self.PFAD_MAPPING, id=konfiguration)).text

    async def datenpfade(self, http: httpx.AsyncClient, pfad: str, wrapper_klasse: str) -> list[str]:
        """Fragt einen Wrapper, welche Datenpfade (Spalten, Tags, JSON-Pfade) er in einer Datei sieht."""
        nutzlast = {"filePath": pfad, "wrapperJavaClass": wrapper_klasse, "templateParams": {}}
        ergebnis = (await self._post(http, self.PFAD_DATENPFADE, nutzlast)).json()
        return ergebnis.get("pathes", [])

    # ---- Verwaltung ----------------------------------------------------------
    async def neu_laden(self, http: httpx.AsyncClient, konfiguration: int | None = None) -> str:
        if konfiguration is None:
            return (await self._get(http, self.PFAD_RELOAD)).text
        return (await self._get(http, f"{self.PFAD_RELOAD_EINZELN}/{konfiguration}")).text

    async def triplestore_setzen(self, http: httpx.AsyncClient, url: str) -> str:
        return (await self._get(http, self.PFAD_TRIPLESTORE_SETZEN, url=url)).text

    async def upload_schalten(self, http: httpx.AsyncClient, ziel: str, an: bool) -> str:
        pfade = {
            ("influx", True): self.PFAD_INFLUX_AN,
            ("influx", False): self.PFAD_INFLUX_AUS,
            ("fuseki", True): self.PFAD_FUSEKI_AN,
            ("fuseki", False): self.PFAD_FUSEKI_AUS,
        }
        return (await self._get(http, pfade[(ziel, an)])).text

    async def auf_kafka(self, http: httpx.AsyncClient, nutzlast: str) -> str:
        return (await self._post(http, self.PFAD_KAFKA, nutzlast)).text


def dienste_aus_config(config_xml: str) -> list[dict]:
    """Liest die config.xml des Mediators in eine Liste von Diensten.

    Das ist die Selbstauskunft, die der Agent am häufigsten braucht: Welche
    `configurationID` gibt es, in welchem Namensraum liegt sie, welche Wrapper
    hängen daran? Ohne diese IDs lässt sich kein Export und kein Reload starten.
    """
    try:
        wurzel = ET.fromstring(config_xml)
    except ET.ParseError as err:
        raise ValueError(f"Die Konfiguration des Mediators ist kein gültiges XML: {err}") from err

    dienste = []
    for service in wurzel.findall("Service"):
        quellen = [
            {
                "config": (q.findtext("pathToConfigFile") or "").strip(),
                "wrapper": (q.findtext("javaClassForWrapper") or "").strip(),
            }
            for q in service.findall("DataSource")
        ]
        dienste.append(
            {
                "id": (service.findtext("id") or "?").strip(),
                "namespace": (service.findtext("namespace") or "").strip(),
                # Die Query steht mehrzeilig und eingerückt in der XML-Datei.
                "query": " ".join((service.findtext("query") or "").split()),
                "quellen": quellen,
            }
        )
    return dienste


class SemanticMediatorAgent:
    """Die 'Intelligenz' des Mediator-Agenten: Auftrag aus dem Text lesen, Mediator rufen."""

    PATH = "/mediator"

    SKILLS = [
        AgentSkill(
            id="mediator_status",
            name="Mediator-Status und Datenquellen",
            description=(
                "Fragt den Semantischen Mediator des BIBA nach seinem Zustand: Läuft er, welche "
                "Version hat die Reasoning-Engine, welche Datenquellen, Wrapper und "
                "Konfigurationen (configurationID) sind registriert, welcher Triple Store ist "
                "eingestellt. Erste Anlaufstelle, um die IDs für alle anderen Mediator-Aufgaben "
                "zu erfahren."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "interoperability", "status"],
            examples=[
                "Welche Datenquellen kennt der Semantische Mediator?",
                "Welche Konfigurationen sind im Mediator registriert?",
                "Läuft der Semantische Mediator?",
                "Welche Version hat der Mediator?",
                "Welcher Triple Store ist im Mediator eingestellt?",
            ],
        ),
        AgentSkill(
            id="mediator_query",
            name="SPARQL-Anfrage über föderierte Datenquellen",
            description=(
                "Führt eine SPARQL-Anfrage über das virtuelle Schema des Semantischen Mediators "
                "aus. Der Mediator übersetzt sie in Anfragen an alle angeschlossenen Wrapper "
                "(CSV, JSON, REST, SQL, InfluxDB) und liefert eine gemeinsame Ergebnistabelle "
                "zurück – Datenföderation über eine gemeinsame Ontologie."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "sparql", "ontologie", "föderation"],
            examples=[
                "Frage den Mediator: select ?B ?C where { ?A a <Observation>. ?A <dateTime> ?B. "
                "?A <hasSimplifiedValue> ?C. }",
                "SPARQL im Namensraum http://www.levelup-project.eu/ontologies: "
                "select ?B where { ?A a <Observation>. ?A <dateTime> ?B. }",
                "Frage den Mediator als CSV: select ?B where { ?A a <Observation>. ?A <dateTime> ?B. }",
            ],
        ),
        AgentSkill(
            id="mediator_graphql",
            name="GraphQL-Anfrage über föderierte Datenquellen",
            description=(
                "Dieselbe Datenföderation wie die SPARQL-Anfrage, nur in GraphQL formuliert. Der "
                "Mediator übersetzt das GraphQL-Dokument nach SPARQL, fragt die Wrapper und gibt "
                "das Ergebnis als GraphQL-Antwort zurück. Der Namensraum steht im Argument site."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "graphql", "föderation"],
            examples=[
                'GraphQL an den Mediator: { Observation(site: "http://www.levelup-project.eu/ontologies") '
                "{ dateTime hasSimplifiedValue } }",
                'Frage per GraphQL: { Observation(site: "http://KIPRO.de") { dateTime } }',
            ],
        ),
        AgentSkill(
            id="mediator_transform",
            name="Daten transformieren und exportieren",
            description=(
                "Transformiert Daten zwischen Datenmodellen: Der Mediator liest eine "
                "Konfiguration über ihren Wrapper ein und gibt das Ergebnis im Zielformat aus – "
                "CSV, JSON, AAS (Asset Administration Shell), SUBMODEL oder OPD. Mitgelieferte "
                "Daten lassen sich als virtuelle Datenquelle durch denselben Wrapper schicken, "
                "ohne die echte Quelle anzufassen."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "transformieren", "export", "aas", "interoperability"],
            examples=[
                "Exportiere Konfiguration 28 als CSV",
                "Transformiere Konfiguration 28 nach AAS",
                "Gib mir die Daten der Konfiguration 28 als JSON",
                "Transformiere diese Daten mit Konfiguration 28: "
                "timestamp,sensor_id,sensor_type,value,unit",
            ],
        ),
        AgentSkill(
            id="mediator_schema",
            name="Ontologie, Mapping und Datenpfade einsehen",
            description=(
                "Zeigt die semantische Beschreibung einer Konfiguration: die Ontologie (OWL), das "
                "Mapping (XML) zwischen Quellfeldern und Ontologiekonzepten, oder – für eine noch "
                "nicht angeschlossene Datei – die Datenpfade, die ein Wrapper darin findet. "
                "Außerdem lässt sich damit eine neue Wrapper-Konfiguration importieren."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "ontologie", "mapping", "wrapper"],
            examples=[
                "Zeige die Ontologie von Konfiguration 28",
                "Zeige das Mapping der Konfiguration 28",
                "Welche Datenpfade findet der Wrapper "
                "de.biba.wrapper.tableCSVWrapper.BigDataCSVWrapper in /config/SCADA/example_data.csv?",
                'Importiere diese Wrapper-Konfiguration: {"configType": "CSV", "namespace": "http://…", '
                '"ontologyOwl": "…", "mappingXml": "…", "configProperties": "…"}',
            ],
        ),
        AgentSkill(
            id="mediator_admin",
            name="Mediator verwalten",
            description=(
                "Verwaltet den laufenden Mediator: Dienste neu laden (nach dem Import einer "
                "Wrapper-Konfiguration nötig), den Triple Store umstellen, den Upload nach "
                "InfluxDB oder Fuseki ein- und ausschalten, eine Nachricht auf Kafka "
                "veröffentlichen."
            ),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            tags=["biba", "semantic-mediator", "verwaltung", "triplestore", "influx", "kafka"],
            examples=[
                "Lade die Dienste des Mediators neu",
                "Lade Konfiguration 28 im Mediator neu",
                "Setze den Triple Store des Mediators auf http://fuseki:3030/demo",
                "Schalte den Influx-Upload des Mediators aus",
            ],
        ),
    ]

    SKILL = SKILLS[0]  # für agents_server.py, das `agent.SKILL.id` ausgibt

    # ---- Textmuster ---------------------------------------------------------
    # "Konfiguration 28", "Datenquelle 16", "configurationID=27", "Dienst Nr. 3"
    _KONFIG = re.compile(
        r"\b(?:konfiguration(?:sid)?|configuration(?:id)?|config|datenquelle|dienst|service)\b"
        r"\s*(?:id|nr\.?|nummer)?\s*[:=#]?\s*(\d+)",
        re.IGNORECASE,
    )
    # Zielformat des Exports; SUBMODELL (deutsch) wird auf SUBMODEL abgebildet.
    _FORMAT = re.compile(r"\b(CSV|JSON|AAS|SUBMODELL?|OPD)\b", re.IGNORECASE)
    _SPARQL = re.compile(r"\bselect\b.*?\bwhere\b\s*\{.*\}", re.IGNORECASE | re.DOTALL)
    _NAMESPACE = re.compile(r"(?:namespace|namensraum)\s*[:=]?\s*(https?://\S+)", re.IGNORECASE)
    _ALS_CSV = re.compile(r"\bals\s+csv\b", re.IGNORECASE)
    _GRAPHQL = re.compile(r"\bgraphql\b", re.IGNORECASE)
    _JSON = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)

    _STATUS = re.compile(
        r"\b(status|zustand|läuft|laeuft|lebt|erreichbar|version|datenquellen|datenmodelle|"
        r"modelle|konfigurationen|registriert|kennt|welche|liste|überblick|ueberblick)\b",
        re.IGNORECASE,
    )
    # "Lade die Dienste neu" trennt das Verb – deshalb zwei Muster statt einem:
    # entweder ein eindeutiges Wort, oder irgendeine Form von "laden" plus "neu".
    _RELOAD = re.compile(r"\b(reload|aktualisiere\w*|neulade\w*)\b", re.IGNORECASE)
    _LADEN = re.compile(r"\blad\w*\b", re.IGNORECASE)
    _NEU = re.compile(r"\b(neu|erneut)\b", re.IGNORECASE)
    _TRIPLESTORE = re.compile(r"\b(triple\s*store|triplestore|fuseki)\b", re.IGNORECASE)
    _INFLUX = re.compile(r"\binflux(db)?\b", re.IGNORECASE)
    _KAFKA = re.compile(r"\bkafka\b", re.IGNORECASE)
    _AN = re.compile(r"\b(aktiviere\w*|anschalten|einschalten|an|ein)\b", re.IGNORECASE)
    _AUS = re.compile(r"\b(deaktiviere\w*|ausschalten|abschalten|aus|stoppe)\b", re.IGNORECASE)
    _URL = re.compile(r"(https?://\S+)")

    _ONTOLOGIE = re.compile(r"\b(ontologie|ontology|owl|schema|konzepte)\b", re.IGNORECASE)
    _MAPPING = re.compile(r"\b(mapping|abbildung|zuordnung)\b", re.IGNORECASE)
    _DATENPFADE = re.compile(r"\b(datenpfade|pfade|tags|spalten|pathes|paths)\b", re.IGNORECASE)
    _WRAPPER_KLASSE = re.compile(r"\b(de\.biba\.wrapper\.[\w.]+)")
    _DATEI = re.compile(r"((?:[A-Za-z]:)?[/\\][\w.\-/\\]+\.(?:csv|json|xml|txt|xlsx))", re.IGNORECASE)

    _EXPORT = re.compile(
        r"\b(exportiere\w*|export|transformiere\w*|übersetze|uebersetze|wandle|konvertiere|"
        r"gib|hole|liefere|zeige|daten)\b",
        re.IGNORECASE,
    )

    def __init__(self, client: MediatorClient | None = None) -> None:
        self.client = client or MediatorClient()
        # Steht im Text kein Namensraum, nimmt der Agent diesen. Die Vorgabe passt
        # zum SCADA-Beispiel, das im docker-compose.yml mitgeliefert wird.
        self.namespace = os.environ.get(
            "SEMANTIC_MEDIATOR_NAMESPACE", "http://www.levelup-project.eu/ontologies"
        )

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent H – Semantic Mediator",
            description=(
                "Ein Agent für den Semantischen Mediator des BIBA: föderiert heterogene "
                "Datenquellen über eine Ontologie, beantwortet SPARQL- und GraphQL-Anfragen "
                "darüber und transformiert die Ergebnisse in Zielformate wie CSV, JSON oder AAS."
            ),
            version="0.2.0",
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
            skills=self.SKILLS,
        )

    # ---- Anfragen verstehen -------------------------------------------------
    def auftrag_aus_text(self, text: str) -> dict:
        """Zerlegt den Text in einen Auftrag: {"art": ..., weitere Felder}.

        Die Reihenfolge der Prüfungen ist der ganze Trick. Zuerst kommt, was sich
        eindeutig erkennen lässt (eine SPARQL-Anfrage, ein JSON-Block mit
        Wrapper-Feldern), zuletzt das Allgemeine. Wie bei ResearchAgent routet der
        Orchestrator nur *zum Agenten*; die Aufteilung auf die Skills passiert hier.
        """
        text = text.strip()
        if not text:
            raise ValueError("Leere Anfrage.")

        json_block = self._JSON.search(text)
        konfig = self._KONFIG.search(text)
        konfig_id = int(konfig.group(1)) if konfig else None

        # 1. Wrapper-Konfiguration importieren – erkennbar am JSON mit den Pflichtfeldern
        if json_block and "mappingXml" in text and "ontologyOwl" in text:
            try:
                beschreibung = json.loads(json_block.group(1))
            except json.JSONDecodeError as err:
                raise ValueError(f"Die Wrapper-Beschreibung ist kein gültiges JSON: {err}") from err
            return {"art": "import", "beschreibung": beschreibung}

        # 2. GraphQL – ausdrücklich benannt oder am `site:`-Argument erkennbar
        if self._GRAPHQL.search(text) or ("site:" in text and "{" in text):
            return {"art": "graphql", "dokument": self._graphql_dokument(text)}

        # 3. SPARQL
        treffer = self._SPARQL.search(text)
        if treffer:
            namensraum = self._NAMESPACE.search(text)
            return {
                "art": "sparql",
                "query": treffer.group(0).strip(),
                "namespace": self._url_bereinigt(namensraum.group(1)) if namensraum else self.namespace,
                "als_csv": bool(self._ALS_CSV.search(text)),
            }

        # 4. Verwaltung
        if self._KAFKA.search(text) and json_block:
            return {"art": "kafka", "nutzlast": json_block.group(1)}
        if self._RELOAD.search(text) or (self._LADEN.search(text) and self._NEU.search(text)):
            return {"art": "reload", "konfiguration": konfig_id}
        if self._TRIPLESTORE.search(text):
            url = self._URL.search(text)
            if url:
                return {"art": "triplestore_setzen", "url": self._url_bereinigt(url.group(1))}
            if self._AN.search(text) or self._AUS.search(text):
                return {"art": "upload", "ziel": "fuseki", "an": not self._AUS.search(text)}
            return {"art": "triplestore_lesen"}
        if self._INFLUX.search(text):
            return {"art": "upload", "ziel": "influx", "an": not self._AUS.search(text)}

        # 5. Schema einsehen – Ontologie, Mapping, Datenpfade
        wrapper = self._WRAPPER_KLASSE.search(text)
        if wrapper and self._DATENPFADE.search(text):
            datei = self._DATEI.search(text)
            if not datei:
                raise ValueError(
                    "Für die Datenpfade brauche ich einen Dateipfad im Mediator-Container, "
                    "z. B. /config/SCADA/example_data.csv"
                )
            return {"art": "datenpfade", "pfad": datei.group(1), "wrapper": wrapper.group(1)}
        if konfig_id is not None and self._MAPPING.search(text):
            return {"art": "mapping", "konfiguration": konfig_id}
        if konfig_id is not None and self._ONTOLOGIE.search(text):
            return {"art": "ontologie", "konfiguration": konfig_id}

        # 6. Transformieren und exportieren
        if konfig_id is not None:
            nutzdaten = self._nutzdaten(text, json_block)
            if nutzdaten:
                return {"art": "virtuell", "konfiguration": konfig_id, "inhalt": nutzdaten}
            datei = self._DATEI.search(text)
            if datei and self._EXPORT.search(text):
                return {"art": "virtuell_pfad", "konfiguration": konfig_id, "pfad": datei.group(1)}
            format_treffer = self._FORMAT.search(text)
            if format_treffer or self._EXPORT.search(text):
                return {
                    "art": "export",
                    "konfiguration": konfig_id,
                    "format": self._format_normiert(format_treffer),
                }

        # 7. Zustand und Selbstauskunft
        if self._STATUS.search(text):
            return {"art": "status"}

        raise ValueError(
            "Ich verstehe den Auftrag an den Semantischen Mediator nicht. Beispiele:\n"
            "  • Welche Datenquellen kennt der Mediator?\n"
            "  • Frage den Mediator: select ?B where { ?A a <Observation>. ?A <dateTime> ?B. }\n"
            "  • Exportiere Konfiguration 28 als CSV\n"
            "  • Zeige die Ontologie von Konfiguration 28\n"
            "  • Lade die Dienste des Mediators neu"
        )

    def _url_bereinigt(self, url: str) -> str:
        """Satzzeichen am Ende einer URL abschneiden.

        "… im Namensraum http://example.org/onto: select …" liefert sonst einen
        Namensraum mit Doppelpunkt. Der Doppelpunkt *innerhalb* der URL (Port)
        bleibt unangetastet, weil nur das Ende betrachtet wird.
        """
        return url.rstrip(".,;:!?)")

    def _graphql_dokument(self, text: str) -> str:
        """Schneidet das GraphQL-Dokument aus dem Fließtext heraus."""
        anfang = text.find("{")
        if anfang == -1:
            raise ValueError(
                "Ich finde kein GraphQL-Dokument. Erwartet wird z. B.: "
                '{ Observation(site: "http://…") { dateTime } }'
            )
        return text[anfang:].strip()

    def _nutzdaten(self, text: str, json_block: re.Match | None) -> str:
        """Findet mitgelieferte Daten für eine virtuelle Datenquelle.

        Zwei Formen: ein JSON-Block, oder alles nach dem ersten Doppelpunkt, wenn
        die erste Zeile wie eine CSV-Kopfzeile aussieht (mindestens zwei Trenner).
        """
        if json_block:
            return json_block.group(1)
        _, trenner, rest = text.partition(":")
        rest = rest.strip()
        if not trenner or not rest:
            return ""
        erste = rest.splitlines()[0]
        return rest if erste.count(",") >= 2 or erste.count(";") >= 2 else ""

    def _format_normiert(self, treffer: re.Match | None) -> str:
        """Zielformat auf die Werte des ResponseType-Enums im Mediator bringen."""
        if not treffer:
            return "CSV"  # dieselbe Vorgabe wie im Mediator selbst
        wert = treffer.group(1).upper()
        return "SUBMODEL" if wert.startswith("SUBMODEL") else wert

    # ---- Aufträge ausführen -------------------------------------------------
    async def invoke(self, user_request: str) -> str:
        auftrag = self.auftrag_aus_text(user_request)
        behandler = {
            "status": self._status,
            "sparql": self._sparql,
            "graphql": self._graphql,
            "export": self._export,
            "virtuell": self._virtuell,
            "virtuell_pfad": self._virtuell_pfad,
            "ontologie": self._ontologie,
            "mapping": self._mapping,
            "datenpfade": self._datenpfade,
            "import": self._import,
            "reload": self._reload,
            "triplestore_lesen": self._triplestore_lesen,
            "triplestore_setzen": self._triplestore_setzen,
            "upload": self._upload,
            "kafka": self._kafka,
        }
        async with httpx.AsyncClient(timeout=self.client.timeout) as http:
            return await behandler[auftrag["art"]](http, auftrag)

    async def _status(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        """Ein Lagebericht: Version, Triple Store und alle registrierten Dienste."""
        zeilen = [f"Semantischer Mediator unter {self.client.base_url} ist erreichbar."]
        zeilen.append((await self.client.version(http)).strip())
        try:
            zeilen.append("Triple Store: " + (await self.client.triplestore(http)).strip())
        except httpx.HTTPError:
            pass  # nicht jede Installation hat einen Triple Store konfiguriert

        dienste = dienste_aus_config(await self.client.konfiguration_roh(http))
        if not dienste:
            zeilen.append(
                "\nEs ist noch keine Datenquelle registriert. Eine neue Wrapper-Konfiguration "
                "lässt sich über den Skill 'Ontologie, Mapping und Datenpfade einsehen' importieren."
            )
            return "\n".join(zeilen)

        zeilen.append(f"\n{len(dienste)} registrierte Konfiguration(en):")
        for dienst in dienste:
            zeilen.append(f"  • Konfiguration {dienst['id']}  (Namensraum: {dienst['namespace'] or 'ohne'})")
            for quelle in dienst["quellen"]:
                kurz = quelle["wrapper"].rsplit(".", 1)[-1] or "?"
                zeilen.append(f"      Wrapper {kurz}: {quelle['config']}")
            if dienst["query"]:
                zeilen.append(f"      Query: {_gekuerzt(dienst['query'], 200)}")
        zeilen.append(
            "\nDie Zahl hinter 'Konfiguration' ist die configurationID – sie wird für Export, "
            "Ontologie-Ansicht und Reload gebraucht."
        )
        return "\n".join(zeilen)

    async def _sparql(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        if auftrag["als_csv"]:
            csv = await self.client.sparql_als_csv(http, auftrag["query"], auftrag["namespace"])
            return f"SPARQL-Ergebnis als CSV (Namensraum {auftrag['namespace']}):\n{_gekuerzt(csv)}"

        ergebnis = await self.client.sparql(http, auftrag["query"], auftrag["namespace"])
        spalten = ergebnis.get("columnNames") or []
        zeilen = ergebnis.get("data") or []
        if not zeilen:
            return (
                f"Die Anfrage im Namensraum {auftrag['namespace']} lief durch, lieferte aber keine "
                "Zeilen. Zu prüfen: Passt der Namensraum zur Ontologie der Datenquelle?"
            )
        return (
            f"SPARQL-Ergebnis ({len(zeilen)} Zeilen, Namensraum {auftrag['namespace']}):\n"
            + _gekuerzt(self._tabelle(spalten, zeilen))
        )

    def _tabelle(self, spalten: list[str], zeilen: list[list[str]]) -> str:
        """Ergebnistabelle als einfacher Text – gut lesbar für Mensch und Modell."""
        kopf = " | ".join(spalten)
        koerper = "\n".join(" | ".join("" if z is None else str(z) for z in zeile) for zeile in zeilen)
        return f"{kopf}\n{'-' * len(kopf)}\n{koerper}"

    async def _graphql(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        ergebnis = await self.client.graphql(http, auftrag["dokument"])
        return "GraphQL-Ergebnis:\n" + _gekuerzt(json.dumps(ergebnis, ensure_ascii=False, indent=2))

    async def _export(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        ergebnis = await self.client.exportiere(http, auftrag["konfiguration"], auftrag["format"])
        if not ergebnis.strip():
            return (
                f"Konfiguration {auftrag['konfiguration']} lieferte keine Daten. Ist der Wrapper "
                f"geladen? Nach einem Import hilft 'Lade Konfiguration "
                f"{auftrag['konfiguration']} im Mediator neu'."
            )
        return (
            f"Konfiguration {auftrag['konfiguration']}, transformiert nach {auftrag['format']}:\n"
            + _gekuerzt(ergebnis)
        )

    async def _virtuell(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        ergebnis = await self.client.virtuelle_quelle(http, auftrag["konfiguration"], auftrag["inhalt"])
        return (
            f"Mitgelieferte Daten durch den Wrapper der Konfiguration {auftrag['konfiguration']} "
            f"transformiert:\n{_gekuerzt(ergebnis)}"
        )

    async def _virtuell_pfad(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        ergebnis = await self.client.virtuelle_quelle_aus_pfad(http, auftrag["konfiguration"], auftrag["pfad"])
        return (
            f"Datei {auftrag['pfad']} durch den Wrapper der Konfiguration "
            f"{auftrag['konfiguration']} transformiert:\n{_gekuerzt(ergebnis)}"
        )

    async def _ontologie(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        owl = await self.client.ontologie(http, auftrag["konfiguration"])
        return f"Ontologie der Konfiguration {auftrag['konfiguration']}:\n{_gekuerzt(owl)}"

    async def _mapping(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        xml = await self.client.mapping(http, auftrag["konfiguration"])
        return f"Mapping der Konfiguration {auftrag['konfiguration']}:\n{_gekuerzt(xml)}"

    async def _datenpfade(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        pfade = await self.client.datenpfade(http, auftrag["pfad"], auftrag["wrapper"])
        if not pfade:
            return f"Der Wrapper hat in {auftrag['pfad']} keine Datenpfade gefunden."
        return (
            f"{len(pfade)} Datenpfad(e) in {auftrag['pfad']}:\n"
            + "\n".join(f"  • {p}" for p in pfade[:100])
        )

    async def _import(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        ergebnis = await self.client.importiere_wrapper(http, auftrag["beschreibung"])
        if ergebnis.get("status") != "success":
            raise ValueError(f"Der Import ist fehlgeschlagen: {ergebnis.get('message', ergebnis)}")
        return (
            "Wrapper-Konfiguration importiert.\n"
            f"  configurationID: {ergebnis.get('configurationID')}\n"
            f"  Ordner:          {ergebnis.get('path')}\n"
            f"  UUID:            {ergebnis.get('uuid')}\n"
            "Damit der Mediator sie benutzt, noch 'Lade Konfiguration "
            f"{ergebnis.get('configurationID')} im Mediator neu' schicken."
        )

    async def _reload(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        antwort = await self.client.neu_laden(http, auftrag["konfiguration"])
        if auftrag["konfiguration"] is None:
            return f"Alle Dienste des Mediators neu geladen.\n{_gekuerzt(antwort, 1000)}"
        return f"Konfiguration {auftrag['konfiguration']} neu geladen: {_gekuerzt(antwort, 1000)}"

    async def _triplestore_lesen(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        return "Eingestellter Triple Store: " + (await self.client.triplestore(http)).strip()

    async def _triplestore_setzen(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        await self.client.triplestore_setzen(http, auftrag["url"])
        return f"Triple Store des Mediators auf {auftrag['url']} gesetzt."

    async def _upload(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        await self.client.upload_schalten(http, auftrag["ziel"], auftrag["an"])
        zustand = "eingeschaltet" if auftrag["an"] else "ausgeschaltet"
        return f"Upload nach {auftrag['ziel'].capitalize()} ist jetzt {zustand}."

    async def _kafka(self, http: httpx.AsyncClient, auftrag: dict) -> str:
        antwort = await self.client.auf_kafka(http, auftrag["nutzlast"])
        return f"Auf Kafka veröffentlicht. Antwort des Mediators:\n{_gekuerzt(antwort, 1000)}"
