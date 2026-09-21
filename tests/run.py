from __future__ import annotations

"""Canonical test entrypoint for local machines and production-like servers."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.runtime_env import apply_test_environment  # noqa: E402


def main() -> int:
    apply_test_environment()
    loader = unittest.defaultTestLoader
    suite = (
        loader.loadTestsFromNames(sys.argv[1:])
        if len(sys.argv) > 1
        else loader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
