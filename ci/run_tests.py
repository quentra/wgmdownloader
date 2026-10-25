"""Run the test suite; on GitHub Actions, report each failure as an error annotation so the
traceback can be read through the REST API (check-run annotations), not only in the log.

Usage: python ci/run_tests.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def annotate(test, trace):
    msg = trace.strip()[-3500:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = test.id().replace("test_wget_gui.", "")
    print("::error title=%s::%s" % (title, msg), flush=True)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromName("test_wget_gui")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("GITHUB_ACTIONS"):
        for test, trace in result.failures + result.errors:
            annotate(test, trace)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
