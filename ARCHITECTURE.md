<div align="center">

<img src="static/favicon.svg" alt="ProofSource logo" width="80" height="80" />

# ProofSource — Architecture

**System design, data model and internals**

![Python](https://img.shields.io/badge/Python-3.11-294584)
![Flask](https://img.shields.io/badge/Flask-3.0-236798)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.x-006699)
![Vue](https://img.shields.io/badge/Vue-3-4D8AC0)
![Tests](https://img.shields.io/badge/tests-44%20passing-2e7d32)

</div>

ProofSource is a web tool for running **proofreading contests on Wikisource** (any language). It tracks which pages were proofread during a contest, lets reviewers (juries) accept or reject them, keeps a leaderboard, and supports multi-wiki **central contests** with one combined leaderboard.

- **Live:** `https://proofsource.toolforge.org` · **Staging:** `https://proofsource-test.toolforge.org`
- **Hosting:** Wikimedia Toolforge (Kubernetes webservice, `python3.11`, uWSGI)
- **Stack:** Python 3.11 · Flask 3.0 · Flask-SQLAlchemy 3.1 (SQLAlchemy 2.x) · SQLite (MySQL/ToolsDB compatible) · Vue 3 (CDN, single HTML template) · Tailwind CSS (CDN)
- **Login:** MediaWiki **OAuth 2.0** (meta.wikimedia.org)
- **UI languages:** English (default) and Bangla, through one central translation table

> 📖 For end users, see [README.md](README.md).

## Table of contents

1. [Repository / file layout](#1-repository--file-layout)
2. [Configuration](#2-configuration-environment-variables)
3. [Authentication](#3-authentication-oauth-20)
4. [Data model](#4-data-model-modelspy)
5. [Core logic](#5-core-logic)
6. [HTTP routes](#6-http-routes-summary)
7. [Frontend](#7-frontend-templatesindexhtml)
8. [Testing](#8-testing)
9. [Deployment](#9-deployment)

## System overview

```mermaid
flowchart LR
    U[Browser<br/>Vue 3 SPA] -- JSON API --> F[Flask app<br/>app.py]
    F --> DB[(SQLite / ToolsDB<br/>models.py)]
    F --> W[wiki_engine.py]
    W -- MediaWiki API --> WS[(Wikisource wikis)]
    F --> B[backup_engine.py]
    B --> BK[(~/proofsource_backups)]
    U -- OAuth 2.0 --> M[meta.wikimedia.org]
    M -- callback --> F
```

```mermaid
flowchart TD
    H[Central contest<br/>rules: dates, points, policies] --> C1[Project contest bn]
    H --> C2[Project contest hi]
    H --> C3[Project contest as]
    C1 --> B1[Index books] --> P1[Contest pages]
    P1 --> R[Review / leaderboard]
```

---

## 1. Repository / file layout

This repository maps directly to `~/www/python/src` on Toolforge:

```
~/www/python/
├── uwsgi.ini                 # Copy from deploy/uwsgi.ini (buffer-size = 65535 for OAuth 2.0 callbacks)
├── venv/                     # Python virtualenv (created once inside `webservice python3.11 shell`)
└── src/                      # Git repository root
    ├── app.py                # Flask app: routes, permissions, sync, statistics, central contests
    ├── models.py             # SQLAlchemy models + backward-compatible migration
    ├── wiki_engine.py        # All Wikisource/MediaWiki API access + proofread eligibility rules
    ├── backup_engine.py      # JSON/CSV export, validated restore, SQLite snapshots
    ├── requirements.txt      # Python dependencies
    ├── deploy/
    │   └── uwsgi.ini         # Toolforge uWSGI config (copied to ~/www/python/uwsgi.ini)
    ├── templates/index.html  # Whole frontend: Vue 3 app + i18n table + styles
    ├── static/favicon.svg    # Favicon (adapts to light/dark browser theme)
    ├── tests/test_app.py     # Automated tests (network mocked)
    └── instance/proofsource_v5.db   # SQLite database (auto-created at runtime, gitignored)
~/proofsource_backups/        # Snapshots and JSON backups (outside the app folder on purpose)
```

---

## 2. Configuration (environment variables)

Set with `toolforge envvars create NAME VALUE`. Secrets are never stored in code.

| Variable | Purpose | Default |
|---|---|---|
| `OAUTH2_CLIENT_ID` | OAuth 2.0 client key (meta.wikimedia.org consumer) | — required |
| `OAUTH2_CLIENT_SECRET` | OAuth 2.0 client secret | — required |
| `OAUTH2_REDIRECT_URI` | Must equal the consumer's registered callback | `https://proofsource.toolforge.org/auth/callback` |
| `OAUTH2_BASE` | OAuth endpoint base | `https://meta.wikimedia.org/w/rest.php/oauth2` |
| `SECRET_KEY` | Flask session signing key | auto-generated and stored in `~/.proofsource_secret_key` |
| `DATABASE_URL` | SQLAlchemy URL (e.g. ToolsDB MySQL) | `sqlite:///proofsource_v5.db` |
| `PROOFSOURCE_BACKUP_DIR` | Where backups/snapshots go | `~/proofsource_backups` |
| `PROOFSOURCE_SNAPSHOT_HOURS` | Interval between automatic DB snapshots | `12` |
| `PROOFSOURCE_SNAPSHOT_KEEP` | Number of snapshots kept | `21` |

`SUPER_ADMINS` (tool admins) is a constant in `wiki_engine.py`.

---

## 3. Authentication (OAuth 2.0)

1. `/login` → redirect to `…/oauth2/authorize` with `client_id`, `redirect_uri`, random `state`.
2. User approves on Meta → `/auth/callback?code=…&state=…` (`/oauth-callback` is an alias).
3. Server checks `state`, exchanges `code` at `…/oauth2/access_token`, reads the username from `…/oauth2/resource/profile` (fallback: `api.php?meta=userinfo`).
4. Tokens are stored **server-side** in `oauth_logins` (they are too long for a cookie); the cookie only holds a random `login_id`.
5. Access tokens are refreshed automatically with the refresh token when expired.
6. Wiki edits use `Authorization: Bearer <token>` against each wiki's `api.php`.

Toolforge requirement: `~/www/python/uwsgi.ini` with `buffer-size = 65535`; otherwise uWSGI drops the long callback request ("upstream connect error … connection termination").

---

## 4. Data model (`models.py`)

| Table | Model | Key fields |
|---|---|---|
| `contests` | `Contest` | slug, title, wiki_domain, start/end (UTC), proofread/validate/max_bonus points, jury_mode (`in_app_validate` / `accept_reject`), reject_action (`hold_batch` / `instant_revert`), admins, juries, excluded_users, approval_status, **review_enabled**, **bulk_review_enabled**, **review_assignment** (`open`/`equal`), **allow_custom_points**, announcement_en/bn, last_backup_at, sync_started_at, hub_id, delete_requested_by/at/reason |
| `index_books` | `IndexBook` | contest_id, title, base_name, total_pages, custom_points, excluded_pages, is_active, optional own start/end window |
| `contest_pages` | `ContestPage` | contest_id, book_id, page_number, page_title, wiki_level, proofreader, validator, proofread_rev_id/time, review_status (`unproofread`/`pending_review`/`accepted`/`needs_fix`/`rejected`), reviewed_by/at, base/bonus points, jury_note, jury & user locks, **last_rev_id** (incremental sync), **assigned_to** (equal review) |
| `hubs` | `Hub` = **Central contest** | slug, title, description, link, start/end, approval_status, proofread/validate points, custom_points_policy / bonus_policy / review_policy (`yes`/`no`/`project`), admins, delete request fields |
| `audit_logs` | `AuditLog` | append-only (ORM blocks update/delete); contest_id (no FK, survives deletion), username, action, target, old/new value, details |
| `slug_aliases` | `SlugAlias` | old slugs keep working after a slug change (301 redirect) |
| `oauth_logins` | `OAuthLogin` | server-side OAuth tokens |

**Migration** (`migrate_schema()`): runs on the first request. Creates missing tables, then `ALTER TABLE … ADD COLUMN` for any missing column and creates missing indexes. Never drops or rewrites data, so old databases upgrade in place.

Indexes for scale: `(contest_id, review_status)`, `(book_id, page_number)`, `(contest_id, proofreader)`, plus single-column indexes.

---

## 5. Core logic

### 5.1 Proofread eligibility (`wiki_engine.analyze_page_history`)
Uses the page's **full revision history** (oldest → newest, paged up to 500 revisions):

1. Only the **first time ever** a page reaches Proofread (level ≥ 3) can count. Pages proofread/validated before the contest never count.
2. That first upgrade must be **inside the contest window** (book window if set, else contest window).
3. The person who made the upgrade must have **changed the page text themselves**, either in that edit or in any earlier edit of theirs (creating the page counts). Only flipping the status on text written entirely by others does not count.
4. Credit goes to that first upgrader. Later backward/forward status changes (e.g. Validated → Proofread) never move the credit.
5. The page must still be Proofread/Validated now.
6. Validation credit: first upgrade 3 → 4 inside the window.

### 5.2 Sync (`sync_book_with_wikisource`, `sync_contest_books`)
- Lists pages with `generator=allpages` in the Page namespace (prefix `Book/`).
- **Incremental**: if a page's latest revision id equals the stored `last_rev_id`, it is skipped (no history fetch).
- **Never destructive on failure**: the fetch returns `(results, complete)`. Pages missing from an incomplete listing are not reset; a network error on one page skips that page.
- Bots (wiki `bot` group) and excluded users are filtered at sync time.
- A per-contest lock (`sync_started_at`, 10 min) prevents two workers syncing the same contest.
- Changing the contest/book window clears `last_rev_id`, so the next sync re-checks every page.
- `GET /api/contests/<id>` returns **cached** data instantly; the frontend calls `POST /sync` in the background when data is stale (> 5 min).

### 5.3 Points & leaderboard
- Book points = custom points (if allowed) else the contest default.
- Review mode: points count when `accepted`. Non-review mode: `pending_review` also counts.
- Validation points only when `validate_points > 0`; juries get them only if `jury_gets_points`.
- All statistics are **SQL aggregates** (GROUP BY), never loading all rows; tested to 100 000 pages (dashboard ≈ 0.3 s).

### 5.4 Review
- Queue: excludes the reviewer's own pages (backend-enforced) and pages locked by another reviewer (10-minute lock).
- Decisions: `accepted`, `needs_fix`, `rejected`, `pending_review`. Duplicate submissions are ignored.
- `in_app_validate`: accepting also edits the page to Validated on Wikisource (Bearer token). `instant_revert`: rejecting sets the page back to Not Proofread.
- **Equal distribution** (`review_assignment = equal`, `assign_pending`): target = ceil(total reviewable / reviewers). Already-reviewed pages count toward each reviewer's share. Pending pages are assigned in book/page order, keeping consecutive runs per reviewer. Runs after every sync and re-balances when the mode, reviewer list or review flag changes. Reviewers see only their pages; admins can switch to "All".
- **Bulk review** (if enabled): select pages in the queue (Shift+click range); admins can select all pages matching a filter (e.g. one participant). Processed in chunks; each page commits separately, so partial failure keeps successes.
- Instant next page: the next page is prefetched (`peek=1`) and shown immediately; `/jury/lock` locks it; the UI rolls back if the server refuses.

### 5.5 Central contests (central control panel)
- A `Hub` owns shared rules: dates, points per page, validation points, and three policies (`custom_points_policy`, `bonus_policy`, `review_policy`: `yes` / `no` / `project`).
- On create/config, `sync_children` creates **one child Contest per selected project** with its project admins. `apply_central_rules` forces dates, points and the policies onto each child; `locked_fields` tells the UI which fields a child admin cannot change.
- Created by the tool admin or a sysop of any selected wiki → approved immediately; otherwise pending until one of them approves. Approval cascades to children.
- A project with recorded pages cannot be removed from a central contest.
- Central page: combined leaderboard (same username across wikis = one person), included projects, statistics, WikiTable result generator.

### 5.6 Permissions
| Role | Determined by | Can |
|---|---|---|
| Tool admin (super admin) | `SUPER_ADMINS` | everything; deletes contests on request; DB snapshots |
| Wiki sysop | wiki `sysop/bureaucrat/steward` group | auto-approved contests on that wiki; approves pending contests |
| Contest admin | creator or `admins` list | configure, books, backup/restore, audit log, request deletion |
| Reviewer (jury) | `juries` list | review queue, bulk review (if enabled) |
| Participant | logged-in user | reserve a random page, view own pages |

Every permission is checked in the backend; the UI only hides buttons.

### 5.7 Data safety
- JSON backup per contest (with SHA-256 checksum and record counts). A server-side copy is saved on every download.
- Restore: validated first (format, version, checksum, counts, same contest), then safety JSON + DB snapshot, then a single transaction.
- Automatic SQLite snapshots via the online backup API, integrity-checked, every 12 h, last 21 kept, stored outside the app folder; file lock so only one worker snapshots.
- Backup reminder: after 7 days without a backup, plus a special reminder if never backed up after a month.
- Audit log is append-only and visible only to contest admins.
- Deletion: a contest admin requests it; the tool admin executes it. A JSON backup is saved first and the audit entry remains.

---

## 6. HTTP routes (summary)

Pages (all render `index.html` with `window.PS_INITIAL` for instant direct links): `/`, `/contest/<lang>/<slug>`, `/contest/<lang>/<slug>/me`, `/contest/<lang>/<slug>/user/<name>`, `/central/<slug>` (`/hub/<slug>` → 301).

Auth: `/login`, `/auth/callback`, `/logout`, `GET /api/session`.

Contests: `GET/POST /api/contests`, `GET /api/contests/<id>`, `POST …/sync`, `POST …/config`, `POST …/approve`, `POST …/books`, `POST …/books/<bid>/update|remove`, `POST …/reserve-page`, `GET …/user/<name>/pages`, `GET /api/contests/by-slug/<lang>/<slug>`.

Review: `GET …/jury/next-page` (filters: book_id, status, contributor, scope, page_id, peek), `POST …/jury/lock|preview|submit`, `GET …/bulk/candidates`, `POST …/bulk/review`, `POST …/batch-revert`.

Data: `GET …/export-csv`, `GET …/backup.json`, `GET …/backup-pages.csv`, `POST …/restore/validate`, `POST …/restore`, `GET …/audit`, `GET/POST /api/admin/snapshots`.

Deletion: `POST/DELETE …/delete-request`, `POST …/delete` (contest and central).

Central: `GET/POST /api/central`, `GET /api/central/<slug>`, `POST /api/central/<slug>/config|approve`.

Errors return `{"success": false, "error_code": "...", "error": "..."}`; the frontend translates `error_code`.

---

## 7. Frontend (`templates/index.html`)

- Single Vue 3 app (no build step). Views: `home` (status tabs, search, language/project filter, central contests as a collapsible tree), `contest`, `edit_config` / `create`, `jury` (review panel), `userpages`, `central`, `central_form`.
- **i18n**: the `I18N = { en: {...}, bn: {...} }` table. No hard-coded UI text; English is the fallback. To add a language, copy `en`.
- Dates are always UTC, with Bangla digits and month names in Bangla.
- Keyboard shortcuts use `event.code` (Alt+A accept, Alt+F needs fix, Alt+R reject, Alt+N next), so they work with Bangla/Avro layouts and never fire inside text fields.
- Cached contest/central data (localStorage) is shown instantly, then refreshed; links prefetch on hover; there is a top progress bar and fade transitions.
- Design system: Wikisource logo blues only, `ws.dark #294584`, `ws.chambray #375493`, `ws.cerulean #236798`, `ws.bright #006699`, `ws.nature #4D8AC0`, plus status colors (green accepted, amber pending, red rejected). Shared CSS classes: `.ps-card`, `.ps-stat`, `.ps-btn*`, `.ps-input`, `.ps-label`.

---

## 8. Testing

- `python3 -m unittest discover -s tests` (44 tests, network fully mocked): OAuth flow, counting rules, sync safety, review rules, equal distribution, bulk review, central contests, my-pages visibility, backup/restore/tamper, audit append-only, migration of an old database, deletion workflow.
- Load test (local, SQLite): 1k / 10k / 50k / 100k pages; 4 concurrent reviewers; restore of 50k pages ≈ 4 s.
- Browser tests were run with Playwright + Chrome (CDN assets served locally): direct links, i18n, shortcuts, bulk selection, mobile (375 px) without horizontal overflow.

---

## 9. Deployment

First time (per tool):

1. Set the environment variables with `toolforge envvars create`.
2. Clone the repository into `~/www/python/src`:
   ```bash
   git clone <REPO_URL> ~/www/python/src
   cp ~/www/python/src/deploy/uwsgi.ini ~/www/python/uwsgi.ini
   ```
3. Inside `webservice python3.11 shell`, create the virtualenv and install dependencies:
   ```bash
   python3 -m venv ~/www/python/venv
   source ~/www/python/venv/bin/activate
   pip install -r ~/www/python/src/requirements.txt
   ```
4. Start the service with `toolforge webservice python3.11 start`.

Update:

```bash
cd ~/www/python/src && git pull && toolforge webservice restart
```
The database migrates itself automatically on startup.
