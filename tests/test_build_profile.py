from __future__ import annotations

import base64
import io
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
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


def copyable_card(markdown: str) -> str:
    return markdown.split("```text\n", 1)[1].split("\n```", 1)[0]


class RefreshScheduleTests(unittest.TestCase):
    def test_next_refresh_defaults_to_the_twelve_hour_slot_on_the_hour(self) -> None:
        FrozenDateTime.frozen_utc = RealDateTime(2026, 9, 1, 17, 20, tzinfo=timezone.utc)
        with patch.object(build_profile, "datetime", FrozenDateTime):
            eta, scheduled_at = build_profile._next_refresh(ZoneInfo("America/New_York"))

        self.assertEqual("10h 40m", eta)
        self.assertEqual("2026-09-02 00:00 EDT", scheduled_at)

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

        self.assertEqual("10h 42m", eta)
        self.assertEqual("2027-03-14 12:00 EDT", scheduled_at)

    def test_workflow_schedule_matches_generator_defaults(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load(
            (repository_root / ".github/workflows/refresh-profile.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            [
                {
                    "cron": "0 */12 * * *",
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


def profile_fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    profile = {
        "login": "aeiree",
        "name": "Eri Reilly",
        "bio": "SWE and Professional Cool Kid",
        "blog": "https://aeiree.com/",
        "created_at": "2004-07-13T00:00:00Z",
    }
    stats = {
        "repo_count": 1,
        "commits": 315,
        "additions": 2190,
        "deletions": 327,
        "lines_of_code": 2629,
        "source": "GitHub GraphQL",
    }
    config = {
        "profile": {
            "card_title": "Eri Reilly",
            "role": "Software Engineer",
            "discord": "@aeiree",
            "email": "",
            "additional_fields": {
                "focus": "Full-Stack Systems · Applied AI · Infrastructure",
                "education": "Rutgers University — CS + Marketing",
            },
            "show_website": True,
        },
        "sections": {
            "stack": {
                "languages": "TypeScript · Python · JavaScript · SQL",
                "frontend": "React · React Native · Vite · Capacitor · Next.js",
                "backend": "Node.js · PostgreSQL · Supabase · PostGIS · SQLite",
                "ai": "RAG · Open WebUI · LLM Tool Calling · NLP",
                "infra": "Docker · Kubernetes · gVisor · GitHub Actions · UniFi",
                "testing": "Jest · Supertest · Playwright · Appium",
                "platforms": "SAP · Firebase · GitHub · GitLab · Jira · New Relic",
            },
            "current systems": {
                "WOE": "SAP-connected ordering · web / iOS / Android",
                "Wilbur": "Internal RAG assistant · secure AI workbench",
                "ATP": "Geospatial social platform · React Native / PostGIS",
            },
        },
        "uptime": {
            "source": "custom",
            "start_date": "2004-07-13",
            "timezone": "America/New_York",
        },
        "display": {},
    }
    return profile, stats, config


class ReadmeRenderingTests(unittest.TestCase):
    def test_readme_uses_theme_images_with_a_text_only_copyable_fallback(self) -> None:
        profile, stats, config = profile_fixture()

        markdown = build_profile.render_readme(profile, stats, config)
        fallback = copyable_card(markdown)

        self.assertTrue(markdown.startswith("<picture>\n"))
        self.assertIn('media="(prefers-color-scheme: dark)"', markdown)
        self.assertIn("./assets/profile-terminal-dark.svg", markdown)
        self.assertIn("./assets/profile-terminal-light.svg", markdown)
        self.assertIn("<summary>copyable text version</summary>", markdown)
        self.assertEqual(fallback, fallback.lower())
        self.assertNotRegex(fallback, "[\\u2800-\\u28ff]")
        self.assertNotIn("   +", fallback)
        self.assertIn("next scheduled slot = ", fallback)

    def test_profile_card_text_is_lowercase(self) -> None:
        profile, stats, config = profile_fixture()

        markdown = build_profile.render_readme(profile, stats, config)
        fallback = copyable_card(markdown)

        self.assertIn("[eri reilly]", fallback)
        self.assertIn("focus = full-stack systems · applied ai · infrastructure", fallback)
        self.assertIn("education = rutgers university — cs + marketing", fallback)
        self.assertIn("website = aeiree.com", fallback)
        self.assertIn("frontend = react · react native · vite · capacitor · next.js", fallback)
        self.assertIn("woe = sap-connected ordering · web / ios / android", fallback)
        self.assertNotIn("email =", fallback)


class AvatarRenderingTests(unittest.TestCase):
    def test_generator_embeds_the_full_color_avatar_in_the_svg(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            avatar_path = temporary_root / "avatar.png"
            avatar = Image.new("RGB", (8, 8), (255, 0, 0))
            ImageDraw.Draw(avatar).rectangle((4, 0, 7, 7), fill=(0, 0, 255))
            avatar.save(avatar_path)

            subprocess.run(
                [
                    sys.executable,
                    str(repository_root / "scripts/build_profile.py"),
                    "--config",
                    str(repository_root / "profile.template.yml"),
                    "--output-dir",
                    str(temporary_root / "assets"),
                    "--username",
                    "aeiree",
                    "--avatar",
                    str(avatar_path),
                    "--offline",
                ],
                cwd=temporary_root,
                check=True,
                capture_output=True,
                text=True,
            )

            svg = (temporary_root / "assets/profile-terminal-dark.svg").read_text(
                encoding="utf-8"
            )
            match = re.search(r'href="data:image/png;base64,([^"]+)"', svg)

            self.assertIsNotNone(match)
            embedded = Image.open(io.BytesIO(base64.b64decode(match.group(1))))
            self.assertEqual((255, 0, 0), embedded.getpixel((0, 0))[:3])
            self.assertEqual((0, 0, 255), embedded.getpixel((7, 0))[:3])
            self.assertNotIn('class="ascii"', svg)
            self.assertFalse((temporary_root / "assets/avatar-ascii.txt").exists())

    def test_svg_gives_the_expanded_profile_and_portrait_room_to_render(self) -> None:
        profile, stats, config = profile_fixture()
        avatar_uri = build_profile.avatar_data_uri(Image.new("RGB", (8, 8), "red"))

        svg = build_profile.render_svg(build_profile.DARK, profile, stats, config, avatar_uri)
        root = ET.fromstring(svg)
        namespace = "{http://www.w3.org/2000/svg}"
        portrait_panel = next(
            node
            for node in root.iter(f"{namespace}rect")
            if node.attrib.get("x") == "38" and node.attrib.get("y") == "92"
        )
        portrait = next(
            node
            for node in root.iter(f"{namespace}image")
            if node.attrib.get("href", "").startswith("data:image/png;base64,")
        )

        self.assertGreaterEqual(float(root.attrib["width"]), 1500)
        self.assertGreaterEqual(float(portrait_panel.attrib["width"]), 480)
        self.assertGreaterEqual(float(portrait.attrib["width"]), 420)
        self.assertEqual("xMidYMid slice", portrait.attrib["preserveAspectRatio"])
        visible_text = "".join(
            "".join(node.itertext())
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1] in {"title", "desc", "text"}
        )
        self.assertEqual(visible_text, visible_text.lower())


if __name__ == "__main__":
    unittest.main()
