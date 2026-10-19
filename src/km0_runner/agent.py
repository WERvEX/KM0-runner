"""mini-swe-agent's DefaultAgent with a signal monitor watching every step."""

from __future__ import annotations

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import InterruptAgentFlow

from .signals import Signal, SignalMonitor

ESCALATE = "Escalate"
"""Exit status of an attempt stopped by a signal."""


class WatchedAgent(DefaultAgent):
    def __init__(self, *args, monitor: SignalMonitor, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor = monitor
        self.signal: Signal | None = None

    def _escalate(self, signal: Signal) -> None:
        self.signal = signal
        raise InterruptAgentFlow(
            {
                "role": "exit",
                "content": f"{signal.name}: {signal.detail}",
                "extra": {"exit_status": ESCALATE, "submission": "", "signal": signal.name},
            }
        )

    def query(self) -> dict:
        if signal := self.monitor.start_step():
            self._escalate(signal)
        return super().query()

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        outputs = []
        fired: Signal | None = None
        for action in actions:
            output = self.env.execute(action)  # raises Submitted when the agent finishes
            outputs.append(output)
            fired = fired or self.monitor.observe(action.get("command", ""), output.get("returncode"))
        observed = self.add_messages(
            *self.model.format_observation_messages(message, outputs, self.get_template_vars())
        )
        if fired:
            self._escalate(fired)
        return observed
