import unittest

from sdlc_extract import classify_tool


class ClassificationTests(unittest.TestCase):
    def test_agent_defaults_to_code(self) -> None:
        self.assertEqual(classify_tool("Agent", {"subagent_type": "implementer"}), "Code")

    def test_agent_review_signal(self) -> None:
        self.assertEqual(classify_tool("Agent", {"subagent_type": "pr-reviewer"}), "Review")

    def test_skill_defaults_to_code(self) -> None:
        self.assertEqual(classify_tool("Skill", {"skill": "refactor-helper"}), "Code")

    def test_skill_review_signal(self) -> None:
        self.assertEqual(classify_tool("Skill", {"skill": "security-review"}), "Review")

    def test_discover_tools(self) -> None:
        self.assertEqual(classify_tool("Read", {}), "Discover")
        self.assertEqual(classify_tool("Grep", {}), "Discover")
        self.assertEqual(classify_tool("Glob", {}), "Discover")

    def test_bash_priority(self) -> None:
        self.assertEqual(classify_tool("Bash", {"command": "pytest -q"}), "Test")
        self.assertEqual(classify_tool("Bash", {"command": "gh pr review 123"}), "Review")
        self.assertEqual(classify_tool("Bash", {"command": "git push origin main"}), "Deliver")


if __name__ == "__main__":
    unittest.main()
