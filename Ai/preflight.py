"""Explicit deployment checks and opt-in development startup tests."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from Ai import database, phone_model

SKIP_ENVIRONMENT_VARIABLE = "SUPER_BOT_SKIP_TESTS"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def diagnose() -> list[str]:
    """Validate the local database and selected inference provider for deployment."""
    db = database.Database(":memory:")
    problems = db.check_integrity()
    if problems:
        raise database.DatabaseError(
            f"The database {db.path} failed its integrity check: {problems[0]}"
        )
    provider = phone_model.provider_description()
    phone_model.probe()
    return [
        "Temporary server database ready (RAM only)",
        f"AI provider ready: {provider}",
    ]


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
    """Opt-in gate (SUPER_BOT_RUN_TESTS=1). Skipped with --skip-tests or SUPER_BOT_SKIP_TESTS=1,
    and when the tests are not shipped (for example in an installed release)."""
    if skip or os.getenv(SKIP_ENVIRONMENT_VARIABLE, "").strip() == "1" or not tests_available():
        return True
    if os.getenv("SUPER_BOT_RUN_TESTS", "").strip() != "1":
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Super Bot deployment checks")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="verify the local database and configured inference provider",
    )
    args = parser.parse_args()
    if not args.diagnose:
        parser.print_help()
        return 2
    try:
        for line in diagnose():
            print(line)
    except (database.DatabaseError, phone_model.PhoneModelError, ValueError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
