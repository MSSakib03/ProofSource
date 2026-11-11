# ProofSource security audit (November 2026)

Scope: the whole repository at commit `625df0f` — Flask backend (`app.py`, `models.py`,
`wiki_engine.py`, `backup_engine.py`), the single-page frontend (`templates/index.html`), the
deployment notes and the Toolforge hosting model. Every finding below is fixed in this change and
covered by a regression test in `tests/test_security.py` unless noted.

Severity: **Critical** = account/token compromise or full bypass of the approval model by any
logged-in user; **High** = stored XSS or privilege escalation by a contest admin, or a remote DoS;
**Medium** = needs an unusual position or has limited impact; **Low** = hardening.

## Summary

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | Critical | `wiki_domain` was never validated: SSRF, OAuth token theft, approval bypass | Wikisource allowlist at input **and** in `get_api_url()` |
| 2 | High | Backup restore let a contest admin forge data and self-approve | HMAC-signed backups; approval/owner/wiki/slug never restored |
| 3 | High | Stored XSS via the central contest "event page" link (`javascript:`) | `http(s)` only, server and client side |
| 4 | High | Stored XSS via `wiki_domain` inside the announcement HTML | allowlist + escaping + DOMPurify |
| 5 | High | ReDoS in the page-quality parser freezes workers during sync | linear-time parser (proved equivalent by fuzzing) |
| 6 | High | No CSRF protection on any state-changing endpoint | per-session token header + Origin/Sec-Fetch-Site checks |
| 7 | Medium | Reflected XSS in the OAuth callback error page | plain-text, fixed messages |
| 8 | Medium | OAuth tokens stored in plaintext; DB file and snapshots readable on shared NFS | Fernet encryption at rest, 0600/0700 files, tokens never in snapshots |
| 9 | Medium | Tool-admin check was case-insensitive (`ms sakib`) | exact canonical-username match, configurable |
| 10 | Medium | Anonymous users could force syncs and reserve pages without limit (DoS) | login required, per-user rate limits, minimum sync interval |
| 11 | Medium | No security headers (clickjacking of the review panel, no CSP) | CSP with nonce, frame-ancestors, nosniff, HSTS, no-store |
| 12 | Medium | Third-party CDNs (Tailwind, cdnjs, Google Fonts) leaked every visitor's IP | all assets self-hosted; system fonts |
| 13 | Low | Wiki-rendered HTML injected with `v-html` unsanitised | DOMPurify with a positioning-stripping hook |
| 14 | Low | A reviewer could take over or decide a page locked by another reviewer | lock respected in `next-page` and `submit` (409) |
| 15 | Low | Login records never expired server-side | 30-day server-side expiry + purge |
| 16 | Low | Unvalidated numbers/enums caused 500s; unbounded text and page ranges | `to_int`, enum checks, length caps, page-range caps |
| 17 | Low | Internal exception text returned to users | generic messages, details logged |
| 18 | Low | No PKCE in the OAuth flow; non-constant-time `state` compare | PKCE S256, `secrets.compare_digest` |
| 19 | Low | Server-side backup copies grew without bound | per-contest retention (`PROOFSOURCE_JSON_BACKUPS_KEEP`) |

## Details

### 1. Unvalidated `wiki_domain` (Critical)
`POST /api/contests` and `POST /api/contests/<id>/config` stored any string as the contest's wiki,
and every wiki call built `https://{domain}/w/api.php` from it. Any logged-in user could:
- point a contest at a server they control; `is_user_wiki_admin()` then asked *that* server whether
  the creator is a sysop → the contest was **auto-approved**;
- add reviewers; when a reviewer accepted a page with "validate on Wikisource", the server sent the
  reviewer's **OAuth bearer token** to the attacker's host (`edit_wikisource_live`), which could then
  edit any Wikimedia wiki as that reviewer;
- reach internal hosts (`localhost:…`, metadata IPs) from Toolforge (SSRF);
- serve arbitrary `rendered_html` into the review panel (`v-html`).

Fix: `wiki_engine.normalize_wiki_domain()` (`^([a-z0-9-]+\.)?wikisource\.org$`) is enforced on create,
config change and central projects, and `get_api_url()` refuses anything else, so no code path can
contact another host. Changing a standalone contest's wiki sends it back to pending approval unless
the user is an admin of the new wiki.

