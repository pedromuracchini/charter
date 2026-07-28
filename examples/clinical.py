"""End-to-end example mirroring the framework's clinical-agent scenario.

Demonstrates: PolicySet, AgentScopedPolicy, ReversibleAction, TollgateRegistry,
and per-agent TollgateInterceptor instances enforcing different access for a
clinical agent (a licensed physician) and a support agent — the same tool is
shared between both agents, and the *interceptor* (carrying agent identity)
decides what each is allowed to do with it.

Run directly to see the enforcement in action:

    uv run python examples/clinical.py

Or inspect it with the CLI (this module follows the `POLICIES` / `REGISTRY` /
`TOOL_NAMES` convention the CLI expects):

    uv run tollgate report --agent examples/clinical.py
    uv run tollgate lint examples/clinical.py
"""

from __future__ import annotations

from tollgate import (
    BLOCK,
    AgentScopedPolicy,
    GuardBlocked,
    PolicySet,
    ReversibleAction,
    TollgateInterceptor,
    TollgateRegistry,
)

# 1. Register agents and their roles.
REGISTRY = TollgateRegistry()
REGISTRY.register("clinical_agent", role="licensed_physician")
REGISTRY.register("support_agent", role="support_staff")

# 2. Define policies.
medical_policy = PolicySet(name="clinical-agent", active_when=lambda ctx: ctx.domain == "healthcare")
medical_policy.require(
    lambda ctx: ctx.caller_role == "licensed_physician" or not ctx.tool_name.startswith("prescribe_"),
    on_fail=BLOCK,
    reason="prescribing requires a licensed physician",
)

# Scoped policy: only a physician may deactivate a patient record. This is the
# policy that actually differs by caller — see `main()` below.
deactivate_policy = AgentScopedPolicy(
    name="only_physician_can_deactivate_patient",
    allowed_roles=["licensed_physician"],
    applies_to=lambda ctx: ctx.tool_name == "deactivate_patient",
    on_fail=BLOCK,
    reason="deactivating a patient record is restricted to physicians",
)

POLICIES = [medical_policy, deactivate_policy]
TOOL_NAMES = ["delete_patient", "deactivate_patient", "prescribe_medication", "read_patient"]

# 3. A simple in-memory "database" and the reversible actions on it.
_patients: dict[int, dict[str, object]] = {1: {"name": "Alex Doe", "active": True}}


def _deactivate(args: dict) -> dict:
    patient_id = args["id"]
    _patients[patient_id]["active"] = False
    return {"deactivated": patient_id}


def _undo_deactivate(args: dict, snapshot: dict) -> None:
    _patients[args["id"]] = snapshot


# Low irreversibility: runs normally, undo is just available if a post-hook ever blocks.
deactivate_patient = ReversibleAction(
    do_fn=_deactivate,
    undo_fn=_undo_deactivate,
    name="deactivate_patient",
    irreversibility_level="low",
    pre_snapshot=lambda args: dict(_patients[args["id"]]),
)


def _delete(args: dict) -> dict:
    return {"deleted": _patients.pop(args["id"])}


def _undo_delete(args: dict, snapshot: dict) -> None:
    _patients[args["id"]] = snapshot


# High irreversibility: always escalates before running. With no Slack/webhook
# handler registered for the target scheme, the default handler denies it —
# even for a licensed physician. This is the framework's "safe by default":
# truly destructive actions need an explicit, configured approval path.
delete_patient = ReversibleAction(
    do_fn=_delete,
    undo_fn=_undo_delete,
    name="delete_patient",
    irreversibility_level="high",
    pre_snapshot=lambda args: dict(_patients[args["id"]]),
)


def main() -> None:
    clinical_interceptor = TollgateInterceptor(
        registry=REGISTRY, agent_id="clinical_agent", policies=POLICIES, mode="enforce"
    )
    support_interceptor = TollgateInterceptor(
        registry=REGISTRY, agent_id="support_agent", policies=POLICIES, mode="enforce"
    )

    # Confused-deputy prevention: the same `deactivate_patient` tool is shared
    # by both agents, but only the physician's interceptor allows it.
    result = clinical_interceptor.call("deactivate_patient", deactivate_patient, domain="healthcare", id=1)
    print(f"clinical_agent (physician): allowed — {result}")

    try:
        support_interceptor.call("deactivate_patient", deactivate_patient, domain="healthcare", id=1)
    except GuardBlocked as exc:
        print(f"support_agent: blocked — {exc.decision.reason}")

    # delete_patient is irreversibility_level="high" -> always escalates, and
    # with no escalation handler configured it's denied for everyone, physician
    # included — demonstrating the framework's fail-safe default.
    try:
        clinical_interceptor.call("delete_patient", delete_patient, domain="healthcare", id=1)
    except GuardBlocked as exc:
        print(f"clinical_agent (physician): blocked — {exc.decision.reason}")


if __name__ == "__main__":
    main()
