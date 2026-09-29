#!/usr/bin/env python3
"""Run the whole acceptance suite: python tests/run_all.py"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

if __name__ == "__main__":
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_*.py",
                                           top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
