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
import json
import hashlib
import html
import io
import os
import re
import subprocess
import sys
import time
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
STATS_MAX_ATTEMPTS = 8
STATS_RETRY_DELAY_SECONDS = 2.0


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
        raise ValueError("config file must contain a top-level mapping")
    return data


def validate_config(config: Mapping[str, Any], config_path: Path) -> None:
    """Validate user template config and raise plain-English errors when invalid."""
    issues: list[str] = []
    notes: list[str] = []

    def expect_mapping(field: str) -> Mapping[str, Any] | None:
        value = config.get(field)
        if value is None:
            return None
        if not isinstance(value, dict):
            issues.append(f"{field} must be a mapping (key/value block) in {config_path.name}.")
            return None
        return value

    profile_config = expect_mapping("profile") or {}
    sections_config = expect_mapping("sections") or {}
    uptime_config = expect_mapping("uptime") or {}
    display_config = expect_mapping("display") or {}

    additional_fields = profile_config.get("additional_fields")
    if additional_fields is not None:
        if not isinstance(additional_fields, dict):
            issues.append("profile.additional_fields must be a key/value mapping of text fields.")

    github_username = profile_config.get("github_username")
    if github_username is not None and not isinstance(github_username, str):
        issues.append("profile.github_username must be text when provided.")

    source = str(uptime_config.get("source") or "github_account").strip().lower()
    if source not in {"custom", "github_account"}:
        notes.append(
            "uptime.source is not one of custom/github_account. The renderer will still run and use defaults where needed."
        )

    precision = str(uptime_config.get("precision") or "days").strip().lower()
    if precision not in {"years", "months", "days"}:
        notes.append(
            "uptime.precision is not one of years/months/days. The renderer may fall back to days."
        )

    timezone_name = str(
        os.getenv("PROFILE_TIMEZONE")
        or uptime_config.get("timezone")
        or "UTC"
    ).strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        issues.append(
            "uptime.timezone must be a valid IANA timezone like America/New_York, Europe/London, or Asia/Tokyo."
        )

    shape = str(display_config.get("ascii_shape") or "rounded_square").strip().lower()
    if shape not in {"rounded_square", "circle", "square"}:
        notes.append(
            "display.ascii_shape is not one of rounded_square/circle/square. The renderer may fall back to rounded_square."
        )

    if "ascii_width" in display_config:
        try:
            int(display_config.get("ascii_width"))
        except (TypeError, ValueError):
            issues.append("display.ascii_width must be a number.")

    section_order = display_config.get("readme_section_order")
    if section_order is not None:
        if not isinstance(section_order, list):
            issues.append("display.readme_section_order must be a list of section names.")
        elif any(not isinstance(item, str) for item in section_order):
            notes.append(
                "display.readme_section_order contains non-text items. They will be stringified where possible."
            )

    stack_config = sections_config.get("stack")
    if stack_config is not None and not isinstance(stack_config, dict):
        issues.append("sections.stack must be a mapping of labels to text.")

    if issues:
        formatted = "\n".join(f"- {issue}" for issue in issues)
        raise ValueError(
            f"Config validation failed for {config_path.name}. Please fix the following:\n{formatted}"
        )

    if notes:
        print(
            "config notes:\n" + "\n".join(f"- {note}" for note in notes),
            file=sys.stderr,
        )


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


