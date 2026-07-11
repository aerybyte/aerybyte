# Dynamic GitHub Profile README Setup

This repo is now template-first so a nontechnical user can customize one file and run.

## Install dependencies

Before running locally, install Python and project dependencies:

1. Use Python 3.13 (or close to it).

1. Create and activate a virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
```

1. Install dependency packages from requirements.txt.

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

These dependencies are used by the renderer for GitHub API calls, YAML parsing,
image processing, and ASCII generation.

## What to edit

Edit only this file for personal values:

- profile.template.yml

The cron workflow reads this template directly.

## What is automatic vs manual

Manual values (you edit in profile.template.yml):

- profile.github_username (optional target account for live GitHub stats/avatar)
- personal metadata: role, tagline, discord, email
- extra personal metadata via profile.additional_fields (any key/value fields)
- optional removal of built-in personal fields via profile.disabled_fields
- extra contact metadata via profile.contact_fields
- top skills text
- timezone name and display label
- avatar settings, theme colors, section order

Automatic values (cron updates these during each run):

- GitHub stats (repositories, commits, + / - line churn, lines of code, scope)
- next refresh timestamp
- timezone suffix details (EST/EDT and UTC offset)
- avatar-ascii render and SVG assets

## Quick start (copy to your own profile repo)

1. Create or open your GitHub profile repository.

- Repository must be named exactly your username, for example: yourname/yourname.

1. Copy this project into that repository.

- Include hidden folders such as .github.

1. Edit profile.template.yml.

- Replace role, skills, discord, email, timezone, and optional start date.

1. Commit and push.

1. Run the workflow once manually.

- GitHub -> Actions -> Refresh profile card -> Run workflow.

1. Confirm generated files updated.

- README.md
- assets/avatar-ascii.txt
- assets/profile-terminal-dark.svg
- assets/profile-terminal-light.svg
- assets/github-stats-cache.json

## Cron schedule

Workflow file:

- .github/workflows/refresh-profile.yml

Schedule:

- `7 */6 * * *` in `America/New_York`
- Runs at 12:00 AM, 6:00 AM, 12:00 PM, and 6:00 PM Eastern

On scheduled runs, stats cache is invalidated and refreshed live, then cache is rewritten.

## Important fields in profile.template.yml

profile section:

- github_username: optional account to sync stats/avatar from (leave blank to use repo owner)
- role: main subtitle text
- tagline: optional
- discord and email: shown in contact section
- additional_fields: any extra personal fields to show in the personal section
- disabled_fields: hide any built-in or additional personal fields by label
- contact_fields: add extra contact key/value fields

sections.stack section:

- Feeds top skills block in README

uptime section:

- source: custom or github_account
- start_date: used when source is custom
- timezone: use IANA timezone (example: America/New_York)
- timezone_display: display prefix text

Timezone examples that work:

- America/New_York
- Europe/London
- Asia/Tokyo
- Australia/Sydney

display section:

- readme_section_order: metadata layout order
- avatar_path: local image override when non-empty
- ascii_shape: rounded_square, circle, or square

## Secrets and optional environment variables

Recommended GitHub Actions secret:

- PROFILE_START_DATE (optional)

Optional environment overrides supported by the script:

- GITHUB_USERNAME (highest priority for target account)
- PROFILE_TIMEZONE
- PROFILE_DISCORD
- PROFILE_EMAIL

Target account resolution priority:

- CLI --username
- GITHUB_USERNAME environment variable
- profile.github_username in profile.template.yml
- repository owner (default)

## Workflow permissions

Required in GitHub Actions:

- permissions: contents: write

This is needed so the bot can commit regenerated files.

## Config validation (plain-English errors)

Before rendering, the script validates profile.template.yml and fails early with clear messages if something is wrong.

Examples of what is validated:

- timezone format (must be a valid IANA timezone)
- allowed values for uptime.source and uptime.precision
- display.ascii_shape value
- structure for profile.additional_fields and section order lists

If validation fails, fix the listed items and run the command again.

## Recipes (safe metadata edits)

Use these quick examples in profile.template.yml.

Add a new personal field:

```yaml
profile:
  additional_fields:
    current_focus: "building developer tools"
```

Hide a built-in field (for example timezone):

```yaml
profile:
  disabled_fields:
    - timezone
```

Hide pronouns only:

```yaml
profile:
  disabled_fields:
    - pronouns
```

Add extra contact fields:

```yaml
profile:
  contact_fields:
    linkedin: "linkedin.com/in/yourname"
    website: "yourdomain.com"
```

Remove contact section entirely:

```yaml
profile:
  discord: ""
  email: ""
  contact_fields: {}
```

After edits, run:

```bash
python scripts/build_profile.py --config profile.template.yml
```

## Local preview

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/build_profile.py --config profile.template.yml --offline
```

Live local run:

```bash
GITHUB_USERNAME=yourname python scripts/build_profile.py --config profile.template.yml
```

profile.template.yml is the source of truth for setup and customization.
