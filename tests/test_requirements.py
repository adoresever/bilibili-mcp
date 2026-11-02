import unittest
from pathlib import Path

from packaging.requirements import Requirement


class RequirementsTests(unittest.TestCase):
    def test_mcp_two_is_excluded(self):
        path = Path(__file__).parents[1] / "requirements.txt"
        requirements = {
            item.name: item
            for item in (
                Requirement(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        }

        self.assertTrue(requirements["mcp"].specifier.contains("1.29.1"))
        self.assertFalse(requirements["mcp"].specifier.contains("2.0.0"))

