"""MCP-Server mit Werkzeugen rund um BIBA-Mitarbeitende und ihre Veröffentlichungen.

MCP (Model Context Protocol) ist ein Standard, über den ein Agent Werkzeuge
("Tools") eines separaten Servers aufrufen kann. Dieser Server läuft als
Kindprozess des Agenten und spricht über stdin/stdout (Transport "stdio").

Werkzeuge:
    list_biba_staff        Mitarbeitende von der BIBA-Webseite holen
    search_publications    Veröffentlichungen einer Person suchen und PDFs laden
    summarize_pdf          Zusammenfassung + Keywords zu einer PDF-Datei
    summarize_text         dasselbe für einen Text (z. B. ein Abstract)

Direkt testen (ohne Agent):
    python biba_mcp_server.py            # wartet auf MCP-Nachrichten über stdio
    mcp dev biba_mcp_server.py           # MCP-Inspector im Browser

Publikationsquellen:
    1. Google Scholar über das Paket `scholarly`. Scholar hat keine offizielle
       API und blockt Skripte häufig. Deshalb mit Zeitlimit und ...
    2. ... OpenAlex (https://openalex.org) als zuverlässige, freie Ausweichquelle
       mit Filter auf die BIBA-Institution, Abstracts, Keywords und Open-Access-PDFs.
"""

import asyncio
import logging
import os
import re
from collections import Counter
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from mcp.server.mcpserver import MCPServer
from pypdf import PdfReader

STAFF_URL = "https://www.biba.uni-bremen.de/institut/mitarbeiterinnen.html"
BIBA_BASE = "https://www.biba.uni-bremen.de/"
MAIL_DOMAIN = "biba.uni-bremen.de"

OPENALEX_WORKS = "https://api.openalex.org/works"
# OpenAlex-ID von "Bremer Institut für Produktion und Logistik GmbH"
OPENALEX_BIBA_ID = os.environ.get("OPENALEX_INSTITUTION_ID", "I4387156409")
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "")

SCHOLAR_TIMEOUT = float(os.environ.get("SCHOLAR_TIMEOUT", "40"))
DOWNLOAD_DIR = Path(os.environ.get("RESEARCH_DOWNLOAD_DIR", "downloads"))
USER_AGENT = "Mozilla/5.0 (compatible; AgentExample-Tutorial/0.1)"

