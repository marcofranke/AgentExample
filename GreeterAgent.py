from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


class GreeterAgent:
    """Die 'Intelligenz' von Agent B – und seine eigene Visitenkarte."""

    # Unter diesem Pfad ist der Agent auf dem Server erreichbar.
    PATH = "/greeter"

    # Der Skill ist die "Werbung" dieses Agenten: Das liest später das LLM im Orchestrator.
    SKILL = AgentSkill(
        id="greet",
        name="Greeter",
        description="Begrüßt den Absender und wiederholt seine Nachricht.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "greeting"],
        examples=["Hallo!", "Wie geht's?"],
    )

    def agent_card(self, base_url: str) -> AgentCard:
        """Baut die Visitenkarte. Nur die Basis-URL kommt von außen, alles andere weiß der Agent selbst."""
        return AgentCard(
            name="Agent B – Greeter",
            description="Ein einfacher Begrüßungs-Agent zum Üben von A2A.",
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

    async def invoke(self, user_request: str) -> str:
        return f"Hallo! Agent B hat deine Nachricht erhalten:'{user_request}'"
