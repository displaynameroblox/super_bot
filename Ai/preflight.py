"""Run the project's test suite before a client starts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKIP_ENVIRONMENT_VARIABLE = "SUPER_BOT_SKIP_TESTS"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def tests_available() -> bool:
    return (PROJECT_ROOT / "Ai" / "tests").is_dir()


def run_tests(output=None) -> bool:
    """Run every test; return True only if all pass. Output is shown only on failure."""
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "Ai/tests", "-t", "."],
        cwd=PROJECT_ROOT,
        env={**os.environ, SKIP_ENVIRONMENT_VARIABLE: "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout, file=output or sys.stderr)
    return result.returncode == 0


def ensure_tests_pass(skip: bool = False) -> bool:
    """Gate for client start-up. Skipped with --skip-tests or SUPER_BOT_SKIP_TESTS=1,
    and when the tests are not shipped (for example in an installed release)."""
    if skip or os.getenv(SKIP_ENVIRONMENT_VARIABLE, "").strip() == "1" or not tests_available():
        return True
    print("Running tests before starting...", flush=True)
    if run_tests():
        print("All tests passed.", flush=True)
        return True
    print(
        "Tests failed, so the app was not started. Fix the failures, or start anyway "
        f"with --skip-tests (or {SKIP_ENVIRONMENT_VARIABLE}=1).",
        file=sys.stderr,
    )
    return False