def fetch_code_frequency_churn(
    username: str,
    repo_names: list[str],
    token: str | None,
    max_repos: int = 24,
) -> tuple[int | None, int | None]:
    """Return summed additions/deletions across repos using REST code_frequency stats.

    The endpoint is asynchronous and may return 202. In that case we skip that repo.
    """
    additions = 0
    deletions = 0
    has_data = False

    for repo_name in repo_names[:max_repos]:
        response: requests.Response | None = None
        for attempt in range(STATS_MAX_ATTEMPTS):
            response = requests.get(
                f"{API_ROOT}/repos/{username}/{repo_name}/stats/code_frequency",
                headers=headers(token),
                timeout=30,
            )
            if response.status_code != 202:
                break
            if attempt < STATS_MAX_ATTEMPTS - 1:
                time.sleep(STATS_RETRY_DELAY_SECONDS)

        if response is None:
            continue

        if response.status_code == 202:
            continue
        if response.status_code in {403, 404, 451}:
            continue

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            continue

        has_data = True
        for week in payload:
            if not isinstance(week, list) or len(week) < 3:
                continue
            additions += int(week[1] or 0)
            deletions += abs(int(week[2] or 0))

    if not has_data:
        return None, None
    return additions, deletions


def fetch_churn_from_contributors(
    username: str,
    repo_names: list[str],
    token: str | None,
    max_repos: int = 24,
) -> tuple[int | None, int | None]:
    """Fallback churn from contributor weekly stats for the target user.

    This endpoint can also return 202 while GitHub computes values, so we retry
    a few times to improve odds of getting fresh data during scheduled runs.
    """
    additions = 0
    deletions = 0
    has_data = False
    found_user = False
    target = username.casefold()

    for repo_name in repo_names[:max_repos]:
        response: requests.Response | None = None
        for attempt in range(STATS_MAX_ATTEMPTS):
            response = requests.get(
                f"{API_ROOT}/repos/{username}/{repo_name}/stats/contributors",
                headers=headers(token),
                timeout=30,
            )
            if response.status_code != 202:
                break
            if attempt < STATS_MAX_ATTEMPTS - 1:
                time.sleep(STATS_RETRY_DELAY_SECONDS)

        if response is None:
            continue
        if response.status_code == 202:
            continue
        if response.status_code in {403, 404, 451}:
            continue

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            continue

        has_data = True
        for contributor in payload:
            if not isinstance(contributor, dict):
                continue
            author = contributor.get("author")
            if not isinstance(author, dict):
                continue
            login = str(author.get("login") or "").strip().casefold()
            if login != target:
                continue
            found_user = True
            weeks = contributor.get("weeks")
            if not isinstance(weeks, list):
                continue
            for week in weeks:
                if not isinstance(week, dict):
                    continue
                additions += int(week.get("a") or 0)
                deletions += int(week.get("d") or 0)

    if not has_data or not found_user:
        return None, None
    return additions, deletions


def fetch_commit_totals_from_contributors(
    username: str,
    repo_names: list[str],
    token: str | None,
    max_repos: int = 24,
) -> int | None:
    """Return summed commit totals for a user from REST contributors stats.

    The endpoint is asynchronous and may return 202. In that case we skip that repo.
    """
    total_commits = 0
    has_data = False
    found_user = False
    target = username.casefold()

    for repo_name in repo_names[:max_repos]:
        response: requests.Response | None = None
        for attempt in range(STATS_MAX_ATTEMPTS):
            response = requests.get(
                f"{API_ROOT}/repos/{username}/{repo_name}/stats/contributors",
                headers=headers(token),
                timeout=30,
            )
            if response.status_code != 202:
                break
            if attempt < STATS_MAX_ATTEMPTS - 1:
                time.sleep(STATS_RETRY_DELAY_SECONDS)

        if response is None:
            continue

        if response.status_code == 202:
            continue
        if response.status_code in {403, 404, 451}:
            continue

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            continue

        has_data = True
        for contributor in payload:
            if not isinstance(contributor, dict):
                continue
            author = contributor.get("author")
            if not isinstance(author, dict):
                continue
            login = str(author.get("login") or "").strip().casefold()
            if login != target:
                continue
            found_user = True
            total_commits += int(contributor.get("total") or 0)

    if not has_data:
        return None
    if not found_user:
        return None
    return total_commits


