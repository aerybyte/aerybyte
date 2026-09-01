from __future__ import annotations

import unittest
from datetime import datetime as RealDateTime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts import build_profile


class FrozenDateTime(RealDateTime):
    @classmethod
    def now(cls, tz: ZoneInfo | None = None) -> "FrozenDateTime":
        return cls(2026, 9, 1, 13, 20, tzinfo=tz)


class RefreshScheduleTests(unittest.TestCase):
    def test_next_refresh_defaults_to_seventeen_minutes_past_the_six_hour_slot(self) -> None:
        with patch.object(build_profile, "datetime", FrozenDateTime):
            eta, scheduled_at = build_profile._next_refresh(ZoneInfo("America/New_York"))

        self.assertEqual("4h 57m", eta)
        self.assertEqual("2026-09-01 18:17 EDT", scheduled_at)


class ReadmeRenderingTests(unittest.TestCase):
    def test_terminal_card_remains_copyable_fenced_text(self) -> None:
        profile = {
            "login": "aerybyte",
            "created_at": "2004-07-13T00:00:00Z",
        }
        stats = {
            "repo_count": 1,
            "commits": 230,
            "additions": 6299,
            "deletions": 3526,
            "lines_of_code": 2088,
        }
        config = {
            "profile": {"role": "software engineer"},
            "uptime": {
                "source": "custom",
                "start_date": "2004-07-13",
                "timezone": "America/New_York",
            },
            "display": {},
        }

        markdown = build_profile.render_readme(profile, stats, config, ["ASCII"])

        self.assertTrue(markdown.startswith("```text\n"))
        self.assertIn("ASCII", markdown)
        self.assertIn("\n```\n", markdown)


if __name__ == "__main__":
    unittest.main()
