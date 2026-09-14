"""Agent F – Wetter: liefert das aktuelle Wetter für einen Ort.

Datenquelle ist Open-Meteo (https://open-meteo.com). Die API ist kostenlos und
braucht keinen API-Key. Zwei Aufrufe pro Anfrage:
  1. Geocoding:  Ortsname -> Koordinaten
  2. Forecast:   Koordinaten -> aktuelle Messwerte
"""

import re

import httpx

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO-Wettercodes (Auszug), wie Open-Meteo sie liefert
WMO_CODES = {
    0: "klar",
    1: "überwiegend klar",
    2: "teilweise bewölkt",
    3: "bedeckt",
    45: "Nebel",
    48: "Reifnebel",
    51: "leichter Nieselregen",
    53: "Nieselregen",
    55: "starker Nieselregen",
    56: "gefrierender Nieselregen",
    57: "starker gefrierender Nieselregen",
    61: "leichter Regen",
    63: "Regen",
    65: "starker Regen",
    66: "gefrierender Regen",
    67: "starker gefrierender Regen",
    71: "leichter Schneefall",
    73: "Schneefall",
    75: "starker Schneefall",
    77: "Schneegriesel",
    80: "leichte Regenschauer",
    81: "Regenschauer",
    82: "heftige Regenschauer",
    85: "leichte Schneeschauer",
    86: "starke Schneeschauer",
    95: "Gewitter",
    96: "Gewitter mit leichtem Hagel",
    99: "Gewitter mit starkem Hagel",
}


class WeatherAgent:
    """Die 'Intelligenz' des Wetter-Agenten: Ort aus dem Text lesen, Wetter abfragen."""

    PATH = "/weather"

    SKILL = AgentSkill(
        id="weather",
        name="Weather",
        description=(
            "Gibt das aktuelle Wetter für einen Ort zurück: Temperatur, gefühlte "
            "Temperatur, Luftfeuchte, Wind und Wetterlage (Wetter, Temperatur, regnet es)."
        ),
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "weather", "wetter"],
        examples=["Wie ist das Wetter in Bremen?", "Wetter für Hamburg", "Regnet es gerade in Berlin?"],
    )

    # Ort steht meist nach "in", "für", "von", "bei" ...
    _ORT_NACH_PRAEPOSITION = re.compile(
        r"\b(?:in|für|fuer|von|bei|aus)\s+([A-ZÄÖÜ][\wäöüß\-]*(?:\s+[A-ZÄÖÜ][\wäöüß\-]*)*)",
    )
    # ... sonst nehmen wir die letzte Folge großgeschriebener Wörter ("New York", "Bad Homburg").
    _GROSSGESCHRIEBEN = re.compile(r"\b[A-ZÄÖÜ][\wäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][\wäöüß\-]{2,})*\b")
    _STOPWOERTER = {"Wie", "Was", "Wetter", "Ist", "Das", "Die", "Der", "Regnet", "Bitte", "Sag", "Gib", "Zeig"}

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent F – Weather",
            description="Ein Wetter-Agent, der das aktuelle Wetter für einen Ort über Open-Meteo abfragt.",
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

    def ort_aus_text(self, text: str) -> str:
        """Liest den Ortsnamen aus einer Frage wie 'Wie ist das Wetter in Bremen?'."""
        text = text.strip().rstrip("?.!")
        match = self._ORT_NACH_PRAEPOSITION.search(text)
        if match:
            return match.group(1)
        kandidaten = []
        for folge in self._GROSSGESCHRIEBEN.findall(text):
            woerter = [w for w in folge.split() if w not in self._STOPWOERTER]
            if woerter:
                kandidaten.append(" ".join(woerter))
        if kandidaten:
            return kandidaten[-1]
        if text:
            return text  # vielleicht steht nur der Ortsname in der Nachricht
        raise ValueError("Ich habe keinen Ortsnamen in der Nachricht gefunden.")

    async def invoke(self, user_request: str) -> str:
        ort = self.ort_aus_text(user_request)

        async with httpx.AsyncClient(timeout=15) as http:
            # 1. Geocoding
            geo = await http.get(GEOCODING_URL, params={"name": ort, "count": 1, "language": "de"})
            geo.raise_for_status()
            treffer = geo.json().get("results") or []
            if not treffer:
                raise ValueError(f"Ich kenne keinen Ort namens '{ort}'.")
            platz = treffer[0]

            # 2. Aktuelles Wetter
            wetter = await http.get(
                FORECAST_URL,
                params={
                    "latitude": platz["latitude"],
                    "longitude": platz["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
            )
            wetter.raise_for_status()
            daten = wetter.json()

        jetzt = daten["current"]
        einheiten = daten["current_units"]
        lage = WMO_CODES.get(jetzt["weather_code"], f"Wettercode {jetzt['weather_code']}")
        name = platz["name"]
        land = platz.get("country")
        region = platz.get("admin1")
        wo = ", ".join(p for p in (name, region, land) if p and p != name)
        return (
            f"Wetter in {name}" + (f" ({wo})" if wo else "") + f": {lage}, "
            f"{jetzt['temperature_2m']} {einheiten['temperature_2m']} "
            f"(gefühlt {jetzt['apparent_temperature']} {einheiten['apparent_temperature']}), "
            f"Luftfeuchte {jetzt['relative_humidity_2m']} {einheiten['relative_humidity_2m']}, "
            f"Wind {jetzt['wind_speed_10m']} {einheiten['wind_speed_10m']}. "
            f"Stand: {jetzt['time']} ({daten['timezone']})"
        )
