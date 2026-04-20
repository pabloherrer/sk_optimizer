#!/usr/bin/env python3
"""
run_tests.py — Test runner for S&K optimizer test suite.

Run with: python run_tests.py
          or: python -m tests.run_tests
"""

import sys
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import test module
from tests.test_scenarios import run_all_tests


if __name__ == '__main__':
    sys.exit(run_all_tests())
