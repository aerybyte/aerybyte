#!/usr/bin/env python3
"""Build a dynamic, terminal-style GitHub profile card.

Outputs:
    README.md
  assets/profile-terminal-dark.svg
  assets/profile-terminal-light.svg
  assets/avatar-ascii.txt

The hosted workflow discovers the username from GITHUB_REPOSITORY_OWNER, reads
public GitHub profile data, converts the current avatar into color ASCII art,
and refreshes the generated assets on a timezone-aware cron schedule.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import os
import re
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

API_ROOT = "https://api.github.com"
GRAPHQL_URL = f"{API_ROOT}/graphql"
API_VERSION = "2026-03-10"
USER_AGENT = "aerybyte-dynamic-profile/1.0"
ASCII_PALETTE = " .,:;irsXA253hMHGS#9B&@"


@dataclass(frozen=True)
class Theme:
    name: str
    page: str
    panel: str
    panel_alt: str
    border: str
    text: str
    muted: str
    faint: str
    label: str
    success: str
    warning: str
    danger: str
    shadow_opacity: float


DARK = Theme(
    name="dark",
    page="#070b12",
    panel="#0d131f",
    panel_alt="#111a28",
    border="#273449",
    text="#e8eef8",
    muted="#91a1b8",
    faint="#3c4a60",
    label="#f7b37f",
    success="#34d399",
    warning="#fbbf24",
    danger="#fb7185",
    shadow_opacity=0.46,
)

LIGHT = Theme(
    name="light",
    page="#eef2f6",
    panel="#ffffff",
    panel_alt="#f8fafc",
    border="#d7dee8",
    text="#172033",
    muted="#607088",
    faint="#c2ccd9",
    label="#b45309",
    success="#047857",
    warning="#b45309",
    danger="#e11d48",
    shadow_opacity=0.14,
)


@dataclass(frozen=True)
class AsciiCell:
    column: int
    row: int
    char: str
    rgb: tuple[int, int, int]


def xml(value: Any) -> str:
    return html.escape(str(value), quote=True)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clean(value: Any, width: int = 68) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).strip().split())
    if not text:
        return ""
    return textwrap.shorten(text, width=width, placeholder="…")


def format_number(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip().isdigit():
        return clean(value, 28)
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return clean(value, 28)


def parse_hex(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text
        except ValueError:
            pass
    return fallback


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(values: Iterable[int | float]) -> str:
    channels = [int(clamp(round(float(value)), 0, 255)) for value in values]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = clamp(amount, 0.0, 1.0)
    return tuple(round(x * (1 - amount) + y * amount) for x, y in zip(a, b))  # type: ignore[return-value]


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def legible_avatar_color(rgb: tuple[int, int, int], theme: Theme) -> str:
    lum = luminance(rgb)
    if theme.name == "dark":
        lift = clamp((122.0 - lum) / 230.0, 0.06, 0.48)
        adjusted = mix(rgb, (240, 246, 255), lift)
    else:
        deepen = clamp((lum - 142.0) / 300.0, 0.02, 0.34)
        adjusted = mix(rgb, (19, 27, 42), deepen)
    return rgb_to_hex(adjusted)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing configuration file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("profile.yml must contain a top-level mapping")
    return data


def headers(token: str | None = None) -> dict[str, str]:
    result = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def get_json(url: str, token: str | None = None, params: Mapping[str, Any] | None = None) -> Any:
    response = requests.get(url, headers=headers(token), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def post_graphql(token: str, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
    request_headers = headers(token)
    request_headers["Accept"] = "application/json"
    response = requests.post(
        GRAPHQL_URL,
        headers=request_headers,
        json={"query": query, "variables": dict(variables)},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors") or []
    if errors:
        message = "; ".join(str(item.get("message", item)) for item in errors)
        raise RuntimeError(f"GitHub GraphQL error: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub GraphQL response did not contain a data object")
    return data


def fetch_profile(username: str, token: str | None) -> dict[str, Any]:
    data = get_json(f"{API_ROOT}/users/{username}", token)
    if not isinstance(data, dict) or not data.get("login"):
        raise RuntimeError(f"Unexpected profile response for {username!r}")
    return data


def fetch_graphql_stats(
    username: str,
    token: str,
    days: int,
    excluded_languages: set[str],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    safe_days = max(1, min(int(days), 365))
    start = now - timedelta(days=safe_days - 1)

    contribution_query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar { totalContributions }
        }
      }
    }
    """
    contribution_data = post_graphql(
        token,
        contribution_query,
        {"login": username, "from": start.isoformat(), "to": now.isoformat()},
    )
    contribution_user = contribution_data.get("user") or {}
    contributions = (
        contribution_user.get("contributionsCollection", {})
        .get("contributionCalendar", {})
        .get("totalContributions")
    )

    repository_query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(
          first: 25,
          after: $cursor,
          ownerAffiliations: OWNER,
          isFork: false,
          orderBy: {field: PUSHED_AT, direction: DESC}
        ) {
          pageInfo { hasNextPage endCursor }
          nodes {
            isPrivate
            stargazerCount
            languages(first: 5, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    }
    """

    cursor: str | None = None
    stars = 0
    language_weights: defaultdict[str, int] = defaultdict(int)
    while True:
        result = post_graphql(token, repository_query, {"login": username, "cursor": cursor})
        user = result.get("user") or {}
        repositories = user.get("repositories") or {}
        for repository in repositories.get("nodes") or []:
            if not repository or repository.get("isPrivate"):
                continue
            stars += int(repository.get("stargazerCount") or 0)
            for edge in (repository.get("languages") or {}).get("edges") or []:
                node = edge.get("node") or {}
                name = str(node.get("name") or "").strip()
                if not name or name.casefold() in excluded_languages:
                    continue
                language_weights[name] += int(edge.get("size") or 0)
        page_info = repositories.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return {
        "stars": stars,
        "contributions": int(contributions) if contributions is not None else None,
        "language_weights": dict(language_weights),
        "source": "GitHub GraphQL",
    }


def fetch_rest_repository_stats(
    username: str,
    token: str | None,
    excluded_languages: set[str],
) -> dict[str, Any]:
    page = 1
    stars = 0
    language_weights: Counter[str] = Counter()
    while True:
        repositories = get_json(
            f"{API_ROOT}/users/{username}/repos",
            token,
            params={"type": "owner", "sort": "updated", "per_page": 100, "page": page},
        )
        if not isinstance(repositories, list):
            break
        for repository in repositories:
            if not isinstance(repository, dict) or repository.get("fork"):
                continue
            stars += int(repository.get("stargazers_count") or 0)
            language = str(repository.get("language") or "").strip()
            if language and language.casefold() not in excluded_languages:
                language_weights[language] += max(1, int(repository.get("size") or 1))
        if len(repositories) < 100 or page >= 20:
            break
        page += 1
    return {
        "stars": stars,
        "contributions": None,
        "language_weights": dict(language_weights),
        "source": "GitHub REST fallback",
    }


def fetch_stats(
    username: str,
    token: str | None,
    days: int,
    excluded_languages: set[str],
) -> dict[str, Any]:
    if token:
        try:
            return fetch_graphql_stats(username, token, days, excluded_languages)
        except Exception as exc:  # noqa: BLE001 - the REST fallback is intentional.
            print(f"warning: GraphQL stats failed; using REST fallback: {exc}", file=sys.stderr)
    try:
        return fetch_rest_repository_stats(username, token, excluded_languages)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: repository stats unavailable; rendering partial data: {exc}", file=sys.stderr)
        return {
            "stars": None,
            "contributions": None,
            "language_weights": {},
            "source": "live sync pending",
        }


def profile_timezone(config: Mapping[str, Any]) -> ZoneInfo:
    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    timezone_name = str(
        os.getenv("PROFILE_TIMEZONE")
        or uptime_config.get("timezone")
        or "UTC"
    ).strip()
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown timezone {timezone_name!r}; use an IANA name such as America/New_York"
        ) from exc


def format_timezone_value(local_zone: ZoneInfo, display_name: str) -> str:
    now = datetime.now(local_zone)
    abbreviation = now.tzname() or ""
    offset = now.utcoffset()

    parts = [display_name.strip() or getattr(local_zone, "key", "Eastern Time")]
    if abbreviation and abbreviation.casefold() not in parts[0].casefold():
        parts.append(abbreviation)

    if offset is not None:
        total_minutes = round(offset.total_seconds() / 60)
        sign = "+" if total_minutes >= 0 else "-"
        total_minutes = abs(total_minutes)
        hours, minutes = divmod(total_minutes, 60)
        parts.append(f"UTC{sign}{hours:02d}:{minutes:02d}")

    return " · ".join(part for part in parts if part)


def parse_start_date(value: str, local_zone: ZoneInfo) -> date:
    text = value.strip()
    if not text:
        raise ValueError("empty date")

    # A date-only value represents that calendar date in the configured timezone.
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(local_zone).date()


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def add_years(moment: date, years: int) -> date:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, month=2, day=28)


def add_months(moment: date, months: int) -> date:
    total = moment.year * 12 + moment.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    month_days = [31, 29 if is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return moment.replace(year=year, month=month, day=min(moment.day, month_days[month - 1]))


def calendar_age(start: date, end: date) -> tuple[int, int, int]:
    if end < start:
        return 0, 0, 0
    years = end.year - start.year
    year_anchor = add_years(start, years)
    if year_anchor > end:
        years -= 1
        year_anchor = add_years(start, years)
    months = (end.year - year_anchor.year) * 12 + end.month - year_anchor.month
    month_anchor = add_months(year_anchor, months)
    if month_anchor > end:
        months -= 1
        month_anchor = add_months(year_anchor, months)
    days = max(0, (end - month_anchor).days)
    return years, months, days


def unit(value: int, name: str) -> str:
    return f"{value} {name if value == 1 else name + 's'}"


def format_uptime(start_value: str, local_zone: ZoneInfo, precision: str = "days") -> str:
    start = parse_start_date(start_value, local_zone)
    today = datetime.now(local_zone).date()
    years, months, days = calendar_age(start, today)

    normalized = precision.strip().lower()
    if normalized not in {"years", "months", "days"}:
        raise ValueError("uptime precision must be years, months, or days")

    parts = [unit(years, "year")]
    if normalized in {"months", "days"}:
        parts.append(unit(months, "month"))
    if normalized == "days":
        parts.append(unit(days, "day"))
    return ", ".join(parts)


def placeholder_avatar(username: str, size: int = 640) -> Image.Image:
    digest = hashlib.sha256(username.encode("utf-8")).digest()
    start = (55 + digest[0] % 145, 55 + digest[1] % 145, 55 + digest[2] % 145)
    end = (55 + digest[3] % 145, 55 + digest[4] % 145, 55 + digest[5] % 145)
    image = Image.new("RGB", (size, size), start)
    pixels = image.load()
    for y in range(size):
        amount = y / max(1, size - 1)
        row = mix(start, end, amount)
        for x in range(size):
            pixels[x, y] = row
    draw = ImageDraw.Draw(image)
    padding = size // 5
    draw.rounded_rectangle(
        (padding, padding, size - padding, size - padding),
        radius=size // 10,
        outline=(245, 248, 255),
        width=max(5, size // 70),
    )
    for index in range(3):
        x = padding + size // 9 + index * size // 7
        height = size // 3 + digest[6 + index] % (size // 7)
        draw.rounded_rectangle(
            (x, size // 2 - height // 2, x + size // 14, size // 2 + height // 2),
            radius=size // 35,
            fill=(245, 248, 255),
        )
    return image


def open_avatar(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def save_avatar_cache(image: Image.Image, path: Path) -> bool:
    """Save the last successful GitHub avatar without rewriting identical pixels."""
    normalized = image.convert("RGB")
    if path.exists():
        try:
            existing = open_avatar(path)
            if existing.size == normalized.size and existing.tobytes() == normalized.tobytes():
                return False
        except Exception:  # noqa: BLE001 - a damaged cache should simply be replaced.
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        normalized.save(temporary, format="PNG", optimize=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def fetch_avatar(
    profile: Mapping[str, Any],
    override_path: Path | None,
    cache_path: Path | None,
) -> Image.Image:
    if override_path:
        try:
            return open_avatar(override_path)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: local avatar override could not be opened: {exc}", file=sys.stderr)

    avatar_url = str(profile.get("avatar_url") or "").strip()
    if avatar_url:
        try:
            # The avatar is public. Do not forward the repository token to another host.
            response = requests.get(
                avatar_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                timeout=30,
            )
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as downloaded:
                image = downloaded.convert("RGB")
            if cache_path:
                save_avatar_cache(image, cache_path)
            return image
        except Exception as exc:  # noqa: BLE001
            print(f"warning: avatar download failed; trying cached portrait: {exc}", file=sys.stderr)

    if cache_path and cache_path.exists():
        try:
            return open_avatar(cache_path)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: cached avatar could not be opened: {exc}", file=sys.stderr)

    return placeholder_avatar(str(profile.get("login") or "github"))


def rounded_square_contains(column: int, row: int, width: int, rows: int, radius_ratio: float = 0.16) -> bool:
    radius = max(1.0, min(width, rows) * radius_ratio)
    x = column + 0.5
    y = row + 0.5
    if radius <= x <= width - radius or radius <= y <= rows - radius:
        return True
    corner_x = radius if x < radius else width - radius
    corner_y = radius if y < radius else rows - radius
    return (x - corner_x) ** 2 + (y - corner_y) ** 2 <= radius**2


def inside_shape(column: int, row: int, width: int, rows: int, shape: str) -> bool:
    if shape == "square":
        return True
    if shape == "circle":
        nx = (column + 0.5 - width / 2) / (width / 2)
        ny = (row + 0.5 - rows / 2) / (rows / 2)
        return nx * nx + ny * ny <= 0.97
    return rounded_square_contains(column, row, width, rows)


def avatar_to_ascii(
    image: Image.Image,
    width: int,
    vertical_focus: float,
    zoom: float,
    shape: str,
) -> tuple[list[AsciiCell], list[str]]:
    width = max(30, min(58, int(width)))
    rows = max(24, round(width * 0.76))
    vertical_focus = clamp(float(vertical_focus), 0.0, 1.0)
    zoom = clamp(float(zoom), 1.0, 1.35)
    shape = shape.strip().lower()
    if shape not in {"rounded_square", "circle", "square"}:
        shape = "rounded_square"

    fitted = ImageOps.fit(
        image.convert("RGB"),
        (720, 720),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, vertical_focus),
    )
    if zoom > 1.001:
        inset = round(360 * (1 - 1 / zoom))
        fitted = fitted.crop((inset, inset, 720 - inset, 720 - inset)).resize((720, 720), Image.Resampling.LANCZOS)
    fitted = ImageEnhance.Color(fitted).enhance(1.10)
    fitted = ImageEnhance.Contrast(fitted).enhance(1.06)
    fitted = fitted.filter(ImageFilter.DETAIL)

    colors = fitted.resize((width, rows), Image.Resampling.LANCZOS)
    gray_full = ImageOps.autocontrast(ImageOps.grayscale(fitted), cutoff=1)
    edge_full = ImageOps.autocontrast(gray_full.filter(ImageFilter.FIND_EDGES), cutoff=2)
    gray = gray_full.resize((width, rows), Image.Resampling.LANCZOS)
    edges = edge_full.resize((width, rows), Image.Resampling.LANCZOS)

    cells: list[AsciiCell] = []
    text_rows: list[str] = []
    for row in range(rows):
        line: list[str] = []
        for column in range(width):
            if not inside_shape(column, row, width, rows, shape):
                line.append(" ")
                continue
            intensity = int(gray.getpixel((column, row)))
            edge = int(edges.getpixel((column, row)))
            density = 0.79 * (1 - intensity / 255.0) + 0.21 * (edge / 255.0)
            palette_index = min(len(ASCII_PALETTE) - 1, max(0, int(density * (len(ASCII_PALETTE) - 1))))
            char = ASCII_PALETTE[palette_index]
            rgb = tuple(int(channel) for channel in colors.getpixel((column, row)))
            if char != " ":
                cells.append(AsciiCell(column, row, char, rgb))
            line.append(char)
        text_rows.append("".join(line).rstrip())
    return cells, text_rows


def offline_fixture(username: str, config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = config.get("preview") if isinstance(config.get("preview"), dict) else {}
    return (
        {
            "login": username,
            "name": preview.get("name") or username,
            "bio": "GRINDING",
            "location": "",
            "blog": "",
            "email": None,
            "public_repos": preview.get("public_repos", "live on first run"),
            "followers": "live on first run",
            "created_at": "",
            "avatar_url": "",
        },
        {
            "stars": preview.get("stars", "live on first run"),
            "contributions": "live on first run",
            "language_weights": {},
            "source": "preview",
        },
    )


def profile_rows(profile: Mapping[str, Any], config: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    rows = [
        ("handle", f"@{profile.get('login', '')}", "accent2"),
        ("role", clean(profile_config.get("role"), 58), "text"),
        ("tagline", clean(profile_config.get("tagline"), 58), "text"),
    ]
    if profile_config.get("show_location", False):
        rows.append(("location", clean(profile.get("location"), 58), "text"))
    if profile_config.get("show_website", False):
        website = clean(profile.get("blog"), 58).removeprefix("https://").removeprefix("http://").rstrip("/")
        rows.append(("website", website, "accent2"))
    return [(label, value, color) for label, value, color in rows if value]


def custom_sections(config: Mapping[str, Any]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    sections_config = config.get("sections") if isinstance(config.get("sections"), dict) else {}
    output: list[tuple[str, list[tuple[str, str, str]]]] = []
    for section_name, raw_rows in sections_config.items():
        if not isinstance(raw_rows, dict):
            continue
        rows = []
        for label, value in raw_rows.items():
            cleaned = clean(value, 58)
            if cleaned:
                rows.append((clean(label, 22), cleaned, "text"))
        if rows:
            output.append((clean(section_name, 28), rows))
    return output


def github_rows(profile: Mapping[str, Any], stats: Mapping[str, Any], config: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    display = config.get("display") if isinstance(config.get("display"), dict) else {}

    uptime_label = clean(uptime_config.get("label") or "github uptime", 22)
    source = str(uptime_config.get("source") or "github_account").strip().lower()
    local_zone = profile_timezone(config)
    precision = str(uptime_config.get("precision") or "days")
    timezone_display = str(
        uptime_config.get("timezone_display")
        or getattr(local_zone, "key", "Eastern Time")
    )
    timezone_value = format_timezone_value(local_zone, timezone_display)

    if source == "custom":
        # PROFILE_START_DATE is an optional private override. The configured date
        # is used when the secret/environment variable is blank.
        start_value = str(
            os.getenv("PROFILE_START_DATE")
            or uptime_config.get("start_date")
            or ""
        ).strip()
    else:
        start_value = str(profile.get("created_at") or "").strip()

    try:
        uptime_value = format_uptime(start_value, local_zone, precision)
    except Exception:
        uptime_value = (
            "add PROFILE_START_DATE secret"
            if source == "custom"
            else "live on first run"
        )

    language_weights = stats.get("language_weights") or {}
    top_count = max(1, min(6, int(display.get("top_languages") or 4)))
    ordered = sorted(language_weights.items(), key=lambda item: item[1], reverse=True)
    languages = " · ".join(name for name, _ in ordered[:top_count])

    rows = [
        (uptime_label, uptime_value, "success"),
        ("timezone", clean(timezone_value, 58), "text"),
        ("public languages", clean(languages, 58), "text"),
    ]
    return [(label, value, color) for label, value, color in rows if value]


def build_sections(
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    result = [("profile", profile_rows(profile, config))]
    result.extend(custom_sections(config))
    result.append(("github --live", github_rows(profile, stats, config)))
    return [(name, rows) for name, rows in result if rows]


def render_svg(
    theme: Theme,
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
    cells: list[AsciiCell],
    ascii_width: int,
) -> str:
    theme_config = config.get("theme") if isinstance(config.get("theme"), dict) else {}
    accent = parse_hex(theme_config.get("accent"), "#f97316")
    accent_2 = parse_hex(theme_config.get("accent_2"), "#14b8a6")
    if theme.name == "light":
        accent = rgb_to_hex(mix(hex_to_rgb(accent), (20, 27, 40), 0.13))
        accent_2 = rgb_to_hex(mix(hex_to_rgb(accent_2), (20, 27, 40), 0.23))

    sections = build_sections(profile, stats, config)
    total_rows = sum(len(rows) for _, rows in sections)
    section_count = len(sections)

    width = 1280
    content_top = 98
    section_header = 32
    row_height = 29
    section_gap = 14
    content_height = section_count * section_header + total_rows * row_height + max(0, section_count - 1) * section_gap
    height = max(760, content_top + content_height + 75)

    art_panel_x = 38
    art_panel_y = 92
    art_panel_width = 410
    divider_x = 468
    art_x = 62
    art_y = 111
    cell_width = min(7.25, 360.0 / max(1, ascii_width))
    cell_height = cell_width * 1.48
    art_rows = max((cell.row for cell in cells), default=0) + 1
    art_height = art_rows * cell_height
    name_y = art_y + art_height + 45
    art_panel_height = max(530, name_y - art_panel_y + 42)

    right_x = 510
    value_x = 704
    right_end = width - 52
    name = clean(profile.get("name") or profile.get("login") or "aery", 34)
    login = clean(profile.get("login") or "aerybyte", 30)
    local_zone = profile_timezone(config)
    refreshed = datetime.now(local_zone).strftime("%Y-%m-%d %H:%M %Z")
    source = clean(stats.get("source") or "live GitHub data", 22)
    top_status = clean(profile.get("bio") or "grinding", 18).lower()

    value_colors = {
        "text": theme.text,
        "accent": accent,
        "accent2": accent_2,
        "success": theme.success,
        "warning": theme.warning,
        "danger": theme.danger,
    }

    parts: list[str] = [
        f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">Dynamic GitHub profile card for @{xml(login)}</title>
<desc id="desc">A terminal-style profile with color ASCII art generated from the current GitHub avatar and refreshed public statistics.</desc>
<defs>
  <linearGradient id="pageGradient" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{theme.page}"/>
    <stop offset="55%" stop-color="{theme.panel}"/>
    <stop offset="100%" stop-color="{theme.page}"/>
  </linearGradient>
  <linearGradient id="rimGradient" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%" stop-color="{accent}"/>
    <stop offset="53%" stop-color="{accent_2}"/>
    <stop offset="100%" stop-color="{theme.success}"/>
  </linearGradient>
  <radialGradient id="portraitGlow" cx="48%" cy="43%" r="68%">
    <stop offset="0%" stop-color="{accent}" stop-opacity="0.16"/>
    <stop offset="100%" stop-color="{accent}" stop-opacity="0"/>
  </radialGradient>
  <filter id="cardShadow" x="-10%" y="-10%" width="120%" height="125%">
    <feDropShadow dx="0" dy="18" stdDeviation="22" flood-color="#000000" flood-opacity="{theme.shadow_opacity}"/>
  </filter>
  <clipPath id="cardClip"><rect x="20" y="20" width="1240" height="{height - 40}" rx="22"/></clipPath>
</defs>
<style>
  .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
  .ascii {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; font-size: 11.2px; font-weight: 700; }}
  .section {{ font-size: 15px; font-weight: 760; letter-spacing: 0.07em; }}
  .label {{ font-size: 15px; font-weight: 650; }}
  .value {{ font-size: 15px; font-weight: 540; }}
  .cursor {{ animation: blink 1.15s steps(2, start) infinite; }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}
</style>
<rect width="{width}" height="{height}" fill="url(#pageGradient)"/>
<g filter="url(#cardShadow)">
  <rect x="20" y="20" width="1240" height="{height - 40}" rx="22" fill="{theme.panel}" stroke="{theme.border}" stroke-width="1.5"/>
</g>
<g clip-path="url(#cardClip)">
  <rect x="20" y="20" width="1240" height="4" fill="url(#rimGradient)"/>
  <circle cx="218" cy="275" r="255" fill="url(#portraitGlow)"/>
</g>
<circle cx="49" cy="50" r="6" fill="{theme.danger}"/>
<circle cx="70" cy="50" r="6" fill="{theme.warning}"/>
<circle cx="91" cy="50" r="6" fill="{theme.success}"/>
<text x="116" y="56" class="mono" font-size="14" fill="{theme.muted}">@{xml(login)} / README.md</text>
<text x="1218" y="56" class="mono" font-size="13" text-anchor="end" fill="{theme.muted}">{xml(top_status)}<tspan class="cursor" fill="{accent_2}">_</tspan></text>
<line x1="34" y1="76" x2="1246" y2="76" stroke="{theme.border}"/>
<rect x="{art_panel_x}" y="{art_panel_y}" width="{art_panel_width}" height="{art_panel_height:.1f}" rx="18" fill="{theme.panel_alt}" stroke="{theme.border}"/>
'''
    ]

    for cell in cells:
        x = art_x + cell.column * cell_width
        y = art_y + (cell.row + 1) * cell_height
        parts.append(
            f'<text x="{x:.2f}" y="{y:.2f}" class="ascii" fill="{legible_avatar_color(cell.rgb, theme)}">{xml(cell.char)}</text>'
        )

    parts.append(
        f'''
<text x="243" y="{name_y:.1f}" class="mono" font-size="26" font-weight="780" text-anchor="middle" fill="{theme.text}">{xml(name)}</text>
<text x="243" y="{name_y + 28:.1f}" class="mono" font-size="15" text-anchor="middle" fill="{accent_2}">@{xml(login)}</text>
<line x1="{divider_x}" y1="93" x2="{divider_x}" y2="{height - 53}" stroke="{theme.border}"/>
'''
    )

    cursor_y = content_top
    for section_name, rows in sections:
        parts.append(
            f'<text x="{right_x}" y="{cursor_y}" class="mono section" fill="{accent}"><tspan fill="{accent_2}">$</tspan> {xml(section_name)}</text>'
        )
        cursor_y += section_header
        for label, value, color_key in rows:
            parts.append(
                f'<text x="{right_x}" y="{cursor_y}" class="mono label" fill="{theme.label}">{xml(label)}</text>'
            )
            parts.append(
                f'<line x1="{right_x + 145}" y1="{cursor_y - 5}" x2="{value_x - 18}" y2="{cursor_y - 5}" stroke="{theme.faint}" stroke-width="1.2" stroke-dasharray="1 7" stroke-linecap="round"/>'
            )
            parts.append(
                f'<text x="{value_x}" y="{cursor_y}" class="mono value" fill="{value_colors.get(color_key, theme.text)}">{xml(value)}</text>'
            )
            cursor_y += row_height
        cursor_y += section_gap

    parts.append(
        f'''
<line x1="{right_x}" y1="{height - 56}" x2="{right_end}" y2="{height - 56}" stroke="{theme.border}"/>
<text x="{right_x}" y="{height - 31}" class="mono" font-size="12.5" fill="{theme.muted}">refreshed {xml(refreshed)} · {xml(source)}</text>
<text x="{right_end}" y="{height - 31}" class="mono" font-size="12.5" text-anchor="end" fill="{theme.muted}">avatar -&gt; ASCII · adaptive</text>
</svg>
'''
    )
    return "".join(parts)


_REFRESHED_STAMP_RE = re.compile(r"(>refreshed )[^<]+?( · )", re.IGNORECASE)
_README_SYNC_STAMP_RE = re.compile(r"(last sync: )(.+)", re.IGNORECASE)


def _normalize_svg_refresh_stamp(svg: str) -> str:
    """Ignore only the volatile footer timestamp during change detection."""
    return _REFRESHED_STAMP_RE.sub(r"\1__LAST_MEANINGFUL_UPDATE__\2", svg, count=1)


def _normalize_readme_sync_stamp(markdown: str) -> str:
    """Ignore only the volatile sync stamp during change detection."""
    return _README_SYNC_STAMP_RE.sub(r"\1__LAST_MEANINGFUL_UPDATE__", markdown, count=1)


def _terminal_row_parts(label: str, value: str, width: int, label_width: int) -> tuple[str, str, str]:
    clean_label = clean(label, label_width)
    base = f"{clean_label}: "
    max_value = max(10, width - len(base) - 5)
    clean_value = clean(value, max_value)
    dots = "." * max(2, width - len(base) - len(clean_value) - 1)
    return base, dots, clean_value


def _terminal_row(label: str, value: str, width: int = 74, label_width: int = 25) -> str:
    base, dots, clean_value = _terminal_row_parts(label, value, width, label_width)
    return f"{base}{dots} {clean_value}"


def _section_header(title: str, width: int = 74) -> str:
    heading = clean(title, 28).lower()
    suffix = max(2, width - len(heading) - 1)
    return f"{heading} {'-' * suffix}"


def _next_refresh(local_zone: ZoneInfo, minute: int = 17, hour_step: int = 6) -> tuple[str, str]:
    now = datetime.now(local_zone).replace(second=0, microsecond=0)
    candidate = now.replace(minute=minute)
    if now.minute > minute:
        candidate = candidate + timedelta(hours=1)

    while candidate <= now or candidate.hour % hour_step != 0:
        candidate = candidate + timedelta(hours=1)

    delta = candidate - now
    total_minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    eta = f"{hours}h {minutes}m"
    when = candidate.strftime("%Y-%m-%d %H:%M %Z")
    return eta, when


def _ordered_sections(
    section_map: Mapping[str, list[tuple[str, str, str]]],
    config: Mapping[str, Any],
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    configured = display.get("readme_section_order")

    if isinstance(configured, list):
        preferred = [str(item).strip().lower() for item in configured if str(item).strip()]
    else:
        preferred = ["profile", "now", "stack", "github --live", "contact", "automation"]

    ordered: list[tuple[str, list[tuple[str, str, str]]]] = []
    seen: set[str] = set()
    for key in preferred:
        if key in section_map and key not in seen:
            ordered.append((key, section_map[key]))
            seen.add(key)

    for key, rows in section_map.items():
        if key not in seen:
            ordered.append((key, rows))
    return ordered


def _pick_rows(
    rows: list[tuple[str, str, str]],
    allowed_labels: set[str],
) -> list[tuple[str, str, str]]:
    if not allowed_labels:
        return rows
    return [entry for entry in rows if entry[0].strip().lower() in allowed_labels]


def _fit_square_ascii_for_readme(
    ascii_rows: list[str],
    row_ratio: float,
) -> list[str]:
    if not ascii_rows:
        return ascii_rows

    width = max((len(row) for row in ascii_rows), default=0)
    if width <= 0:
        return ascii_rows

    ratio = clamp(float(row_ratio), 0.42, 0.80)
    target_rows = max(18, int(round(width * ratio)))
    source_rows = [row.ljust(width) for row in ascii_rows]
    source_count = len(source_rows)
    if source_count <= 1 or source_count == target_rows:
        return source_rows

    output: list[str] = []
    for index in range(target_rows):
        source_index = round(index * (source_count - 1) / max(1, target_rows - 1))
        output.append(source_rows[source_index])
    return output


def _plain(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text.replace("`", "")


def _clip_no_ellipsis(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text[:width]


def _center_no_overflow(text: str, width: int) -> str:
    clipped = _clip_no_ellipsis(_plain(text), width)
    return clipped.center(width)


def _split_top_skills(value: Any, limit: int = 4) -> str:
    raw = _plain(value)
    if not raw:
        return ""
    tokens = [token.strip() for token in re.split(r"\s*[·,|]\s*", raw) if token.strip()]
    return " · ".join(tokens[: max(1, limit)])


def _box_lines(lines: list[str], width: int, centered: bool = False) -> list[str]:
    top = f"+{'-' * (width + 2)}+"
    output = [top]
    for line in lines:
        if centered:
            content = _center_no_overflow(line, width)
        else:
            content = _clip_no_ellipsis(str(line), width).ljust(width)
        output.append(f"| {content} |")
    output.append(top)
    return output


def _metadata_rows_for_readme(profile: Mapping[str, Any], stats: Mapping[str, Any], config: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    sections_config = config.get("sections") if isinstance(config.get("sections"), dict) else {}
    now_config = sections_config.get("now") if isinstance(sections_config.get("now"), dict) else {}
    stack_config = sections_config.get("stack") if isinstance(sections_config.get("stack"), dict) else {}

    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    local_zone = profile_timezone(config)
    timezone_display = str(uptime_config.get("timezone_display") or getattr(local_zone, "key", "Eastern Time"))
    _next_eta, next_at = _next_refresh(local_zone)
    stats_source = clean(stats.get("source") or "live GitHub data", 40)

    uptime_label = clean(uptime_config.get("label") or "human uptime", 22)
    source = str(uptime_config.get("source") or "github_account").strip().lower()
    precision = str(uptime_config.get("precision") or "days")
    if source == "custom":
        start_value = str(os.getenv("PROFILE_START_DATE") or uptime_config.get("start_date") or "").strip()
    else:
        start_value = str(profile.get("created_at") or "").strip()
    try:
        uptime_value = format_uptime(start_value, local_zone, precision)
    except Exception:
        uptime_value = "live sync pending"

    curated = [
        ("languages", _split_top_skills(stack_config.get("languages"), 4)),
        ("frontend", _split_top_skills(stack_config.get("frontend"), 3)),
        ("backend", _split_top_skills(stack_config.get("backend & data"), 3)),
        ("devops", _split_top_skills(stack_config.get("testing & devops"), 3)),
        ("cloud", _split_top_skills(stack_config.get("cloud/security/obs"), 3)),
        ("analytics", _split_top_skills(stack_config.get("analytics"), 3)),
    ]

    live_language_weights = stats.get("language_weights") or {}
    if isinstance(live_language_weights, dict) and live_language_weights:
        live_languages = " · ".join(
            name for name, _ in sorted(live_language_weights.items(), key=lambda item: item[1], reverse=True)[:4]
        )
    else:
        live_languages = _split_top_skills(stack_config.get("languages"), 4)

    profile_rows = [
        f"handle: @{_plain(profile.get('login') or 'aerybyte')}",
        f"role: {_plain(profile_config.get('role') or 'software engineer · product builder')}",
    ]
    now_rows = [
        f"building: {_plain(now_config.get('building') or 'shipping product work')}",
        f"learning: {_plain(now_config.get('learning') or 'architecture · product velocity')}",
    ]
    stack_rows = [f"{name}: {value}" for name, value in curated if value]
    live_rows = [
        f"{uptime_label}: {uptime_value}",
        f"timezone: {format_timezone_value(local_zone, timezone_display)}",
        f"languages: {live_languages}",
    ]
    automation_rows = [
        f"data source: {stats_source}",
        f"next refresh at: {next_at}",
        f"refresh timezone: {timezone_display}",
    ]

    return [
        ("profile", profile_rows),
        ("now", now_rows),
        ("top skills", stack_rows),
        ("github live", live_rows),
        ("automation", automation_rows),
    ]


def _terminal_info_lines(
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
    panel_width: int = 68,
    label_width: int = 21,
) -> list[str]:
    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    local_zone = profile_timezone(config)
    refreshed = datetime.now(local_zone).strftime("%Y-%m-%d %H:%M %Z")
    next_eta, next_at = _next_refresh(local_zone)

    lines: list[str] = []
    section_map: dict[str, list[tuple[str, str, str]]] = {
        name.lower(): rows for name, rows in build_sections(profile, stats, config)
    }

    compact_readme = bool(display.get("readme_compact", True))
    if compact_readme:
        section_map["profile"] = _pick_rows(section_map.get("profile", []), {"handle", "role"})
        section_map["now"] = _pick_rows(section_map.get("now", []), {"building", "learning"})
        section_map["stack"] = _pick_rows(
            section_map.get("stack", []),
            {
                "languages",
                "frontend",
                "backend & data",
                "testing & devops",
                "cloud/security/obs",
                "analytics",
            },
        )
        section_map["github --live"] = _pick_rows(
            section_map.get("github --live", []),
            {"human uptime", "timezone", "public languages"},
        )

    show_public_email = bool(display.get("show_public_email", False))
    contact_rows: list[tuple[str, str, str]] = []
    blog = clean(profile.get("blog"), 50).removeprefix("https://").removeprefix("http://").rstrip("/")
    if blog:
        contact_rows.append(("website", blog, "text"))
    if show_public_email and profile.get("email"):
        contact_rows.append(("email", clean(profile.get("email"), 50), "warning"))
    if contact_rows:
        section_map["contact"] = contact_rows

    days = max(1, min(365, int(display.get("contributions_window_days") or 365)))
    timezone_name = str(
        uptime_config.get("timezone_display")
        or getattr(local_zone, "key", "Eastern Time")
    )
    section_map["automation"] = [
        ("next refresh in", next_eta, "success"),
        ("next refresh at", next_at, "success"),
        ("refresh cron", "17 */6 * * *", "text"),
        ("refresh timezone", timezone_name, "text"),
        ("last sync", refreshed, "text"),
    ]

    lines.append(_section_header(str(profile.get("login") or "aerybyte"), width=panel_width))
    for section_name, rows in _ordered_sections(section_map, config):
        lines.append(_section_header(section_name, width=panel_width))
        for label, value, color_key in rows:
            lines.append(_terminal_row(label, value, width=panel_width, label_width=label_width))
    return lines


def render_readme(
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
    ascii_rows: list[str],
) -> str:
    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    square_rows = _fit_square_ascii_for_readme(
        ascii_rows,
        float(display.get("readme_avatar_rows_ratio") or 0.56),
    )
    intrinsic_left_width = max((len(row) for row in square_rows), default=44)
    left_width = max(38, min(56, int(display.get("readme_ascii_column_width") or intrinsic_left_width)))
    # keep one blank column against the right border to avoid visual artifacts
    # where dense ascii can look like stray punctuation near the divider.
    avatar_inner_width = max(1, left_width - 1)
    avatar_lines = [row[:avatar_inner_width].ljust(avatar_inner_width) + " " for row in square_rows]
    avatar_box_lines = _box_lines(avatar_lines, left_width, centered=False)

    metadata_width = max(56, min(78, int(display.get("readme_info_column_width") or 64)))
    metadata_sections = _metadata_rows_for_readme(profile, stats, config)
    metadata_ini_lines: list[str] = []
    divider = ("- " * (metadata_width // 2 + 2)).strip()[:metadata_width]
    for index, (section_name, rows) in enumerate(metadata_sections):
        safe_section = _plain(section_name)
        metadata_ini_lines.append(f"[{safe_section}]")
        for row in rows:
            safe_row = _plain(row)
            key, separator, value = safe_row.partition(":")
            if separator:
                metadata_ini_lines.append(f"{key.strip()} = {value.strip()}")
            else:
                metadata_ini_lines.append(f"item = {safe_row.strip()}")
        if index < len(metadata_sections) - 1:
            metadata_ini_lines.append(divider)
    metadata_box_lines = _box_lines(metadata_ini_lines, metadata_width, centered=False)

    if len(metadata_box_lines) < len(avatar_box_lines):
        pad_count = len(avatar_box_lines) - len(metadata_box_lines)
        empty_row = f"| {' ' * metadata_width} |"
        metadata_box_lines = (
            metadata_box_lines[:-1] + [empty_row] * pad_count + [metadata_box_lines[-1]]
        )

    combined_lines: list[str] = []
    total_lines = max(len(avatar_box_lines), len(metadata_box_lines))
    left_blank = " " * len(avatar_box_lines[0]) if avatar_box_lines else ""
    right_blank = " " * len(metadata_box_lines[0]) if metadata_box_lines else ""
    for index in range(total_lines):
        left = avatar_box_lines[index] if index < len(avatar_box_lines) else left_blank
        right = metadata_box_lines[index] if index < len(metadata_box_lines) else right_blank
        combined_lines.append(f"{left}   {right}")
    combined = "\n".join(combined_lines).replace("`", "")

    return (
        f"```text\n{combined}\n```\n\n"
        "<!-- Generated by scripts/build_profile.py -->\n"
    )


def write_text_if_changed(path: Path, content: str) -> bool:
    """Write text only when its bytes differ, keeping cron runs quiet."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_svg_if_meaningfully_changed(path: Path, content: str) -> bool:
    """Preserve the previous timestamp when no rendered data actually changed."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _normalize_svg_refresh_stamp(existing) == _normalize_svg_refresh_stamp(content):
            return False
    path.write_text(content, encoding="utf-8")
    return True


def write_readme_if_meaningfully_changed(path: Path, content: str) -> bool:
    """Preserve the previous sync stamp when nothing else changed."""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _normalize_readme_sync_stamp(existing) == _normalize_readme_sync_stamp(content):
            return False
    path.write_text(content, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("profile.yml"))
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument("--username", default=os.getenv("GITHUB_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER"))
    parser.add_argument("--avatar", type=Path, help="Optional local image for a preview or permanent override")
    parser.add_argument("--offline", action="store_true", help="Use bundled preview values and make no GitHub API calls")
    args = parser.parse_args()

    config = load_config(args.config)
    username = str(args.username or "aerybyte").strip()
    token = os.getenv("GITHUB_TOKEN") or None

    if args.offline:
        profile, stats = offline_fixture(username, config)
    else:
        try:
            profile = fetch_profile(username, token)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: profile request failed; using bundled fallback: {exc}", file=sys.stderr)
            profile, _ = offline_fixture(username, config)
        display = config.get("display") if isinstance(config.get("display"), dict) else {}
        excluded = {
            str(item).strip().casefold()
            for item in (display.get("exclude_languages") or [])
            if str(item).strip()
        }
        stats = fetch_stats(
            username,
            token,
            int(display.get("contributions_window_days") or 365),
            excluded,
        )

    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    configured_avatar = str(display.get("avatar_path") or "").strip()
    avatar_path = args.avatar or (Path(configured_avatar) if configured_avatar else None)
    configured_cache = str(display.get("avatar_cache_path") or "assets/avatar.png").strip()
    avatar_cache_path = Path(configured_cache) if configured_cache else None
    avatar = fetch_avatar(profile, avatar_path, avatar_cache_path)
    ascii_width = int(display.get("ascii_width") or 50)
    cells, rows = avatar_to_ascii(
        avatar,
        ascii_width,
        float(display.get("avatar_vertical_focus") or 0.5),
        float(display.get("avatar_zoom") or 1.0),
        str(display.get("ascii_shape") or "rounded_square"),
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    dark_svg = render_svg(DARK, profile, stats, config, cells, max(30, min(58, ascii_width)))
    light_svg = render_svg(LIGHT, profile, stats, config, cells, max(30, min(58, ascii_width)))
    readme = render_readme(profile, stats, config, rows)

    dark_changed = write_svg_if_meaningfully_changed(
        args.output_dir / "profile-terminal-dark.svg", dark_svg
    )
    light_changed = write_svg_if_meaningfully_changed(
        args.output_dir / "profile-terminal-light.svg", light_svg
    )
    ascii_changed = write_text_if_changed(
        args.output_dir / "avatar-ascii.txt", "\n".join(rows) + "\n"
    )
    readme_changed = write_readme_if_meaningfully_changed(Path("README.md"), readme)

    changed = dark_changed or light_changed or ascii_changed or readme_changed
    state = "updated" if changed else "already current"
    print(f"Profile card for @{profile.get('login', username)} is {state} in {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
