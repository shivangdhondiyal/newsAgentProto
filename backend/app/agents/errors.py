from __future__ import annotations


class AgentRunFailed(RuntimeError):
    """A recorded agent run failed after its audit row was persisted."""

    def __init__(
        self,
        agent_name: str,
        run_id: str,
        message: str,
        errors: list[str] | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.run_id = run_id
        self.errors = list(errors or [message])
        super().__init__(message)
