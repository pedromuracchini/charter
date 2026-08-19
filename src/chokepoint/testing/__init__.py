"""Testing utilities Chokepoint exposes to people building on it.

This is library code that ships with the package — the way pytest ships
fixtures or Django ships `django.test` — not this repo's own test suite, which
lives under `/tests` and is excluded from the built distribution.
"""

from chokepoint.testing.harness import fixtures_from_events
from chokepoint.testing.repl import EvaluationTrace, evaluate_synthetic, run_repl

__all__ = [
    "EvaluationTrace",
    "evaluate_synthetic",
    "fixtures_from_events",
    "run_repl",
]
