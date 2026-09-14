"""Agent G – Ontologie-Suche: findet bekannte Ontologien im Internet zu einem Begriff.

Quellen (beide ohne API-Key):
  1. OLS4 – Ontology Lookup Service des EMBL-EBI
     https://www.ebi.ac.uk/ols4  (rund 280 Ontologien, Schwerpunkt Life Sciences,
     aber auch SOSA/SSN, Schema.org, PROV, QUDT u. a.)
  2. LOV – Linked Open Vocabularies
     https://lov.linkeddata.es  (klassisches Register für Linked-Data-Vokabulare)

Strategie: Wir suchen Klassen, die zum Begriff passen, und gruppieren die Treffer
nach der Ontologie, aus der sie stammen. So bekommen wir "Ontologien, in denen
der Begriff modelliert ist". LOV wird zusätzlich befragt; antwortet die Seite
nicht mit JSON (kommt vor), wird sie übersprungen.
"""

import re

import httpx

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol

OLS_SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"
OLS_ONTOLOGY_URL = "https://www.ebi.ac.uk/ols4/api/ontologies/{id}"
LOV_SEARCH_URL = "https://lov.linkeddata.es/dataset/lov/api/v2/vocabulary/search"

MAX_ONTOLOGIEN = 8


class OntologySearchAgent:
    """Die 'Intelligenz' des Ontologie-Agenten: Begriff extrahieren, Register befragen."""

    PATH = "/ontology"

    SKILL = AgentSkill(
        id="ontology_search",
        name="Ontology Search",
        description=(
            "Sucht im Internet nach bekannten Ontologien und Vokabularen zu einem Begriff "
            "oder Fachgebiet (Ontologie, Vokabular, OWL, RDF, Linked Data, Semantic Web)."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "ontology", "semantic-web", "linked-data"],
        examples=[
            "Welche Ontologien gibt es für Sensoren?",
            "Suche eine Ontologie zu Logistik",
            "Finde Vokabulare für Produktdaten",
        ],
    )

    # Alles nach "für", "zu", "über", "zum Thema" ... ist der Suchbegriff
    _BEGRIFF = re.compile(
        r"\b(?:für|fuer|zu|zum thema|zum|über|ueber|nach|bezüglich|betreffend)\s+(.+)$",
        re.IGNORECASE,
    )
    _FUELLWOERTER = re.compile(
        r"\b(ontologie|ontologien|ontology|ontologies|vokabular|vokabulare|vocabulary|"
        r"vocabularies|suche|such|finde|find|welche|gibt|es|eine|einen|ein|die|der|das|"
        r"bitte|mir|im|internet|bekannte)\b",
        re.IGNORECASE,
    )

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent G – Ontology Search",
            description="Ein Agent, der bekannte Ontologien zu einem Begriff in OLS4 und LOV sucht.",
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

    def begriff_aus_text(self, text: str) -> str:
        """'Welche Ontologien gibt es für Sensoren?' -> 'Sensoren'."""
        text = text.strip().rstrip("?.!")
        match = self._BEGRIFF.search(text)
        kandidat = match.group(1) if match else text
        kandidat = self._FUELLWOERTER.sub(" ", kandidat)
        kandidat = re.sub(r"\s+", " ", kandidat).strip(" ,;:")
        if not kandidat:
            raise ValueError("Ich habe keinen Suchbegriff in der Nachricht gefunden.")
        return kandidat

    @staticmethod
    def suchvarianten(begriff: str) -> list[str]:
        """OLS ist englisch indexiert. Für deutsche Eingaben probieren wir der Reihe nach:
        den Begriff selbst, dann ohne typische Plural-/Flexionsendungen ("Sensoren" -> "Sensor"),
        zuletzt den Wortstamm mit Wildcard ("Logistik" -> "Logisti*")."""
        varianten = [begriff]
        wort = begriff.split()[-1] if " " in begriff else begriff
        for endung in ("en", "n", "e", "s", "er"):
            if wort.lower().endswith(endung) and len(wort) - len(endung) >= 4:
                varianten.append(wort[: -len(endung)])
        stamm = wort[:-1] if len(wort) > 5 else wort
        varianten.append(f"{stamm}*")
        # Duplikate entfernen, Reihenfolge behalten
        return list(dict.fromkeys(varianten))

    async def _suche_ols(self, http: httpx.AsyncClient, begriff: str) -> tuple[str, list[dict]]:
        """Klassen-Treffer in OLS4, gruppiert nach Ontologie, angereichert mit Titel.

        Liefert (verwendeter Suchbegriff, Ontologien)."""
        docs: list[dict] = []
        verwendet = begriff
        for variante in self.suchvarianten(begriff):
            antwort = await http.get(
                OLS_SEARCH_URL,
                params={
                    "q": variante,
                    "type": "class",
                    "rows": 50,
                    "fieldList": "iri,label,ontology_name,ontology_prefix",
                },
            )
            antwort.raise_for_status()
            docs = antwort.json()["response"]["docs"]
            if docs:
                verwendet = variante
                break

        # Reihenfolge der ersten Treffer beibehalten (OLS sortiert nach Relevanz)
        gruppen: dict[str, dict] = {}
        for doc in docs:
            oid = doc["ontology_name"]
            eintrag = gruppen.setdefault(
                oid, {"id": oid, "prefix": doc.get("ontology_prefix", oid.upper()), "beispiele": []}
            )
            if len(eintrag["beispiele"]) < 2:
                eintrag["beispiele"].append(f"{doc.get('label', '?')} <{doc['iri']}>")

        ergebnisse = []
        for eintrag in list(gruppen.values())[:MAX_ONTOLOGIEN]:
            detail = await http.get(OLS_ONTOLOGY_URL.format(id=eintrag["id"]))
            if detail.status_code == 200:
                config = detail.json().get("config", {})
                eintrag["titel"] = config.get("title") or eintrag["prefix"]
                eintrag["homepage"] = config.get("homepage") or config.get("fileLocation") or ""
            else:
                eintrag["titel"] = eintrag["prefix"]
                eintrag["homepage"] = ""
            eintrag["quelle"] = f"https://www.ebi.ac.uk/ols4/ontologies/{eintrag['id']}"
            ergebnisse.append(eintrag)
        return verwendet, ergebnisse

    async def _suche_lov(self, http: httpx.AsyncClient, begriff: str) -> list[dict] | None:
        """Vokabular-Treffer in LOV. None, wenn LOV nicht (mit JSON) antwortet."""
        try:
            antwort = await http.get(LOV_SEARCH_URL, params={"q": begriff})
            antwort.raise_for_status()
            if "json" not in antwort.headers.get("content-type", ""):
                return None
            daten = antwort.json()
        except (httpx.HTTPError, ValueError):
            return None

        ergebnisse = []
        for hit in daten.get("results", [])[:MAX_ONTOLOGIEN]:
            src = hit.get("_source", hit)
            prefix = (src.get("prefix") or [""])[0] if isinstance(src.get("prefix"), list) else src.get("prefix", "")
            titel = src.get("titles") or src.get("title") or [{"value": prefix}]
            if isinstance(titel, list) and titel and isinstance(titel[0], dict):
                titel = titel[0].get("value", prefix)
            uri = (src.get("uri") or [""])[0] if isinstance(src.get("uri"), list) else src.get("uri", "")
            ergebnisse.append({"prefix": prefix, "titel": titel, "homepage": uri,
                               "quelle": f"https://lov.linkeddata.es/dataset/lov/vocabs/{prefix}"})
        return ergebnisse

    async def invoke(self, user_request: str) -> str:
        begriff = self.begriff_aus_text(user_request)

        async with httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"}) as http:
            suchbegriff, ols = await self._suche_ols(http, begriff)
            lov = await self._suche_lov(http, begriff)

        if not ols and not lov:
            raise ValueError(f"Keine Ontologie zu '{begriff}' gefunden.")

        zeilen = [f"Ontologien zu '{begriff}':", ""]
        hinweis = f" (gesucht nach '{suchbegriff}')" if suchbegriff != begriff else ""
        zeilen.append(f"OLS4 (EMBL-EBI) – {len(ols)} Ontologien mit passenden Klassen{hinweis}:")
        for e in ols:
            zeilen.append(f"  • {e['titel']} [{e['prefix']}]  {e['quelle']}")
            for bsp in e["beispiele"]:
                zeilen.append(f"      z. B. {bsp}")

        zeilen.append("")
        if lov is None:
            zeilen.append("LOV (Linked Open Vocabularies): nicht erreichbar oder keine JSON-Antwort, übersprungen.")
        elif not lov:
            zeilen.append("LOV (Linked Open Vocabularies): keine Treffer.")
        else:
            zeilen.append(f"LOV (Linked Open Vocabularies) – {len(lov)} Vokabulare:")
            for e in lov:
                zeilen.append(f"  • {e['titel']} [{e['prefix']}]  {e['homepage'] or e['quelle']}")
        return "\n".join(zeilen)
