# Install the cron-powered profile in `aerybyte/aerybyte`

This package is personalized for **@aerybyte**. It includes a terminal-style
profile card, birthday-based human uptime, live public GitHub statistics, and an
ASCII portrait rebuilt from the current GitHub profile picture.

## Already configured

- birth date: `2004-07-13`
- uptime label: `human uptime`
- timezone: `America/New_York`
- timezone display: `Eastern Time · EST/EDT · UTC offset`
- profile-picture source: the current `@aerybyte` GitHub avatar
- automatic cadence: every six hours at `:17` Eastern Time
- themes: automatic dark and light SVGs

The exact date is stored in `profile.yml`, so it will be public in the profile
repository. To keep the raw date out of the source, blank `uptime.start_date`
and create an Actions secret named `PROFILE_START_DATE` with `2004-07-13`.
The rendered age still makes the date reasonably inferable.

## Cron schedule

The scheduled workflow is `.github/workflows/refresh-profile.yml`:

```yaml
schedule:
  - cron: "17 */6 * * *"
    timezone: "America/New_York"
```

It runs at **12:17 AM, 6:17 AM, 12:17 PM, and 6:17 PM Eastern Time**. The IANA
zone follows daylight-saving time, so GitHub schedules it in EST during winter
and EDT during summer. The midnight run advances human uptime on the correct
local calendar date.

The generator compares the rendered content while ignoring only the volatile
footer timestamp. It commits when the avatar, uptime, statistics, copy, timezone,
or theme actually changed; unchanged checks create no noise commits. The footer
therefore records the last meaningful card update, not merely the last cron check.

## Dynamic profile-picture ASCII

Every run performs this sequence:

1. Read the repository owner (`aerybyte`).
2. Request the public GitHub user record.
3. Download the current `avatar_url` without forwarding a personal token.
4. Convert the image into adaptive color ASCII.
5. Update `assets/avatar-ascii.txt` and both SVG profile cards.
6. Cache the last successful image as `assets/avatar.png`.
7. Commit changed assets with `github-actions[bot]`.

The avatar cache means a temporary image/CDN failure keeps the last good portrait
instead of replacing it with a generic placeholder. Changing the GitHub profile
picture is reflected on the next cron run, or immediately after a manual run.

## Install

1. Open or create the public profile repository `aerybyte/aerybyte`.
2. Copy **all** files from this folder into the repository root, including the
   hidden `.github` directory.
3. Commit and push.
4. Open **Actions → Refresh profile card → Run workflow** for the first live
   refresh.
5. Confirm that the bot updates:
   - `assets/avatar.png`
   - `assets/avatar-ascii.txt`
   - `assets/profile-terminal-dark.svg`
   - `assets/profile-terminal-light.svg`

The bundled assets are already usable as an initial preview. The first workflow
run replaces preview values with current public repository, star, follower,
contribution, and language data.

## Permissions

The workflow requests only:

```yaml
permissions:
  contents: write
```

That permission is needed to commit regenerated assets. If a run renders files
but cannot push, check **Settings → Actions → General → Workflow permissions**,
plus any branch-protection or organization rules.

No personal access token is required. The workflow uses GitHub's built-in,
short-lived `GITHUB_TOKEN`.

## Customize

Edit `profile.yml`. Useful settings include:

```yaml
uptime:
  start_date: "2004-07-13"
  timezone: "America/New_York"
  timezone_display: "Eastern Time"

display:
  avatar_path: ""                    # blank means current GitHub profile picture
  avatar_cache_path: "assets/avatar.png"
  ascii_width: 54
  avatar_zoom: 1.05
  ascii_shape: "rounded_square"
```

To use a permanent non-GitHub image, add it to the repository and set
`display.avatar_path` to that path. When `avatar_path` is nonblank, it becomes a
manual override and the GitHub avatar is no longer downloaded.

## Local preview

```bash
python -m venv .venv
source .venv/bin/activate      # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/build_profile.py --offline
```

For a live local refresh:

```bash
GITHUB_USERNAME=aerybyte python scripts/build_profile.py
```

Optional overrides:

```bash
PROFILE_START_DATE=2004-07-13 \
PROFILE_TIMEZONE=America/New_York \
GITHUB_USERNAME=aerybyte \
python scripts/build_profile.py
```
