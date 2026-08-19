import re
import unittest
from pathlib import Path


class RequirementsTest(unittest.TestCase):
    def test_mcp_requirement_excludes_sdk_2(self):
        requirements = Path(__file__).parents[1] / "requirements.txt"
        mcp_line = next(
            line for line in requirements.read_text().splitlines()
            if re.match(r"^mcp(?:[<>=!~]|$)", line)
        )

        self.assertRegex(mcp_line, r"mcp>=1\.0\.0,<2(?:\.0\.0)?")


if __name__ == "__main__":
    unittest.main()
