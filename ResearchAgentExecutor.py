from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.helpers import (
    new_task_from_user_message,
    new_text_message,
    new_text_part,
    get_message_text,
)

from ResearchAgent import ResearchAgent


class ResearchAgentExecutor(AgentExecutor):
    """Baugleich mit den anderen Executoren. Der Agent selbst hat zusätzlich
    `beim_start()`/`beim_stopp()`, die agents_server.py beim Hochfahren aufruft."""

    def __init__(self) -> None:
        self.agent = ResearchAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 1. Task holen oder neu anlegen
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )

        # 2. Status: "wird bearbeitet"
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Durchsuche Forschungsgedächtnis..."),
        )

        # 3. Eigentliche Arbeit erledigen
        query = get_message_text(context.message) if context.message else ""
        try:
            result = await self.agent.invoke(user_request=query)
        except ValueError as err:
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(str(err)),
            )
            return
        except RuntimeError as err:  # MCP-Server nicht erreichbar oder Tool-Fehler
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message(f"MCP-Fehler: {err}"),
            )
            return

        # 4. Ergebnis als Artefakt zurückgeben
        await updater.add_artifact(
            parts=[new_text_part(text=result, media_type="text/plain")],
            name="research",
        )

        # 5. Status: "fertig"
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Fertig!"),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Abbrechen wird hier nicht unterstützt")
