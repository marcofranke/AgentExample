import re

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils import TransportProtocol


class SubtractorAgent:
    """Die 'Intelligenz' des Subtrahier-Agenten: zieht die zweite Zahl von der ersten ab."""

    PATH = "/subtractor"

    # Bewusst ähnlich zum Adder beschrieben, damit das Routing wirklich entscheiden muss.
    SKILL = AgentSkill(
        id="subtract",
        name="Subtractor",
        description="Subtrahiert zwei Zahlen: zieht die zweite von der ersten ab (minus, abziehen, Differenz).",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["demo", "math", "subtraction"],
        examples=["Was ist 20 minus 8?", "Ziehe 5 von 12 ab", "10 - 3"],
    )

    _NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
    # "Ziehe 5 von 12 ab" oder "Nimm 4 weg von 9" -> zweite Zahl minus erste
    _VON_AB = re.compile(r"\d\s+(?:\w+\s+)?von\s+-?\d", re.IGNORECASE)

    def agent_card(self, base_url: str) -> AgentCard:
        return AgentCard(
            name="Agent E – Subtractor",
            description="Ein einfacher Rechen-Agent, der zwei Zahlen subtrahiert.",
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
        if self._VON_AB.search(user_request):
            a, b = b, a  # "x von y" bedeutet y - x
        return f"{self._fmt(a)} - {self._fmt(b)} = {self._fmt(a - b)}"

    @staticmethod
    def _fmt(x: float) -> str:
        return str(int(x)) if x.is_integer() else str(x)
