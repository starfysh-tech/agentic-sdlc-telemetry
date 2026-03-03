import json
import tempfile
import unittest
from pathlib import Path

from sdlc_extract import DB, SessionExtractor


class IncrementalExtractionTests(unittest.TestCase):
    def test_unchanged_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "test.db"
            project_dir = root / "project"
            project_dir.mkdir()
            jsonl = project_dir / "session-1.jsonl"
            jsonl.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "system", "timestamp": "2026-03-01T10:00:00Z", "cwd": "/tmp"}),
                        json.dumps({
                            "type": "assistant",
                            "timestamp": "2026-03-01T10:00:01Z",
                            "message": {
                                "model": "claude",
                                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}],
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        }),
                        json.dumps({
                            "type": "user",
                            "timestamp": "2026-03-01T10:00:02Z",
                            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
                        }),
                    ]
                )
            )

            db = DB(db_path)
            extractor = SessionExtractor(db)
            first = extractor.run([project_dir], full=False)
            second = extractor.run([project_dir], full=False)
            db.close()

            self.assertEqual(first["processed"], 1)
            self.assertEqual(second["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