### 2. Forged backups (High)
The backup "checksum" was a plain SHA-256 of the data, which anyone can recompute. A contest admin
could edit a downloaded backup — set `approval_status: approved`, change `created_by`, `wiki_domain`
(see #1), invent accepted pages with bonus points, or attach the contest to another central contest
via `hub_slug` — and restore it. Fix: backups carry an HMAC-SHA256 signature made with the server
secret; contest admins can only restore signed backups of the same contest (the tool admin can still
restore legacy/unsigned files). Approval, creator, wiki, slug and hub are never restored, and page
rows are type-checked.

### 3–4. Stored XSS (High)
- `Hub.link` was rendered as `<a :href>`; `javascript:alert(document.cookie)` ran in every visitor's
  session. Now only `http(s)` URLs are accepted, stored values are re-checked on output, and the
  frontend only binds links that pass `safeUrl()`.
- `announcementHtml` concatenated the unescaped `wiki_domain` into an `href` attribute
  (`x" onmouseover="…`). The domain is now validated, escaped, and the result passes through DOMPurify.

### 5. ReDoS in `parse_proofread_page` (High)
The regex `^<noinclude>…(.*?)</noinclude>(.*?)<noinclude>(.*?)</noinclude>$` (DOTALL) backtracks
cubically: 400 repetitions of `</noinclude><noinclude>` took 1.9 s, a 2 MB page would take hours.
Any wiki editor could save such a page in a contest book and freeze every worker that syncs it. The
new parser is linear (200 000 tags in 25 ms) and was checked against the old regex on 200 000
random inputs with identical results.

### 6. CSRF (High)
No endpoint checked request origin. `SameSite=Lax` helps, but all `*.toolforge.org` tools share a
registrable domain, the multipart restore endpoint accepted simple form posts, and `DELETE` and
`/logout` relied on browser behaviour only. Every non-GET request now needs the session's
`X-CSRF-Token`; cross-site requests (`Sec-Fetch-Site: cross-site`, foreign `Origin`) are refused.

### 7. Reflected XSS in `/auth/callback` (Medium)
`?error=<script>…` was echoed into a `text/html` response. Error pages are now `text/plain` with
fixed wording; exception details go to the log only.

### 8. Tokens at rest (Medium)
Tool home directories on Toolforge are readable by other tools by default. The SQLite file (and every
snapshot copy of it) held working OAuth access and refresh tokens. Tokens are now encrypted with
Fernet (key derived from `SECRET_KEY`), snapshots never contain the `oauth_logins` table, the SQLite
file, backup directory and files are created private, and the secret-key file is created `0600`
atomically. Moving to ToolsDB (below) removes the shared-file exposure entirely.

### 9. Tool-admin identity (Medium)
`SUPER_ADMINS = {"ms sakib"}` was compared after lower-casing. MediaWiki usernames are
case-sensitive after the first letter, so `Ms sakib` or `MS SAKIB` would have matched. Tool admins
are now matched by exact canonical name and configurable via `PROOFSOURCE_SUPER_ADMINS`.

### 10. Resource abuse (Medium)
Anonymous clients could `POST /sync {"force": true}` repeatedly (each sync walks wiki histories) and
`/reserve-page` as "Anonymous" (each call fetched the content of every page of a book and locked a
page for 20 minutes). Forcing a sync and reserving pages now need a login, are rate-limited per user,
and syncs keep a minimum interval.

### 11–12. Headers and third-party assets (Medium)
Added CSP (per-request nonce; `'unsafe-eval'` remains because Vue compiles the in-page template),
`frame-ancestors 'none'`/`X-Frame-Options` (the review panel performs wiki edits, so it must not be
framed), `nosniff`, HSTS, `Referrer-Policy`, `Permissions-Policy`, and `Cache-Control: no-store` for
the API. The page loaded Tailwind's runtime compiler, Vue and Google Fonts from third parties on
every visit, which the Wikimedia Cloud Services terms ask tools not to do; all assets are now served
by the tool and the UI uses system fonts.

### 13–19. Hardening (Low)
See the summary table; each item has a regression test where behaviour is observable.

## Database: SQLite → ToolsDB (MariaDB)

SQLite is not a good fit for this deployment:
- the tool home (and so `instance/proofsource_v5.db`) is on NFS, where SQLite's file locking is
  unreliable with several uWSGI workers — "database is locked" errors under load and a real risk of
  corruption;
- the file sits in a directory other tools can read (see #8);
- WMCS recommends ToolsDB for tools that need a relational database.

The app now prefers ToolsDB (`TOOLSDB_DATABASE`, credentials from Toolforge envvars or
`~/replica.my.cnf`), with small connection pools, `utf8mb4` and a binary collation so behaviour
matches SQLite exactly. `manage.py copy-db` migrates an existing SQLite database (skipping orphan
rows SQLite never rejected), and snapshots work on MariaDB as gzip'd JSON dumps. The full test suite
passes on both SQLite and MariaDB 10.11. SQLite remains the default for local development and tests.

## Not changed (accepted risks / follow-ups)
- `'unsafe-eval'` in CSP is required by Vue's runtime template compiler; removing it needs a build
  step that precompiles the template.
- Contest/jury lists still match usernames case-insensitively (existing contests rely on it). The
  only effect is that a deliberately created case-variant account could be treated as a listed user;
  AntiSpoof normally prevents such accounts.
- Adding a new project to an already-approved central contest keeps it approved (the approval model
  is "any sysop of any included wiki"); maintainers may want a stricter rule.
- Rate limits are per uWSGI worker (in-process); good enough to stop scripts, not a global quota.
