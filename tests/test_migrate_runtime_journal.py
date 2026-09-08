from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_runtime_journal import migrate


class RuntimeJournalMigrationTests(unittest.TestCase):
    def test_migrate_markdown_journal_to_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy = Path(temporary_directory) / "runtime.md"
            legacy.write_text(
                """# Runtime Journal

<!-- runtime:start decision-1 -->

## judge.decision · decision-1

```json
{
  "record_id": "decision-1",
  "kind": "judge.decision",
  "recorded_at": "2026-08-25T01:00:00+00:00",
  "payload": {"outcome": "silent", "reason": "已完成。"}
}
```

<!-- runtime:end decision-1 -->
""",
                encoding="utf-8",
            )

            target = migrate(legacy, remove_legacy=True)

            self.assertEqual(target.name, "runtime.jsonl")
            self.assertFalse(legacy.exists())
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["record_id"], "decision-1")


if __name__ == "__main__":
    unittest.main()
