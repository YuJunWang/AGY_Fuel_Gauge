import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import data_fetcher
import history_logger

OK_CLAUDE = {
    "status": "ok",
    "bars": {},
    "history": {"claude_5h": 64.0, "claude_wk": 88.0},
    "extra": {},
}


class HistoryLoggerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.tmp = os.path.join(d, "usage_history.json")
        p = patch.object(history_logger, "HISTORY_FILE", self.tmp)
        p.start()
        self.addCleanup(p.stop)

    def test_log_usage_partial_fields(self):
        # Bug: AGY offline wrote fake 0 values for AGY fields next to Claude data
        history_logger.log_usage(claude_5h=64.0, claude_wk=88.0)
        with open(self.tmp) as f:
            rec = json.load(f)[-1]
        self.assertEqual(set(rec), {"timestamp", "claude_5h", "claude_wk"})

    def test_log_usage_empty_fields_skipped(self):
        # Bug: both sources failing wrote an empty record that consumed the cooldown
        history_logger.log_usage()
        self.assertFalse(os.path.exists(self.tmp))

    def test_old_records_still_readable(self):
        # Bug: upgrade made pre-existing gemini/external history vanish
        recent = (datetime.now() - timedelta(minutes=5)).isoformat()
        with open(self.tmp, "w") as f:
            json.dump([{"timestamp": recent, "gemini_5h": 90, "external_5h": 80}], f)
        self.assertEqual(len(history_logger.get_history(minutes=60)), 1)

    def test_cooldown_still_enforced(self):
        # Bug: generalizing the signature dropped the 2.5 min cooldown, polluting series
        history_logger.log_usage(claude_5h=1.0)
        history_logger.log_usage(claude_5h=2.0)
        with open(self.tmp) as f:
            self.assertEqual(len(json.load(f)), 1)


class FetchAllTests(unittest.TestCase):
    def test_fetch_all_isolates_failures(self):
        # Bug: one provider raising took down the other provider's data
        with patch("providers.agy.fetch", side_effect=RuntimeError("boom")), \
             patch("providers.claude.fetch", return_value=OK_CLAUDE), \
             patch("history_logger.log_usage") as log:
            out = data_fetcher.fetch_all()
        self.assertEqual(out["agy"]["status"], "error")
        self.assertEqual(out["claude"]["status"], "ok")
        self.assertRegex(out["last_updated"], r"^\d\d:\d\d:\d\d$")
        log.assert_called_once_with(**OK_CLAUDE["history"])


if __name__ == "__main__":
    unittest.main()
