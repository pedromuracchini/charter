"""Logical policy composition — `&` (AND), `|` (OR), `~` (NOT) — combining
multiple `Policy` objects into one, for a deploy pipeline's authorization gate.

Run directly:

    uv run python examples/policy_composition.py
"""

from __future__ import annotations

from tollgate import (
    BLOCK,
    AgentScopedPolicy,
    GuardBlocked,
    PolicySet,
    TollgateInterceptor,
    TollgateRegistry,
)

# --- AND: both conditions must hold ---
tests_passed_policy = PolicySet("tests_passed")
tests_passed_policy.require(
    lambda ctx: ctx.args.get("tests_passed", False),
    on_fail=BLOCK,
    reason="tests have not passed",
)

approved_policy = PolicySet("approved")
approved_policy.require(
    lambda ctx: ctx.args.get("approved_by") is not None,
    on_fail=BLOCK,
    reason="deploy has not been approved by anyone",
)

# --- OR: an incident commander can bypass the normal AND-gated path ---
incident_commander_policy = AgentScopedPolicy(
    name="incident_commander_bypass",
    allowed_roles=["incident_commander"],
    on_fail=BLOCK,
    reason="only an incident commander may bypass the normal deploy gate",
)

deploy_policy = (tests_passed_policy & approved_policy) | incident_commander_policy

# --- NOT: only allow rollback when there IS evidence of a problem ---
canary_healthy_policy = PolicySet("canary_healthy")
canary_healthy_policy.require(
    lambda ctx: ctx.args.get("error_rate", 0) < 0.01,
    on_fail=BLOCK,
    reason="canary error rate is elevated",
)
# ~canary_healthy_policy fails (blocks) exactly when canary_healthy_policy
# found NO violation (metrics look fine), and passes when it DID find one
# (elevated error rate) — i.e. it only allows rollback when there's evidence
# to justify one. Rolling back a healthy deploy is itself a mistake.
rollback_requires_evidence_policy = ~canary_healthy_policy


registry = TollgateRegistry()
registry.register("release_bot", role="ci")
registry.register("oncall_engineer", role="incident_commander")


def deploy(service: str, version: str, tests_passed: bool = False, approved_by: str | None = None) -> dict:
    return {"deployed": service, "version": version}


def rollback(service: str, error_rate: float = 0.0) -> dict:
    return {"rolled_back": service}


def main() -> None:
    release_bot = TollgateInterceptor(registry=registry, agent_id="release_bot", policies=[deploy_policy])
    oncall = TollgateInterceptor(registry=registry, agent_id="oncall_engineer", policies=[deploy_policy])
    rollback_interceptor = TollgateInterceptor(policies=[rollback_requires_evidence_policy])

    # AND: fails without both tests_passed and approved_by.
    try:
        release_bot.call("deploy", deploy, service="api", version="1.2.3", tests_passed=True)
    except GuardBlocked as exc:
        print(f"release_bot (no approval): blocked -> {exc.decision.reason}")

    # AND: succeeds once both conditions are met.
    result = release_bot.call(
        "deploy", deploy, service="api", version="1.2.3", tests_passed=True, approved_by="alice"
    )
    print(f"release_bot (tests+approval): allowed -> {result}")

    # OR: an incident commander bypasses the AND-gate entirely, nothing else set.
    result = oncall.call("deploy", deploy, service="api", version="1.2.2-hotfix")
    print(f"oncall (incident commander): allowed -> {result}")

    # NOT: rollback blocked when the canary looks healthy (no evidence of a problem).
    try:
        rollback_interceptor.call("rollback", rollback, service="api", error_rate=0.001)
    except GuardBlocked as exc:
        print(f"rollback (healthy canary): blocked -> {exc.decision.reason}")

    # NOT: rollback allowed once there's actual evidence (elevated error rate).
    result = rollback_interceptor.call("rollback", rollback, service="api", error_rate=0.05)
    print(f"rollback (elevated error rate): allowed -> {result}")


if __name__ == "__main__":
    main()
