#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""The fork's test suite. No global install, no test framework to add.

    uv run tests/run_tests.py            # everything
    uv run tests/run_tests.py gates      # only tests/test_gates.py

stdlib unittest, discovered from this directory. The PEP 723 header above is
the same mechanism every ADW and the installer already use, so `uv run` is the
only thing a contributor needs on their PATH.
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main(argv: list[str]) -> int:
    pattern = f"test_*{argv[0]}*.py" if argv else "test_*.py"
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
