from __future__ import annotations

"""Stable feature-flag baseline for unit tests.

Production servers commonly export .env before invoking unittest. Without an
explicit baseline, V1 contract tests silently run through V2 routes and report
false regressions. Individual V2 tests may still override these values with
patch.dict(os.environ, ...).
"""

import os


TEST_FEATURE_FLAGS = {
    "ROUTER_V2_ENABLED": "0",
    "ROUTER_V2_SHADOW": "0",
    "ENTITY_RESOLVER_V2_ENABLED": "0",
    "ENTITY_RESOLVER_V2_SHADOW": "0",
    "AGENT_PLAN_LAYER_ENABLED": "0",
    "AGENT_PLAN_LAYER_SHADOW": "0",
    "EXECUTABLE_PLAN_V2_ENABLED": "0",
    "EXECUTABLE_PLAN_V2_SHADOW": "0",
    "EXECUTABLE_PLAN_DERIVE_ENABLED": "0",
    "EXECUTABLE_PLAN_GENERATED_ENABLED": "0",
    # Unit tests must not inherit production Qwen credentials/flags. Task
    # context merge remains enabled, while relation classification is covered
    # deterministically unless a test opts in explicitly.
    "TASK_CONTEXT_V2_ENABLED": "1",
    "TASK_CONTEXT_LLM_ENABLED": "0",
    "TASK_CONTEXT_TRACE_ENABLED": "0",
}


def apply_test_environment() -> None:
    os.environ.update(TEST_FEATURE_FLAGS)
