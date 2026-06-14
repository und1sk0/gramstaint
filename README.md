# gramstaint

Enumerate your Instagram followers, score them for botness, and bulk-remove the ones you don't want.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)

```bash
uv sync
```

## Usage

### 1. Login

Authenticate once. Credentials are never written to disk — only a session token is saved to `.creds/token.json`.

```bash
uv run gramstaint.py login
```

### 2. Scrape followers

**Fast mode** — list only (no per-user API calls):

```bash
uv run gramstaint.py scrape
```

**Full mode** — includes follower/following/post counts per account:

```bash
uv run gramstaint.py scrape --full
```

Output goes to `followers.csv` by default. Use `--output` to change it:

```bash
uv run gramstaint.py scrape --full --output ~/Desktop/followers.csv
```

Cap the number of followers fetched (useful for testing):

```bash
uv run gramstaint.py scrape --limit 100
```

The following list is always fetched in full so mutual detection is accurate. Instagram controls the page size (~25 per request).

### 3. Review the CSV

Open `followers.csv` in a spreadsheet. Columns:

| Column | Description |
|--------|-------------|
| `user_id` | Numeric Instagram user ID |
| `username` | Instagram handle |
| `full_name` | Display name |
| `follower_count` | Their follower count (`--full` only) |
| `following_count` | How many they follow (`--full` only) |
| `media_count` | Number of posts (`--full` only) |
| `is_private` | Private account |
| `is_verified` | Verified account |
| `is_mutual` | You follow them back |
| `low_id` | Numeric ID suggests account predates ~2015 (older = more likely real) |
| `remove` | Set to `true` to remove this follower |

Bot signals to look for: `is_mutual=False`, `media_count=0`, `following_count` in the thousands, `low_id=False`.

### 4. Remove

Set `remove=true` on any rows you want gone, then:

```bash
uv run gramstaint.py remove followers.csv
```

### 5. Token (for raw API access)

Print the bearer token and example `curl` command for direct API calls:

```bash
uv run gramstaint.py token
```

## Persistence

| File | Contents |
|------|----------|
| `.creds/token.json` | Bearer token (gitignored) |
| `session.json` | Full instagrapi session state (gitignored) |
| `followers.csv` | Scrape output (gitignored) |

If your session expires, re-run `gramstaint login`.

## Notes

- Uses Instagram's private mobile API via [instagrapi](https://github.com/subzeroid/instagrapi). This violates Instagram's ToS — use at your own risk and keep request rates modest.
- Requests are randomized and rate-limited. Exponential backoff handles transient errors automatically.
- Account creation date is not exposed by the API. `low_id` (numeric ID below 2B) is used as a proxy for account age.
