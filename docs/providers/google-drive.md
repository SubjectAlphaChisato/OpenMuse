# Provider contract: Google Drive and Docs (read-only)

Status: implemented against a fake; live verification pending credentials. Checked against Google's documentation on 2026-09-28.

| Item | Contract |
|---|---|
| API | Drive API v3, `https://www.googleapis.com/drive/v3` |
| Accounts | Any Google account; uses the existing Google connection (same OAuth client, incremental consent) |
| Capability | `drive` (off / read only). No write tools yet. |
| Scope | `https://www.googleapis.com/auth/drive.readonly` — **restricted** (like Gmail): fine for owner test users in Testing mode; public verification needs Google's security assessment. The non-sensitive `drive.file` only covers files picked in the Google Picker, which this version does not implement. |
| `drive.search_files` (read) | `GET /files` with `q = (name contains 'x' or fullText contains 'x') and trashed = false`, quotes and backslashes escaped; `pageSize` ≤ 50; `fields=nextPageToken,files(id,name,mimeType,modifiedTime,owners(emailAddress),webViewLink,size)` |
| `drive.read_file` (read) | Google Docs → `GET /files/{id}/export?mimeType=text/markdown`; Sheets → `text/csv` (first sheet only); Slides → `text/plain`; plain text, Markdown, CSV, JSON → `GET /files/{id}?alt=media`. Export is capped at 10 MB by Google; results are cut to 50 KB and marked untrusted. Other types return `unsupported_format` with metadata. |
| Tokens | Same Google refresh, revoke, and reauth handling as Gmail (M3). |
| Errors | Same mapping as the Google adapter; reads retry on 429 and 5xx. |
| Tests | Fake Drive endpoints in `tests/fake_google.py`; demo files in the fixture provider. |

Sources: [Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth), [search syntax](https://developers.google.com/workspace/drive/api/guides/search-files), [files.export](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/export), [export formats](https://developers.google.com/workspace/drive/api/guides/ref-export-formats).