def fetch_commit_totals_from_commits_api(
    username: str,
    repo_names: list[str],
    token: str | None,
    max_repos: int = 24,
) -> int | None:
    """Fallback commit count using repository commits pagination.

    This measures total commits in owned repositories, which is stable and
    reliably updates even when author identity matching is inconsistent.
    """
    total = 0
    has_data = False

    for repo_name in repo_names[:max_repos]:
        response = requests.get(
            f"{API_ROOT}/repos/{username}/{repo_name}/commits",
            headers=headers(token),
            params={"per_page": 1},
            timeout=30,
        )

        if response.status_code in {403, 404, 409, 451}:
            continue

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            continue

        has_data = True
        if not payload:
            continue

        link_header = str(response.headers.get("Link") or "")
        last_page_match = re.search(r"[?&]page=(\d+)>;\s*rel=\"last\"", link_header)
        if last_page_match:
            total += int(last_page_match.group(1))
        else:
            total += len(payload)

    if not has_data:
        return None
    return total


def fetch_churn_from_compare_api(
    username: str,
    repo_names: list[str],
    token: str | None,
    max_repos: int = 12,
) -> tuple[int | None, int | None]:
    """Best-effort churn fallback using compare API from oldest commit to HEAD.

    This avoids permanent `live sync pending` when async stats endpoints never
    finish in time. It may undercount in very large repositories because GitHub
    can truncate compare file lists.
    """
    additions = 0
    deletions = 0
    has_data = False

    for repo_name in repo_names[:max_repos]:
        repo_response = requests.get(
            f"{API_ROOT}/repos/{username}/{repo_name}",
            headers=headers(token),
            timeout=30,
        )
        if repo_response.status_code in {403, 404, 451}:
            continue
        repo_response.raise_for_status()
        repo_payload = repo_response.json()
        if not isinstance(repo_payload, dict):
            continue
        default_branch = str(repo_payload.get("default_branch") or "main").strip()
        if not default_branch:
            continue

        head_response = requests.get(
            f"{API_ROOT}/repos/{username}/{repo_name}/commits",
            headers=headers(token),
            params={"per_page": 1},
            timeout=30,
        )
        if head_response.status_code in {403, 404, 409, 451}:
            continue
        head_response.raise_for_status()
        head_payload = head_response.json()
        if not isinstance(head_payload, list) or not head_payload:
            continue

        link_header = str(head_response.headers.get("Link") or "")
        last_page_match = re.search(r"[?&]page=(\d+)>;\s*rel=\"last\"", link_header)
        last_page = int(last_page_match.group(1)) if last_page_match else 1

        oldest_sha = ""
        if last_page <= 1:
            oldest_sha = str(head_payload[0].get("sha") or "").strip()
        else:
            oldest_response = requests.get(
                f"{API_ROOT}/repos/{username}/{repo_name}/commits",
                headers=headers(token),
                params={"per_page": 1, "page": last_page},
                timeout=30,
            )
            if oldest_response.status_code in {403, 404, 409, 451}:
                continue
            oldest_response.raise_for_status()
            oldest_payload = oldest_response.json()
            if isinstance(oldest_payload, list) and oldest_payload:
                oldest_sha = str(oldest_payload[0].get("sha") or "").strip()

        if not oldest_sha:
            continue

        compare_response = requests.get(
            f"{API_ROOT}/repos/{username}/{repo_name}/compare/{oldest_sha}...{default_branch}",
            headers=headers(token),
            timeout=45,
        )
        if compare_response.status_code in {403, 404, 409, 422, 451}:
            continue
        compare_response.raise_for_status()
        compare_payload = compare_response.json()
        if not isinstance(compare_payload, dict):
            continue
        files = compare_payload.get("files")
        if not isinstance(files, list):
            continue

        has_data = True
        for changed_file in files:
            if not isinstance(changed_file, dict):
                continue
            additions += int(changed_file.get("additions") or 0)
            deletions += int(changed_file.get("deletions") or 0)

    if not has_data:
        return None, None
    return additions, deletions


