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
    def test_isolated_colored_background_speckles_are_removed(self) -> None:
        image = Image.new("RGB", (160, 160), (22, 42, 76))
        draw = ImageDraw.Draw(image)
        for x, y in ((12, 18), (24, 52), (14, 96), (136, 30), (146, 74), (132, 124)):
            draw.rectangle((x, y, x + 4, y + 4), fill=(170, 110, 80))

        draw.ellipse((44, 18, 116, 146), fill=(205, 190, 175))
        draw.pieslice((38, 10, 122, 92), 180, 360, fill=(28, 24, 42))
        draw.line((60, 77, 70, 77), fill=(18, 18, 24), width=4)
        draw.line((90, 77, 100, 77), fill=(18, 18, 24), width=4)
        draw.arc((70, 102, 90, 117), 0, 180, fill=(90, 40, 50), width=3)

        _cells, rows = build_profile.avatar_to_ascii(image, 40, 0.5, 1.0, "square")
        grid = [row.ljust(40) for row in rows]
        background_cells = sum(
            character != " " for row in grid for character in row[:8] + row[-8:]
        )
        face_cells = sum(
            character != " " for row in grid[5:20] for character in row[10:30]
        )

        self.assertEqual(0, background_cells)
        self.assertGreater(face_cells, 45)

    def test_colored_background_grain_is_suppressed_without_losing_the_face(self) -> None:
        image = Image.new("RGB", (160, 160), (22, 42, 76))
        draw = ImageDraw.Draw(image)
        for x in range(0, 40, 8):
            draw.line((x, 0, x, 159), fill=(57, 77, 111), width=2)
        for x in range(120, 160, 8):
            draw.line((x, 0, x, 159), fill=(57, 77, 111), width=2)

        draw.ellipse((42, 16, 118, 150), fill=(205, 190, 175))
        draw.pieslice((36, 8, 124, 94), 180, 360, fill=(28, 24, 42))
        draw.line((58, 76, 69, 76), fill=(18, 18, 24), width=4)
        draw.line((91, 76, 102, 76), fill=(18, 18, 24), width=4)
        draw.line((78, 85, 75, 101), fill=(60, 50, 55), width=3)
        draw.arc((69, 101, 91, 118), 0, 180, fill=(90, 40, 50), width=3)

        _cells, rows = build_profile.avatar_to_ascii(image, 40, 0.5, 1.0, "square")
        grid = [row.ljust(40) for row in rows]
        background_cells = sum(
            character != " " for row in grid for character in row[:8] + row[-8:]
        )
        face_cells = sum(
            character != " " for row in grid[5:20] for character in row[10:30]
        )

        self.assertLess(background_cells, 18)
        self.assertGreater(face_cells, 55)

    def test_color_texture_does_not_overwhelm_copyable_portrait(self) -> None:
        image = Image.new("RGB", (128, 128))
        pixels = image.load()
        for y in range(128):
            for x in range(128):
                pixels[x, y] = (18, 38, 88) if (x + y) % 2 else (45, 72, 125)

        draw = ImageDraw.Draw(image)
        draw.ellipse((32, 18, 96, 112), fill=(210, 175, 135))
        draw.pieslice((28, 12, 100, 80), 180, 360, fill=(32, 18, 55))
        draw.line((48, 67, 56, 67), fill=(15, 15, 20), width=3)
        draw.line((72, 67, 80, 67), fill=(15, 15, 20), width=3)
        draw.arc((57, 78, 72, 92), 0, 180, fill=(130, 40, 45), width=2)

        _cells, rows = build_profile.avatar_to_ascii(image, 40, 0.5, 1.0, "square")
        grid = [row.ljust(40) for row in rows]
        background_cells = sum(
            character != " " for row in grid[2:19] for character in row[:8] + row[-8:]
        )
        portrait_cells = sum(
            character != " " for row in grid for character in row[10:30]
        )

        self.assertLess(background_cells, 24)
        self.assertGreater(portrait_cells, 100)

    def test_color_edge_map_preserves_equal_luminance_hue_boundaries(self) -> None:
        image = Image.new("RGB", (80, 88), (255, 0, 0))
        ImageDraw.Draw(image).rectangle((40, 0, 79, 87), fill=(0, 130, 0))
        self.assertEqual(
            Image.new("RGB", (1, 1), (255, 0, 0)).convert("L").getpixel((0, 0)),
            Image.new("RGB", (1, 1), (0, 130, 0)).convert("L").getpixel((0, 0)),
        )

        edges = build_profile.color_edge_map(image)

        boundary_strength = max(edges.getpixel((x, y)) for x in (39, 40) for y in range(4, 84))
        flat_strength = max(edges.getpixel((x, 44)) for x in (10, 20, 60, 70))
        self.assertGreater(boundary_strength, 80)
        self.assertLess(flat_strength, 8)

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
        self.assertGreater(active_dots, 180)
        self.assertLess(active_dots, 260)
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