mcp = MCPServer("biba-research", version="0.1.0")
logging.getLogger("httpx").setLevel(logging.WARNING)  # HTTP-Zeilen nicht ins Log
logging.getLogger("pypdf").setLevel(logging.ERROR)    # Font-Warnungen beim PDF-Lesen unterdrücken


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def slug(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return text[:max_len] or "unbenannt"


def name_teile(anzeige: str) -> dict:
    """'Franke, Dr.-Ing. Marco (Geschäftsführung)' -> nachname, vorname, titel, rolle, name."""
    nachname, _, rest = anzeige.partition(",")
    rolle = " ".join(re.findall(r"\(([^)]*)\)", rest))
    rest = re.sub(r"\([^)]*\)", " ", rest).replace(",", " ").strip()  # "Dr., Michael" -> "Dr. Michael"
    # Akademische Titel erkennen; "Karl A." ist ein Namenskürzel, kein Titel.
    ist_titel = re.compile(r"(Prof|Dr|Dipl|Ing|habil|rer|nat|pol)\.?(-\w+\.?)*|[MB]\.?(Sc|A|Eng)\.?|MBA|PhD")
    titel = " ".join(t for t in rest.split() if ist_titel.fullmatch(t))
    vorname = " ".join(t for t in rest.split() if not ist_titel.fullmatch(t))
    # Für Suche und Anzeige: Vorname ohne Kürzel ("Karl A." -> "Karl")
    rufname = " ".join(t for t in vorname.split() if not re.fullmatch(r"[A-ZÄÖÜ]\.?", t))
    return {
        "nachname": nachname.strip(),
        "vorname": vorname.strip(),
        "titel": titel.strip(),
        "rolle": rolle.strip(),
        "name": f"{rufname.strip() or vorname.strip()} {nachname.strip()}".strip(),
    }


def abstract_aus_index(inverted: dict | None) -> str:
    """OpenAlex liefert Abstracts als invertierten Index {wort: [positionen]}."""
    if not inverted:
        return ""
    positionen = sorted((pos, wort) for wort, plist in inverted.items() for pos in plist)
    text = " ".join(wort for _, wort in positionen)
    return re.sub(r"^\s*(abstract|zusammenfassung)\s*[:.]?\s*", "", text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Tool 1: Mitarbeitende
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_biba_staff(abteilung: str = "") -> list[dict]:
    """Holt die Liste der Mitarbeitenden von der BIBA-Webseite.

    Liefert pro Person: name, vorname, nachname, titel, nick, email, telefon,
    raum, abteilung, homepage. Mit `abteilung` (z. B. "2.2") wird gefiltert.
    """
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        antwort = await http.get(STAFF_URL)
        antwort.raise_for_status()

    soup = BeautifulSoup(antwort.text, "html.parser")
    personen: list[dict] = []
    for tabelle in soup.select("table.employee"):
        for zeile in tabelle.select("tr"):
            zellen = zeile.find_all("td")
            if len(zellen) < 5:
                continue
            link = zellen[0].find("a")
            if not link:
                continue
            anzeige = link.get_text(" ", strip=True)
            if "," not in anzeige:
                continue  # z. B. "Zentrale": ein Sammelanschluss, keine Person
            mail = zellen[2].find("a", class_="cryptedmail")
            nick = (mail.get("data-name") if mail else "") or ""
            href = link.get("href", "")
            person = {
                **name_teile(anzeige),
                "anzeige": anzeige,
                "nick": nick,
                "email": f"{nick}@{MAIL_DOMAIN}" if nick else "",
                "telefon": zellen[1].get_text(" ", strip=True).replace(" ", " "),
                "raum": zellen[3].get_text(" ", strip=True),
                "abteilung": zellen[4].get_text(" ", strip=True),
                "homepage": href if href.startswith("http") else BIBA_BASE + href.lstrip("/"),
            }
            if not abteilung or person["abteilung"] == abteilung:
                personen.append(person)

    # Duplikate (dieselbe Person in mehreren Tabellen) entfernen
    gesehen: set[str] = set()
    eindeutig = []
    for p in personen:
        key = p["nick"] or p["anzeige"]
        if key not in gesehen:
            gesehen.add(key)
            eindeutig.append(p)
    return eindeutig


# ---------------------------------------------------------------------------
# Tool 2: Veröffentlichungen suchen und herunterladen
# ---------------------------------------------------------------------------
def _scholar_suche(autor: str, max_results: int) -> list[dict]:
    """Blockierender Google-Scholar-Aufruf (läuft in einem Thread)."""
    from scholarly import scholarly  # Import hier, damit der Server auch ohne scholarly startet

    ergebnisse = []
    suche = scholarly.search_pubs(f'author:"{autor}"')
    for pub in suche:
        bib = pub.get("bib", {})
        ergebnisse.append(
            {
                "titel": bib.get("title", ""),
                "jahr": bib.get("pub_year"),
                "autoren": bib.get("author", []) if isinstance(bib.get("author"), list) else [bib.get("author", "")],
                "venue": bib.get("venue", ""),
                "abstract": bib.get("abstract", ""),
                "keywords": [],
                "url": pub.get("pub_url", ""),
                "pdf_url": pub.get("eprint_url", ""),
                "quelle": "google_scholar",
            }
        )
        if len(ergebnisse) >= max_results:
            break
    return ergebnisse


async def _openalex_suche(http: httpx.AsyncClient, autor: str, max_results: int, nur_biba: bool) -> list[dict]:
    # Kommas trennen bei OpenAlex die Filter, Doppelpunkte Feld und Wert: aus dem Namen entfernen
    autor = re.sub(r"[,:|]", " ", autor).strip()
    filter_teile = [f"raw_author_name.search:{autor}"]
    if nur_biba and OPENALEX_BIBA_ID:
        filter_teile.append(f"authorships.institutions.lineage:{OPENALEX_BIBA_ID}")
    params = {
        "filter": ",".join(filter_teile),
        "per-page": max_results,
        "sort": "publication_year:desc",
        "select": "id,title,publication_year,open_access,primary_location,best_oa_location,locations,authorships,keywords,abstract_inverted_index,doi",
    }
    if OPENALEX_MAILTO:
        params["mailto"] = OPENALEX_MAILTO
    antwort = await http.get(OPENALEX_WORKS, params=params)
    antwort.raise_for_status()
    ergebnisse = []
    for w in antwort.json().get("results", []):
        ort = w.get("primary_location") or {}
        quelle = ort.get("source") or {}
        oa = w.get("open_access") or {}
        # Alle bekannten PDF-Fundorte sammeln: bestes OA zuerst, dann Verlag, dann Repositorien
        pdf_urls: list[str] = []
        for kandidat in [(w.get("best_oa_location") or {}).get("pdf_url"), ort.get("pdf_url"), oa.get("oa_url")] + [
            loc.get("pdf_url") for loc in w.get("locations") or []
        ]:
            if kandidat and kandidat not in pdf_urls:
                pdf_urls.append(kandidat)
        ergebnisse.append(
            {
                "titel": w.get("title") or "",
                "jahr": w.get("publication_year"),
                "autoren": [a.get("raw_author_name") or a.get("author", {}).get("display_name", "") for a in w.get("authorships", [])],
                "venue": quelle.get("display_name", ""),
                "abstract": abstract_aus_index(w.get("abstract_inverted_index")),
                "keywords": [k.get("display_name", "") for k in w.get("keywords", [])],
                "url": w.get("doi") or ort.get("landing_page_url") or w.get("id", ""),
                "pdf_url": pdf_urls[0] if pdf_urls else "",
                "pdf_urls": pdf_urls,
                "quelle": "openalex",
            }
        )
    return ergebnisse


_META_REFRESH = re.compile(r"http-equiv=[\"']refresh[\"'][^>]*url=['\"]?([^'\">]+)", re.IGNORECASE)


async def _pdf_laden(http: httpx.AsyncClient, urls: list[str], ziel: Path) -> str:
    """Probiert alle Fundorte durch und speichert die erste echte PDF. Sonst ""."""
    if ziel.exists():
        return str(ziel)
    for url in urls:
        try:
            antwort = await http.get(url)
            # Manche Verlage schicken erst eine HTML-Seite mit Weiterleitung
            if not antwort.content.startswith(b"%PDF") and b"refresh" in antwort.content[:2000].lower():
                match = _META_REFRESH.search(antwort.text[:2000])
                if match:
                    antwort = await http.get(httpx.URL(url).join(match.group(1)))
        except httpx.HTTPError:
            continue
        if antwort.status_code == 200 and antwort.content.startswith(b"%PDF"):
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(antwort.content)
            return str(ziel)
    return ""


@mcp.tool()
async def search_publications(
    autor: str, max_results: int = 5, download: bool = True, quelle: str = "auto", nur_biba: bool = True
) -> dict:
    """Sucht Veröffentlichungen einer Person und lädt verfügbare PDFs herunter.

    quelle: "auto" (erst Google Scholar, bei Fehler OpenAlex), "scholar" oder "openalex".
    nur_biba: bei OpenAlex nur Arbeiten mit BIBA-Zugehörigkeit (vermeidet Namensvettern).
    Liefert {"quelle", "hinweis", "publikationen": [{titel, jahr, autoren, venue,
    abstract, keywords, url, pdf_url, pdf_pfad, quelle}]}.
    """
    publikationen: list[dict] = []
    hinweis = ""

    if quelle in ("auto", "scholar"):
        try:
            publikationen = await asyncio.wait_for(
                asyncio.to_thread(_scholar_suche, autor, max_results), timeout=SCHOLAR_TIMEOUT
            )
            if not publikationen:
                hinweis = "Google Scholar: keine Treffer"
        except (asyncio.TimeoutError, Exception) as err:  # scholarly wirft diverse eigene Fehler
            hinweis = f"Google Scholar nicht nutzbar ({type(err).__name__})"
            publikationen = []

    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as http:
        if not publikationen and quelle in ("auto", "openalex"):
            publikationen = await _openalex_suche(http, autor, max_results, nur_biba)
            if hinweis:
                hinweis += "; Ausweichquelle OpenAlex verwendet"

        if download:
            ordner = DOWNLOAD_DIR / slug(autor)
            for pub in publikationen:
                pub["pdf_pfad"] = ""
                kandidaten = pub.get("pdf_urls") or ([pub["pdf_url"]] if pub.get("pdf_url") else [])
                if kandidaten:
                    pub["pdf_pfad"] = await _pdf_laden(http, kandidaten, ordner / f"{slug(pub['titel'])}.pdf")
        else:
            for pub in publikationen:
                pub["pdf_pfad"] = ""

    verwendete_quelle = publikationen[0]["quelle"] if publikationen else "keine"
    return {"quelle": verwendete_quelle, "hinweis": hinweis, "publikationen": publikationen}


# ---------------------------------------------------------------------------
# Tool 3: Zusammenfassung und Keywords
# ---------------------------------------------------------------------------
STOPWOERTER = set(
    """
    the a an and or of to in on for with by from as at is are was were be been being this that these those it its
    we our you your they their he she his her which who whom whose what when where why how not no nor but if then
    than so such into onto over under between among within without through during before after above below up down
    out off again further once here there all any both each few more most other some own same very can will just
    also may might must shall should would could using use used based via et al fig figure table section paper
    approach results result method methods data model models system systems proposed presented
    der die das den dem des ein eine einer eines einem einen und oder von zu zur zum im in auf für mit bei aus nach
    über unter durch gegen ohne um an als auch noch nur so wie ist sind war waren wird werden wurde wurden kann
    können soll sollen muss müssen hat haben hatte hatten nicht kein keine dies diese dieser dieses jene es sie er
    wir ihr uns euch sich ich du man mehr sehr sowie bzw z b etc ca dabei dazu daher deshalb somit
    erste ersten erster zweite zweiten neue neuen neuer neues große großen großer hohe hohen hoher weitere weiteren
    zeigt zeigen wurde wurden dabei jedoch bereits sowohl insbesondere hierbei anhand mittels zudem zwischen
    abstract zusammenfassung keywords introduction conclusion conclusions however therefore thus moreover
    furthermore within respectively significant different various several
    """.split()
)
_WORT = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\-]{2,}")
_SATZ_ENDE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ\d])")


