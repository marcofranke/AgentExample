"""Startet Agenten-Server UND LLM-Orchestrator in EINEM Prozess.

Bisher brauchte das Tutorial zwei Terminals: `python agents_server.py` und
daneben `python agent_c_orchestrator.py`. Diese Datei nimmt beides ab:

    python main.py                                  # interaktiv
    python main.py "Wie ist das Wetter in Bremen?"  # Anfragen direkt übergeben

Wie das geht: Der Server ist eine asyncio-Anwendung (`uvicorn.Server.serve()`
ist eine Koroutine), der Orchestrator ebenfalls. Beide laufen deshalb in
derselben Event-Loop – der Server als Hintergrund-Task, der Orchestrator im
Vordergrund. Alles Blockierende (Modell laden, `input()`) liegt in Threads,
sonst stünde der Server still, solange auf eine Eingabe gewartet wird.

Die Einzelstarts bleiben unverändert möglich und sind zum Verstehen der
Bausteine weiterhin der bessere Weg (siehe README, Teil 1 und 2).
"""

import asyncio
import sys

import uvicorn

from agents_server import BASE_URL, HOST, PORT, build_app
from agent_c_orchestrator import LokalesLLM, orchestriere


async def server_starten() -> tuple[uvicorn.Server, asyncio.Task]:
    """Startet uvicorn als Hintergrund-Task und wartet, bis er Anfragen annimmt.

    Das Warten ist wichtig: Der Orchestrator holt als Erstes die AgentCards.
    Fragt er zu früh, bekommt er einen ConnectError.
    """
    config = uvicorn.Config(build_app(), host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="agents-server")

    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    if task.done():
        try:
            await task  # Startfehler hier sichtbar machen
        except SystemExit:  # uvicorn beendet sich bei Bind-Fehlern per sys.exit()
            raise RuntimeError(
                f"Der Server konnte Port {PORT} nicht belegen. Meist läuft noch ein "
                f"altes `python agents_server.py` oder `python main.py` in einem anderen Terminal."
            ) from None
        raise RuntimeError("Der Server hat sich sofort wieder beendet.")

    print(f"\n[main] Server bereit unter {BASE_URL}\n")
    return server, task


async def server_stoppen(server: uvicorn.Server, task: asyncio.Task) -> None:
    """Sauber herunterfahren, damit der Lifespan noch `beim_stopp()` aufruft.

    Der Research-Agent beendet dort seinen MCP-Kindprozess – ohne das bliebe
    ein Python-Prozess zurück.
    """
    print("\n[main] Fahre Server herunter...")
    server.should_exit = True
    await task


async def main() -> None:
    # Modell und Server gleichzeitig hochfahren. Das Laden des LLM dauert lange
    # (beim allerersten Start inklusive Download), und der Research-Agent baut
    # in dieser Zeit ohnehin schon sein Gedächtnis auf.
    llm_task = asyncio.create_task(asyncio.to_thread(LokalesLLM), name="llm-laden")

    try:
        server, server_task = await server_starten()
    except BaseException:
        llm_task.cancel()
        raise

    try:
        llm = await llm_task
        await orchestriere(sys.argv[1:], llm=llm)
    except (KeyboardInterrupt, EOFError):
        print("\n[main] Abbruch durch Nutzer.")
    finally:
        await server_stoppen(server, server_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Strg+C beim Herunterfahren nicht als Fehler-Traceback zeigen
    except RuntimeError as err:
        print(f"\n[main] {err}")
        raise SystemExit(1) from None
