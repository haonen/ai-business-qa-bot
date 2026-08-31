"""Test package bootstrap: isolate unit tests from production rollout flags."""

from tests.runtime_env import apply_test_environment


apply_test_environment()