def fetch_local_git_churn_if_single_repo(username: str, repo_names: list[str]) -> tuple[int | None, int | None]:
    """Fallback churn from local git history when the profile has one owned repo.

    This only applies when the current checkout matches the single discovered
    repository, preventing accidental partial totals for multi-repo profiles.
    """
    if len(repo_names) != 1:
        return None, None

    repo_ref = str(os.getenv("GITHUB_REPOSITORY") or "").strip()
    if not repo_ref or "/" not in repo_ref:
        return None, None
    owner, current_repo = repo_ref.split("/", 1)
    if owner.casefold() != username.casefold():
        return None, None
    if current_repo.casefold() != repo_names[0].casefold():
        return None, None

    try:
        result = subprocess.run(
            ["git", "log", "--numstat", "--pretty=tformat:"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None, None

    additions = 0
    deletions = 0
    has_data = False
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_text, del_text = parts[0].strip(), parts[1].strip()
        if not add_text.isdigit() or not del_text.isdigit():
            continue
        additions += int(add_text)
        deletions += int(del_text)
        has_data = True

    if not has_data:
        return None, None
    return additions, deletions


def fetch_local_git_stats(username: str) -> dict[str, Any] | None:
    """Best-effort local fallback when GitHub API calls fail entirely."""
    repo_ref = str(os.getenv("GITHUB_REPOSITORY") or "").strip()
    if not repo_ref or "/" not in repo_ref:
        return None
    owner, _repo_name = repo_ref.split("/", 1)
    if owner.casefold() != username.casefold():
        return None

    try:
        commits_result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        commits = int(commits_result.stdout.strip())
    except Exception:
        commits = None

    additions = 0
    deletions = 0
    has_churn = False
    try:
        churn_result = subprocess.run(
            ["git", "log", "--numstat", "--pretty=tformat:"],
            check=True,
            capture_output=True,
            text=True,
        )
        for line in churn_result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_text, del_text = parts[0].strip(), parts[1].strip()
            if not add_text.isdigit() or not del_text.isdigit():
                continue
            additions += int(add_text)
            deletions += int(del_text)
            has_churn = True
    except Exception:
        pass

    if commits is None and not has_churn:
        return None

    return {
        "stars": None,
        "repo_count": 1,
        "contributions": None,
        "commits": commits,
        "additions": additions if has_churn else None,
        "deletions": deletions if has_churn else None,
        "lines_of_code": None,
        "includes_private": False,
        "language_weights": {},
        "source": "local git fallback",
    }


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
    contribution_collection = contribution_user.get("contributionsCollection") or {}
    contributions = (
        contribution_collection.get("contributionCalendar", {})
        .get("totalContributions")
    )
    commits = contribution_collection.get("totalCommitContributions")

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
                        name
            stargazerCount
                        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    }
    """

    cursor: str | None = None
    stars = 0
    repo_count = 0
    includes_private = False
    total_language_bytes = 0
    repo_names: list[str] = []
    language_weights: defaultdict[str, int] = defaultdict(int)
    while True:
        result = post_graphql(token, repository_query, {"login": username, "cursor": cursor})
        user = result.get("user") or {}
        repositories = user.get("repositories") or {}
        for repository in repositories.get("nodes") or []:
            if not repository:
                continue
            repo_count += 1
            if repository.get("isPrivate"):
                includes_private = True
            repo_name = str(repository.get("name") or "").strip()
            if repo_name:
                repo_names.append(repo_name)
            stars += int(repository.get("stargazerCount") or 0)
            for edge in (repository.get("languages") or {}).get("edges") or []:
                node = edge.get("node") or {}
                name = str(node.get("name") or "").strip()
                size = int(edge.get("size") or 0)
                total_language_bytes += size
                if not name or name.casefold() in excluded_languages:
                    continue
                language_weights[name] += size
        page_info = repositories.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    additions, deletions = fetch_code_frequency_churn(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_churn_from_contributors(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_churn_from_compare_api(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_local_git_churn_if_single_repo(username, repo_names)
    if commits is None:
        commits = fetch_commit_totals_from_contributors(username, repo_names, token)
    if commits is None:
        commits = fetch_commit_totals_from_commits_api(username, repo_names, token)

    return {
        "stars": stars,
        "repo_count": repo_count,
        "contributions": int(contributions) if contributions is not None else None,
        "commits": int(commits) if commits is not None else None,
        "additions": additions,
        "deletions": deletions,
        "lines_of_code": int(round(total_language_bytes / 38.0)) if total_language_bytes > 0 else None,
        "includes_private": includes_private,
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
    repo_count = 0
    estimated_lines = 0
    repo_names: list[str] = []
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
            repo_name = str(repository.get("name") or "").strip()
            if repo_name:
                repo_names.append(repo_name)
            repo_count += 1
            stars += int(repository.get("stargazers_count") or 0)
            repo_size_kib = int(repository.get("size") or 0)
            if repo_size_kib > 0:
                estimated_lines += int(round((repo_size_kib * 1024) / 38.0))
            language = str(repository.get("language") or "").strip()
            if language and language.casefold() not in excluded_languages:
                language_weights[language] += max(1, int(repository.get("size") or 1))
        if len(repositories) < 100 or page >= 20:
            break
        page += 1

    additions, deletions = fetch_code_frequency_churn(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_churn_from_contributors(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_churn_from_compare_api(username, repo_names, token)
    if additions is None or deletions is None:
        additions, deletions = fetch_local_git_churn_if_single_repo(username, repo_names)
    commit_total = fetch_commit_totals_from_contributors(username, repo_names, token)
    if commit_total is None:
        commit_total = fetch_commit_totals_from_commits_api(username, repo_names, token)

    return {
        "stars": stars,
        "repo_count": repo_count,
        "contributions": None,
        "commits": commit_total,
        "additions": additions,
        "deletions": deletions,
        "lines_of_code": estimated_lines if estimated_lines > 0 else None,
        "includes_private": False,
        "language_weights": dict(language_weights),
        "source": "GitHub REST API",
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
            rest = fetch_rest_repository_stats(username, token, excluded_languages)
            rest["source"] = "GitHub REST fallback"
            return rest
    try:
        return fetch_rest_repository_stats(username, token, excluded_languages)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: repository stats unavailable; rendering partial data: {exc}", file=sys.stderr)
        local_stats = fetch_local_git_stats(username)
        if local_stats is not None:
            return local_stats
        return {
            "stars": None,
            "repo_count": None,
            "contributions": None,
            "commits": None,
            "additions": None,
            "deletions": None,
            "lines_of_code": None,
            "includes_private": False,
            "language_weights": {},
            "source": "live sync pending",
        }


def read_stats_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def merge_stats_with_cache(stats: Mapping[str, Any], cached: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(stats)
    cached_stats = cached.get("stats") if isinstance(cached.get("stats"), dict) else {}

    def numeric_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    for key in (
        "repo_count",
        "contributions",
        "commits",
        "additions",
        "deletions",
        "lines_of_code",
        "stars",
    ):
        cached_value = numeric_or_none(cached_stats.get(key))
        if merged.get(key) is None and cached_value is not None:
            merged[key] = cached_value
    if not merged.get("language_weights") and cached_stats.get("language_weights"):
        merged["language_weights"] = cached_stats.get("language_weights")
    if merged.get("source") in {None, "", "live sync pending"} and cached_stats.get("source"):
        merged["source"] = cached_stats.get("source")
    return merged


def write_stats_cache(path: Path, username: str, stats: Mapping[str, Any]) -> bool:
    def numeric_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "login": username,
        "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "stats": {
            "repo_count": numeric_or_none(stats.get("repo_count")),
            "contributions": numeric_or_none(stats.get("contributions")),
            "commits": numeric_or_none(stats.get("commits")),
            "additions": numeric_or_none(stats.get("additions")),
            "deletions": numeric_or_none(stats.get("deletions")),
            "lines_of_code": numeric_or_none(stats.get("lines_of_code")),
            "stars": numeric_or_none(stats.get("stars")),
            "includes_private": stats.get("includes_private"),
            "language_weights": stats.get("language_weights") or {},
            "source": stats.get("source"),
        },
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return write_text_if_changed(path, rendered)


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

    cleaned_display = re.sub(r"\s*·?\s*UTC[+-]\d{1,2}:\d{2}\b", "", display_name, flags=re.IGNORECASE)
    cleaned_display = re.sub(r"\s*·?\s*(EST|EDT)\b", "", cleaned_display, flags=re.IGNORECASE)
    cleaned_display = cleaned_display.strip(" ·")

    parts = [cleaned_display or getattr(local_zone, "key", "Eastern Time")]
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
            "stars": preview.get("stars") if preview.get("stars") is not None else None,
            "contributions": None,
            "commits": None,
            "additions": None,
            "deletions": None,
            "language_weights": {},
            "source": "preview",
        },
    )


def profile_rows(profile: Mapping[str, Any], config: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    hidden_fields = _hidden_personal_fields(config)
    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    local_zone = profile_timezone(config)
    precision = str(uptime_config.get("precision") or "days")
    timezone_display = str(
        uptime_config.get("timezone_display")
        or getattr(local_zone, "key", "Eastern Time")
    )
    timezone_value = format_timezone_value(local_zone, timezone_display)

    uptime_label = clean(uptime_config.get("label") or "human uptime", 22)
    source = str(uptime_config.get("source") or "github_account").strip().lower()
    if source == "custom":
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

    rows = [
        ("handle", f"@{profile.get('login', '')}", "accent2"),
        ("role", clean(profile_config.get("role"), 58), "text"),
        ("tagline", clean(profile_config.get("tagline"), 58), "text"),
        (uptime_label, uptime_value, "success"),
        ("timezone", clean(timezone_value, 58), "text"),
    ]
    additional_fields = profile_config.get("additional_fields") if isinstance(profile_config.get("additional_fields"), dict) else {}
    for key, value in additional_fields.items():
        label = clean(key, 22)
        rendered = clean(value, 58)
        if label and rendered and label.strip().lower() not in hidden_fields:
            rows.append((label, rendered, "text"))
    if profile_config.get("show_location", False):
        rows.append(("location", clean(profile.get("location"), 58), "text"))
    if profile_config.get("show_website", False):
        website = clean(profile.get("blog"), 58).removeprefix("https://").removeprefix("http://").rstrip("/")
        rows.append(("website", website, "accent2"))
    return [
        (label, value, color)
        for label, value, color in rows
        if value and label.strip().lower() not in hidden_fields
    ]


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
    rows = [
        ("repositories", format_number(stats.get("repo_count")) or "live sync pending", "text"),
        ("commits", format_number(stats.get("commits")) or "live sync pending", "text"),
        (
            "+ / -",
            (
                f"+{format_number(stats.get('additions'))} / -{format_number(stats.get('deletions'))}"
                if stats.get("additions") is not None and stats.get("deletions") is not None
                else "live sync pending"
            ),
            "text",
        ),
        ("lines of code", format_number(stats.get("lines_of_code")) or "live sync pending", "text"),
    ]
    if stats.get("includes_private"):
        rows.append(("scope", "public + private", "accent2"))
    else:
        rows.append(("scope", "public", "text"))
    return [(label, value, color) for label, value, color in rows if value]


def build_sections(
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    result = [("personal", profile_rows(profile, config))]
    result.extend(custom_sections(config))
    result.append(("public github stats", github_rows(profile, stats, config)))
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


def _next_refresh(local_zone: ZoneInfo, minute: int = 0, hour_step: int = 6) -> tuple[str, str]:
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
        preferred = []
        for item in configured:
            label = str(item).strip().lower()
            if not label:
                continue
            normalized = "personal" if label == "profile" else label
            if normalized == "github stats":
                normalized = "public github stats"
            preferred.append(normalized)
    else:
        preferred = ["personal", "top skills", "public github stats", "contact"]

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


def _hidden_personal_fields(config: Mapping[str, Any]) -> set[str]:
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    raw = profile_config.get("disabled_fields") if isinstance(profile_config.get("disabled_fields"), list) else []
    return {str(item).strip().lower() for item in raw if str(item).strip()}


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
    hidden_fields = _hidden_personal_fields(config)
    sections_config = config.get("sections") if isinstance(config.get("sections"), dict) else {}
    stack_config = sections_config.get("stack") if isinstance(sections_config.get("stack"), dict) else {}

    uptime_config = config.get("uptime") if isinstance(config.get("uptime"), dict) else {}
    local_zone = profile_timezone(config)
    timezone_display = str(uptime_config.get("timezone_display") or getattr(local_zone, "key", "Eastern Time"))
    discord_handle = clean(os.getenv("PROFILE_DISCORD") or profile_config.get("discord"), 40)
    personal_email = clean(os.getenv("PROFILE_EMAIL") or profile_config.get("email"), 60)

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

    profile_rows = [
        f"handle: @{_plain(profile.get('login') or 'aerybyte')}",
        f"role: {_plain(profile_config.get('role') or 'software engineer · product builder')}",
        f"{uptime_label}: {uptime_value}",
        f"timezone: {format_timezone_value(local_zone, timezone_display)}",
    ]
    additional_fields = profile_config.get("additional_fields") if isinstance(profile_config.get("additional_fields"), dict) else {}
    for key, value in additional_fields.items():
        label = _plain(key)
        rendered = _plain(value)
        if label and rendered and label.strip().lower() not in hidden_fields:
            profile_rows.append(f"{label}: {rendered}")
    profile_rows = [
        row for row in profile_rows if row.partition(":")[0].strip().lower() not in hidden_fields
    ]
    skills_rows = [
        f"languages: {_split_top_skills(stack_config.get('languages'), 4)}",
        f"frontend: {_split_top_skills(stack_config.get('frontend'), 3)}",
        f"backend: {_split_top_skills(stack_config.get('backend & data'), 3)}",
        f"devops: {_split_top_skills(stack_config.get('testing & devops'), 3)}",
        f"cloud: {_split_top_skills(stack_config.get('cloud/security/obs'), 3)}",
        f"analytics: {_split_top_skills(stack_config.get('analytics'), 3)}",
    ]
    skills_rows = [row for row in skills_rows if row.rsplit(":", 1)[-1].strip()]
    live_rows = [
        f"repositories: {format_number(stats.get('repo_count')) or 'live sync pending'}",
        f"commits: {format_number(stats.get('commits')) or 'live sync pending'}",
        (
            f"+ / -: +{format_number(stats.get('additions'))} / -{format_number(stats.get('deletions'))}"
            if stats.get("additions") is not None and stats.get("deletions") is not None
            else "+ / -: live sync pending"
        ),
        f"lines of code: {format_number(stats.get('lines_of_code')) or 'live sync pending'}",
    ]
    contact_rows: list[str] = []
    if discord_handle:
        contact_rows.append(f"discord: {discord_handle}")
    if personal_email:
        contact_rows.append(f"email: {personal_email}")
    custom_contact_fields = profile_config.get("contact_fields") if isinstance(profile_config.get("contact_fields"), dict) else {}
    for key, value in custom_contact_fields.items():
        label = _plain(key)
        rendered = _plain(value)
        if label and rendered:
            contact_rows.append(f"{label}: {rendered}")

    output = [
        ("personal", profile_rows),
        ("top skills", skills_rows),
        ("public github stats", live_rows),
    ]
    if contact_rows:
        output.append(("contact", contact_rows))
    return output


def _terminal_info_lines(
    profile: Mapping[str, Any],
    stats: Mapping[str, Any],
    config: Mapping[str, Any],
    panel_width: int = 68,
    label_width: int = 21,
) -> list[str]:
    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}

    lines: list[str] = []
    section_map: dict[str, list[tuple[str, str, str]]] = {
        name.lower(): rows for name, rows in build_sections(profile, stats, config)
    }

    compact_readme = bool(display.get("readme_compact", True))
    if compact_readme:
        section_map["personal"] = _pick_rows(
            section_map.get("personal", []),
            {"handle", "role", "human uptime", "timezone"},
        )
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
        section_map["github stats"] = _pick_rows(
            section_map.get("github stats", []),
            {"repositories", "commits", "+ / -", "lines of code", "scope"},
        )
        section_map["public github stats"] = _pick_rows(
            section_map.get("public github stats", section_map.get("github stats", [])),
            {"repositories", "commits", "+ / -", "lines of code", "scope"},
        )

    contact_rows: list[tuple[str, str, str]] = []
    discord_value = clean(os.getenv("PROFILE_DISCORD") or profile_config.get("discord"), 58)
    email_value = clean(os.getenv("PROFILE_EMAIL") or profile_config.get("email"), 58)
    if discord_value:
        contact_rows.append(("discord", discord_value, "accent2"))
    if email_value:
        contact_rows.append(("email", email_value, "warning"))
    custom_contact_fields = profile_config.get("contact_fields") if isinstance(profile_config.get("contact_fields"), dict) else {}
    for key, value in custom_contact_fields.items():
        label = clean(key, 22)
        rendered = clean(value, 58)
        if label and rendered:
            contact_rows.append((label, rendered, "text"))
    show_public_email = bool(display.get("show_public_email", False))
    blog = clean(profile.get("blog"), 50).removeprefix("https://").removeprefix("http://").rstrip("/")
    if blog:
        contact_rows.append(("website", blog, "text"))
    if show_public_email and profile.get("email"):
        contact_rows.append(("email", clean(profile.get("email"), 50), "warning"))
    if contact_rows:
        section_map["contact"] = contact_rows

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
    local_zone = profile_timezone(config)
    _next_eta, next_at = _next_refresh(local_zone)
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
    metadata_ini_lines.append(divider)
    metadata_ini_lines.append(f"next refresh at = {next_at}")
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
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("profile.template.yml"),
        help="Template config path (defaults to profile.template.yml)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument("--username")
    parser.add_argument("--avatar", type=Path, help="Optional local image for a preview or permanent override")
    parser.add_argument("--offline", action="store_true", help="Use bundled preview values and make no GitHub API calls")
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config, args.config)
    profile_config = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    configured_username = str(profile_config.get("github_username") or "").strip()
    username = str(
        args.username
        or os.getenv("GITHUB_USERNAME")
        or configured_username
        or os.getenv("GITHUB_REPOSITORY_OWNER")
        or "aerybyte"
    ).strip()
    token = os.getenv("GITHUB_TOKEN") or None
    is_scheduled_refresh = str(os.getenv("GITHUB_EVENT_NAME") or "").strip().lower() == "schedule"
    stats_cache_path = args.output_dir / "github-stats-cache.json"
    cached_stats = read_stats_cache(stats_cache_path)
    cached_login = str(cached_stats.get("login") or "").strip()
    if cached_login and cached_login.casefold() != username.casefold():
        cached_stats = {}
        stats_cache_path.unlink(missing_ok=True)
    if is_scheduled_refresh:
        cached_stats = {}
        stats_cache_path.unlink(missing_ok=True)

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

    # Force fresh stats on cron schedule runs by bypassing cache fallback.
    if not is_scheduled_refresh:
        stats = merge_stats_with_cache(stats, cached_stats)
    write_stats_cache(stats_cache_path, username, stats)

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
