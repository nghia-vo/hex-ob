"""
Run every mock-tier test (tests/*_mock_test.py) in its own interpreter.

This is what CI's `pixi run test-mock` executes; each test file stays a
standalone script (runnable in the profile env too, which has no pytest).
"""

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> None:
    tests = sorted(TESTS_DIR.glob("*_mock_test.py"))
    assert tests, "no *_mock_test.py files found"
    failures = []
    for test in tests:
        print(f"\n=== {test.name} ===")
        result = subprocess.run([sys.executable, str(test)])
        if result.returncode != 0:
            failures.append(test.name)
    print("\n" + "=" * 52)
    if failures:
        sys.exit(f"FAILED: {', '.join(failures)}")
    print(f"ALL {len(tests)} MOCK TEST FILE(S) PASS")


if __name__ == "__main__":
    main()
