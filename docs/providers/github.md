# Provider contract: GitHub (read-only)

Status: implemented against a fake; live verification pending credentials. Checked against GitHub's documentation on 2026-09-28.

| Item | Contract |
|---|---|
| API | `https://api.github.com`, headers `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` |
| App type | A **GitHub App** (not an OAuth App). OAuth App scopes are coarse: reading private code needs `repo`, which also grants write. A GitHub App's user token has only the fine-grained permissions both the app and the user have, on repositories where the app is installed. Configure the app with read-only **Contents**, **Issues**, **Pull requests**, and **Metadata**. |
| Accounts | Any GitHub user who installs the app on the repositories they choose. Identity = numeric user id; label = login. |
| Capability | `github` (off / read only). No write tools yet. |
| OAuth | Authorize `https://github.com/login/oauth/authorize` with `client_id`, `redirect_uri`, `state`, `code_challenge` (S256 only). Token `POST https://github.com/login/oauth/access_token` with `client_id`, `client_secret`, `code`, `redirect_uri`, `code_verifier`, header `Accept: application/json`. |
| Tokens | With expiring tokens enabled (default): access tokens last 8 hours and refresh tokens 6 months; refresh with `grant_type=refresh_token`. If expiry is disabled, tokens do not expire and are refreshed never. Revoke: `DELETE https://api.github.com/applications/{client_id}/token` with `{"access_token": ...}`. The documentation's example omits authentication; this adapter sends HTTP Basic with the client ID and secret, the documented pattern for `/applications/{client_id}/...` endpoints. **Verify live.** |
| `github.list_repos` (read) | `GET /user/repos?per_page=50&page=N&sort=updated` |
| `github.search_issues` (read) | `GET /search/issues?q=...&per_page≤50` (issues and pull requests) |
| `github.get_issue` (read) | `GET /repos/{owner}/{repo}/issues/{number}` plus up to 30 comments from `.../comments` |
| `github.read_file` (read) | `GET /repos/{owner}/{repo}/contents/{path}?ref=` — files only, base64-decoded, text only, 50 KB cap, untrusted |
| Rate limits | 5,000 requests an hour per user token; search 30 a minute. 403 or 429 with `Retry-After` or `x-ratelimit-remaining: 0` maps to `rate_limited`. |
| Tests | `tests/fake_providers.py`; demo repositories in the fixture provider. |

Sources: [user access tokens for GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app), [authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps), [OAuth application endpoints](https://docs.github.com/en/rest/apps/oauth-applications).