def _pdf_text(pfad: str, max_seiten: int = 15, max_zeichen: int = 40_000) -> tuple[str, int]:
    reader = PdfReader(pfad)
    teile = []
    for seite in reader.pages[:max_seiten]:
        teile.append(seite.extract_text() or "")
        if sum(len(t) for t in teile) > max_zeichen:
            break
    text = "\n".join(teile)
    text = re.sub(r"-\n(?=[a-zäöü])", "", text)  # Silbentrennung am Zeilenende aufheben
    text = re.sub(r"\s+", " ", text)
    return text[:max_zeichen], len(reader.pages)


def _zusammenfassen(text: str, max_saetze: int, max_keywords: int) -> dict:
    """Extraktive Zusammenfassung: Sätze mit den häufigsten Fachwörtern, in Originalreihenfolge.

    Bewusst ohne Sprachmodell: schnell, deterministisch und beim Indexieren
    vieler PDFs praktikabel. Keywords sind die häufigsten Einzelwörter und
    Wortpaare ohne Stoppwörter.
    """
    woerter = [w.lower() for w in _WORT.findall(text)]
    inhalt = [w for w in woerter if w not in STOPWOERTER]
    haeufigkeit = Counter(inhalt)
    if not haeufigkeit:
        return {"zusammenfassung": "", "keywords": []}

    # Keywords: Einzelwörter + Bigramme, Bigramme leicht bevorzugt
    bigramme = Counter(
        f"{a} {b}" for a, b in zip(woerter, woerter[1:]) if a not in STOPWOERTER and b not in STOPWOERTER
    )
    kandidaten = Counter()
    for w, n in haeufigkeit.items():
        kandidaten[w] = n
    for bg, n in bigramme.items():
        if n >= 2:
            kandidaten[bg] = n * 1.5
    keywords = [k for k, _ in kandidaten.most_common(max_keywords * 2)]
    # Einzelwörter entfernen, die schon Teil eines gewählten Bigramms sind
    bigramm_woerter = {w for k in keywords if " " in k for w in k.split()}
    keywords = [k for k in keywords if " " in k or k not in bigramm_woerter][:max_keywords]

    saetze = [s.strip() for s in _SATZ_ENDE.split(text) if 40 <= len(s.strip()) <= 400]
    if not saetze:
        return {"zusammenfassung": text[:500], "keywords": keywords}
    max_n = haeufigkeit.most_common(1)[0][1]
    bewertung = []
    for i, satz in enumerate(saetze):
        sw = [w.lower() for w in _WORT.findall(satz)]
        score = sum(haeufigkeit.get(w, 0) / max_n for w in sw if w not in STOPWOERTER) / (len(sw) + 5)
        bewertung.append((score, i))
    beste = sorted(sorted(bewertung, reverse=True)[:max_saetze], key=lambda x: x[1])
    return {"zusammenfassung": " ".join(saetze[i] for _, i in beste), "keywords": keywords}


@mcp.tool()
def summarize_pdf(pfad: str, max_saetze: int = 5, max_keywords: int = 10) -> dict:
    """Erstellt zu einer PDF-Datei eine kurze Zusammenfassung und wichtige Keywords.

    Liefert {"pfad", "seiten", "zeichen", "zusammenfassung", "keywords"}.
    """
    if not os.path.exists(pfad):
        raise FileNotFoundError(f"PDF nicht gefunden: {pfad}")
    text, seiten = _pdf_text(pfad)
    ergebnis = _zusammenfassen(text, max_saetze, max_keywords)
    # "anfang": Titelseite mit Autor:innen, damit ein Aufrufer die Verfasser erkennen kann
    return {"pfad": pfad, "seiten": seiten, "zeichen": len(text), "anfang": text[:1500], **ergebnis}


@mcp.tool()
def summarize_text(text: str, max_saetze: int = 3, max_keywords: int = 10) -> dict:
    """Wie summarize_pdf, aber für einen übergebenen Text (z. B. ein Abstract)."""
    return {"zeichen": len(text), **_zusammenfassen(text, max_saetze, max_keywords)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
