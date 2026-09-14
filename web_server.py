"""Startet Agenten, Orchestrator und Weboberfläche als eine Webanwendung.

Das ist der Einstiegspunkt für den Betrieb (auch im Docker-Container):

    python web_server.py        ->  http://127.0.0.1:9999/

Unterschied zu den anderen Startern:

    agents_server.py   nur die Agenten, kein LLM        (Teil 1 des Tutorials)
    main.py            Agenten + Orchestrator im Terminal
    web_server.py      Agenten + Orchestrator + Weboberfläche im Browser

Konfiguration über Umgebungsvariablen (siehe README):
    A2A_HOST       Adresse, auf der gelauscht wird (im Container 0.0.0.0)
    A2A_PORT       Port (Standard 9999)
    A2A_BASE_URL   Adresse in den Visitenkarten (Standard http://127.0.0.1:<PORT>)
"""

import uvicorn

from agents_server import BASE_URL, HOST, PORT, build_app
from web_ui import WebUI


def build_web_app():
    """Die komplette Anwendung: Agenten-Routen plus die Routen der Oberfläche."""
    return build_app(extras=[WebUI()])


if __name__ == "__main__":
    print(f"[Web] Oberfläche: {BASE_URL}/   (lauscht auf {HOST}:{PORT})")
    uvicorn.run(build_web_app(), host=HOST, port=PORT)
