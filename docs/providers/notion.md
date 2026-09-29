# Provider contract: Notion (read-only)

Status: implemented against a fake; live verification pending credentials. Checked against Notion's documentation on 2026-09-28.

| Item | Contract |
|---|---|
| API | `https://api.notion.com/v1`, header `Notion-Version: 2026-03-11` on every request |
| Accounts | Any Notion workspace member, through a **public integration** (OAuth). One connection per workspace. |
| Capability | `notion` (off / read only). The integration should request only "Read content" in Notion's settings. No write tools yet. |
| What it can see | Only the pages and databases the owner selects in Notion's page picker during sign-in. Notion has no OAuth scopes. |
| OAuth | Authorize `https://api.notion.com/v1/oauth/authorize?client_id&redirect_uri&response_type=code&owner=user&state`. Token `POST https://api.notion.com/v1/oauth/token`, HTTP Basic client auth, JSON body. PKCE is not documented, so it is not sent; the single-use, session-bound state still protects the callback. |
| Tokens | Response includes `access_token`, `refresh_token`, `bot_id`, `workspace_id`, `workspace_name`, `owner`. Refresh: same endpoint, `grant_type=refresh_token`. Revoke: `POST https://api.notion.com/v1/oauth/revoke`, Basic auth, `{"token": ...}`. Account identity = `workspace_id`; label = workspace name. |
| `notion.search` (read) | `POST /search` with `query`, optional `filter {property: object, value: page|data_source}`, `page_size` ≤ 50, `start_cursor` |
| `notion.read_page` (read) | Page title from `GET /pages/{id}`, text from `GET /blocks/{id}/children` read recursively (children fetched per `has_children`), bounded to depth 3 and 400 blocks; `rich_text[].plain_text` joined; 50 KB cap; untrusted |
| `notion.query_data_source` (read) | `POST /data_sources/{id}/query` with `page_size` ≤ 50 and `start_cursor`; each row's properties flattened to plain text |
| Rate limits | Averages about three requests a second per integration; 429 with `Retry-After` is retried for reads. |
| Tests | `tests/fake_providers.py` (OAuth, refresh, revoke, search, blocks, data sources); demo pages in the fixture provider. |

Sources: [authorization](https://developers.notion.com/docs/authorization), [versioning](https://developers.notion.com/reference/versioning), [search](https://developers.notion.com/reference/post-search), [block children](https://developers.notion.com/reference/get-block-children), [query a data source](https://developers.notion.com/reference/query-a-data-source), [revoke](https://developers.notion.com/reference/revoke-token).
