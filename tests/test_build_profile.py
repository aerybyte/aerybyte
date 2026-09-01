from __future__ import annotations

import unittest
from datetime import datetime as RealDateTime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml
from PIL import Image, ImageDraw

from scripts import build_profile


class FrozenDateTime(RealDateTime):
    frozen_utc = RealDateTime(2026, 9, 1, 17, 20, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz: ZoneInfo | None = None) -> "FrozenDateTime":
        return cls.fromtimestamp(cls.frozen_utc.timestamp(), tz=tz)


class RefreshScheduleTests(unittest.TestCase):
    def test_next_refresh_defaults_to_the_six_hour_slot_on_the_hour(self) -> None:
        FrozenDateTime.frozen_utc = RealDateTime(2026, 9, 1, 17, 20, tzinfo=timezone.utc)
        with patch.object(build_profile, "datetime", FrozenDateTime):
            eta, scheduled_at = build_profile._next_refresh(ZoneInfo("America/New_York"))

        self.assertEqual("4h 40m", eta)
        self.assertEqual("2026-09-01 18:00 EDT", scheduled_at)

    def test_next_refresh_converts_new_york_schedule_into_profile_timezone(self) -> None:
        FrozenDateTime.frozen_utc = RealDateTime(2026, 9, 1, 12, 20, tzinfo=timezone.utc)
        with patch.object(build_profile, "datetime", FrozenDateTime):
            eta, scheduled_at = build_profile._next_refresh(ZoneInfo("Europe/London"))

        self.assertEqual("3h 40m", eta)
        self.assertEqual("2026-09-01 17:00 BST", scheduled_at)

    def test_next_refresh_eta_uses_elapsed_time_across_spring_dst_change(self) -> None:
        FrozenDateTime.frozen_utc = RealDateTime(2027, 3, 14, 5, 18, tzinfo=timezone.utc)
        with patch.object(build_profile, "datetime", FrozenDateTime):
            eta, scheduled_at = build_profile._next_refresh(ZoneInfo("America/New_York"))

        self.assertEqual("4h 42m", eta)
        self.assertEqual("2027-03-14 06:00 EDT", scheduled_at)

    def test_workflow_schedule_matches_generator_defaults(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load(
            (repository_root / ".github/workflows/refresh-profile.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            [
                {
                    "cron": "0 */6 * * *",
                    "timezone": "America/New_York",
                }
            ],
            workflow["on"]["schedule"],
        )

    def test_workflow_runs_tests_before_default_branch_asset_write(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load(
            (repository_root / ".github/workflows/refresh-profile.yml").read_text(encoding="utf-8")
        )

        self.assertIn("pull_request", workflow["on"])
        self.assertIn("test", workflow["jobs"])
        self.assertEqual("test", workflow["jobs"]["build"]["needs"])
        self.assertIn("github.event_name != 'pull_request'", workflow["jobs"]["build"]["if"])


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
        self.assertIn("next scheduled slot = ", markdown)
        self.assertIn("\n```\n", markdown)

    def test_terminal_card_aligns_both_bottom_borders_when_info_is_taller(self) -> None:
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

        markdown = build_profile.render_readme(profile, stats, config, ["portrait"])
        card_lines = markdown.removeprefix("```text\n").split("\n```", 1)[0].splitlines()

        self.assertTrue(card_lines[-1].startswith("+"))
        self.assertIn("   +", card_lines[-1])

    def test_braille_portrait_box_uses_one_glyph_family_for_stable_alignment(self) -> None:
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

        markdown = build_profile.render_readme(profile, stats, config, ["⠁⠀⠈"])
        card_lines = markdown.removeprefix("```text\n").split("\n```", 1)[0].splitlines()
        separator_index = card_lines[0].find("   +")
        left_panel = [line[:separator_index] for line in card_lines]

        self.assertGreater(separator_index, 0)
        self.assertTrue(
            all("\u2800" <= character <= "\u28ff" for line in left_panel for character in line)
        )


class AvatarRenderingTests(unittest.TestCase):
    def test_copyable_avatar_uses_braille_edges_without_filling_dark_regions(self) -> None:
        image = Image.new("RGB", (128, 128), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((24, 24, 104, 104), fill="black")

        cells, rows = build_profile.avatar_to_ascii(
            image,
            width=40,
            vertical_focus=0.5,
            zoom=1.0,
            shape="square",
        )
        grid = [row.ljust(40) for row in rows]
        rendered_characters = [character for row in rows for character in row if character != " "]

        self.assertEqual(" ", grid[len(grid) // 2][20])
        self.assertTrue(any(row[7:10].strip() for row in grid[5:25]))
        self.assertLess(sum(character != " " for row in grid for character in row), 400)
        self.assertTrue(rendered_characters)
        self.assertTrue(all("\u2801" <= character <= "\u28ff" for character in rendered_characters))
        self.assertGreater(len(set(rendered_characters)), 4)
        active_dots = sum(
            bin(ord(character) - 0x2800).count("1") for character in rendered_characters
        )
        self.assertGreater(active_dots, 400)
        self.assertLess(active_dots, 600)
        center_cell = next(
            cell
            for cell in cells
            if cell.row == max(candidate.row for candidate in cells) // 2 and cell.column == 20
        )
        self.assertNotEqual(" ", center_cell.char)

    def test_copyable_avatar_masks_braille_dots_to_the_configured_shape(self) -> None:
        image = Image.new("RGB", (128, 128), "white")
        draw = ImageDraw.Draw(image)
        for offset in range(8, 128, 16):
            draw.line((offset, 0, offset, 127), fill="black", width=4)
            draw.line((0, offset, 127, offset), fill="black", width=4)

        _cells, square_rows = build_profile.avatar_to_ascii(image, 40, 0.5, 1.0, "square")
        _cells, rounded_rows = build_profile.avatar_to_ascii(
            image, 40, 0.5, 1.0, "rounded_square"
        )
        _cells, circle_rows = build_profile.avatar_to_ascii(image, 40, 0.5, 1.0, "circle")

        self.assertNotEqual(square_rows, rounded_rows)
        self.assertNotEqual(square_rows, circle_rows)
        dot_positions = (
            (0, 0, 0x01),
            (0, 1, 0x02),
            (0, 2, 0x04),
            (1, 0, 0x08),
            (1, 1, 0x10),
            (1, 2, 0x20),
            (0, 3, 0x40),
            (1, 3, 0x80),
        )
        dot_width = 80
        dot_rows = len(circle_rows) * 4
        for row, line in enumerate(circle_rows):
            for column, character in enumerate(line):
                if character == " ":
                    continue
                mask = ord(character) - 0x2800
                for offset_x, offset_y, bit in dot_positions:
                    if not mask & bit:
                        continue
                    x = column * 2 + offset_x
                    y = row * 4 + offset_y
                    normalized_x = (x + 0.5 - dot_width / 2) / (dot_width / 2)
                    normalized_y = (y + 0.5 - dot_rows / 2) / (dot_rows / 2)
                    self.assertLessEqual(normalized_x**2 + normalized_y**2, 0.97)


if __name__ == "__main__":
    unittest.main()
