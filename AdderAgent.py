import re

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


class AdderAgent:
    """Die 'Intelligenz' von Agent C: addiert zwei Zahlen aus einem Text – und seine Visitenkarte."""

    PATH = "/adder"

    SKILL = AgentSkill(
        id="add",
        name="Adder",
        description="Addiert zwei Zahlen, die im Text der Nachricht stehen.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "math", "addition"],
        examples=["Addiere 17 und 25", "Was ist 3,5 + 4?"],
    )

    _NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent C – Adder",
            description="Ein einfacher Rechen-Agent, der zwei Zahlen addiert.",
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
        numbers = [float(n.replace(",", ".")) for n in self._NUMBER.findall(user_request)]
        if len(numbers) != 2:
            raise ValueError(
                f"Ich brauche genau zwei Zahlen, habe aber {len(numbers)} gefunden: {numbers}"
            )
        a, b = numbers
        return f"{self._fmt(a)} + {self._fmt(b)} = {self._fmt(a + b)}"

    @staticmethod
    def _fmt(x: float) -> str:
        return str(int(x)) if x.is_integer() else str(x)
