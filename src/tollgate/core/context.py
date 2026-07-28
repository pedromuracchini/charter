"""`GuardContext` — the typed, validated context passed to every guard predicate.

Built fresh for each tool call by the evaluation engine (`tollgate._engine`),
combining the tool's own arguments with the ambient session/identity data carried
by `tollgate._scope.ExecutionScope`. Pydantic gives IDE autocomplete and type
validation inside the lambdas that make up most policies, and free JSON
serialization for the ledger and OTEL spans.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from tollgate._scope import ExecutionScope


class GuardContext(BaseModel):
    """Everything a policy predicate needs to know about a single tool call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Execution fields ---
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    domain: str | None = None
    session_id: str = "default"
    step_index: int = 0
    pre_snapshot: Any = None
    state_checksum: str | None = None

    # --- Multi-agent identity fields ---
    caller_agent_id: str | None = None
    caller_role: str | None = None
    delegation_chain: list[str] = Field(default_factory=list)
    trust_level: int = 0

    _checksum_provider: Callable[[], str] | None = PrivateAttr(default=None)
    _consent_provider: Callable[[str], bool] | None = PrivateAttr(default=None)

    @classmethod
    def build(
        cls,
        *,
        tool_name: str,
        args: dict[str, Any],
        scope: ExecutionScope,
        result: Any = None,
        pre_snapshot: Any = None,
    ) -> GuardContext:
        """Construct a `GuardContext` for one tool call from its args and the
        ambient `ExecutionScope` (set by the interceptor or `tollgate.session()`)."""
        ctx = cls(
            tool_name=tool_name,
            args=args,
            result=result,
            domain=scope.domain,
            session_id=scope.session_id,
            step_index=scope.step_index,
            pre_snapshot=pre_snapshot,
            state_checksum=scope.state_checksum,
            caller_agent_id=scope.caller_agent_id,
            caller_role=scope.caller_role,
            delegation_chain=list(scope.delegation_chain),
            trust_level=scope.trust_level,
        )
        ctx._checksum_provider = scope.checksum_provider
        ctx._consent_provider = scope.consent_provider
        return ctx

    def state_checksum_matches(self) -> bool:
        """Compare the recorded `state_checksum` against a freshly computed one.

        Fails safe: returns `False` if no checksum was recorded or no
        `checksum_provider` is configured on the active `ExecutionScope`, rather
        than assuming the state is unchanged.
        """
        if self.state_checksum is None or self._checksum_provider is None:
            return False
        return self._checksum_provider() == self.state_checksum

    def patient_consent_on_file(self, patient_id: str | None) -> bool:
        """Domain-specific helper backed by an injectable consent provider.

        Fails safe (`False`) when no provider is configured on the active
        `ExecutionScope` — see `ExecutionScope.consent_provider`.
        """
        if patient_id is None or self._consent_provider is None:
            return False
        return self._consent_provider(patient_id)

    def recompute_checksum(self) -> str | None:
        """Best-effort call to the active `checksum_provider`, if any.

        Used by the engine to record what the state checksum actually was at
        evaluation time, alongside the expected one, for audit purposes.
        """
        if self._checksum_provider is None:
            return None
        try:
            return self._checksum_provider()
        except Exception:
            return None

    def is_delegated_from(self, agent_id: str) -> bool:
        """Whether `agent_id` appears anywhere in this call's `delegation_chain`."""
        return agent_id in self.delegation_chain

    def trust_at_least(self, level: int) -> bool:
        """Whether the caller's `trust_level` meets or exceeds `level`."""
        return self.trust_level >= level
