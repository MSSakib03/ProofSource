# ProofSource — context file for AI assistants

**How to use this file.** Give it to any AI together with your request (a prompt, a bug report, a screenshot). It tells the AI what the tool is, how the code is organised, which rules must never be broken, and how to work with the owner. It is a summary: when it disagrees with the code, the code wins; for depth read `ARCHITECTURE.md` (design), `README.md` / `README_bn.md` (user guide) and `docs/SECURITY_AUDIT.md` (security decisions).

Suggested opening prompt:

> Read CONTEXT.md first. Then do: *<your task>*. Follow the "Working rules" section, run the tests, and tell me what you could not verify.

---

## 1. What the tool is

ProofSource runs **proofreading contests on Wikisource** (any language edition). It reads page histories from the wiki, credits the person who first brought a page to *Proofread*, lets reviewers (juries) accept / reject pages, keeps leaderboards, and supports **central contests** that combine several wikis.

- Live: `https://proofsource.toolforge.org` (staging: `proofsource-test.toolforge.org`), hosted on **Wikimedia Toolforge** (Kubernetes, `python3.11`, uWSGI, several workers).
- Users log in with **MediaWiki OAuth 2.0** (meta.wikimedia.org). UI languages: **English (default) and Bangla**. Most users are Bangla Wikisource contributors, many on **phones**.
- Owner/maintainer: MS Sakib (tool admin). Repository: `MSSakib03/ProofSource`.

## 2. Stack and file map

Python 3.11+ · Flask 3 · Flask-SQLAlchemy 3 (SQLAlchemy 2) · SQLite (dev) / **ToolsDB = MariaDB** (production) · Vue 3 (vendored, **no JS build step**) · Tailwind CSS (built at development time, output committed).

| Path | Role |
|---|---|
| `app.py` | Everything server-side: routes, permissions, sync, statistics, review, central contests, trash, notices |
| `models.py` | SQLAlchemy models + `migrate_schema()` (adds missing tables/columns automatically) + `utcnow()` |
| `wiki_engine.py` | **All** MediaWiki API access + the proofread eligibility rules + `edit_wikisource_live`, `post_talk_message` |
| `backup_engine.py` | JSON/CSV export, signed backups, validated restore, DB snapshots |
| `security.py` | CSRF, CSP/security headers, token encryption, rate limiter, URL checks |
| `db_config.py` | Database URL resolution and pool options |
| `manage.py` | CLI: `check-db`, `copy-db`, `snapshot`, `sync` |
| `templates/index.html` | **The whole frontend**: Vue app, the `I18N` table (en + bn), markup (≈3.6k lines) |
| `static/src/app.css` → `static/css/app.css` | Stylesheet source → built file (**commit the built file**) |
| `static/vendor/`, `static/fonts/` | Self-hosted Vue, DOMPurify, Bangla fonts (no CDN, ever) |
| `tests/` | `test_app.py` (base class, helpers), `test_features.py`, `test_security.py`, `test_hardening.py`, `test_v10.py` |
| `deploy/uwsgi.ini` | Toolforge uWSGI settings (`buffer-size = 65535` for OAuth callbacks) |

## 3. Domain model in one page

- **Contest** (`contests`): slug, title, `wiki_domain`, start/end (UTC), points, `jury_mode`, `reject_action`, `review_enabled`, `review_assignment` (`open`/`equal`), `submission_mode` (`auto`/`enroll`/`manual`), admins, juries, excluded users, announcements (en/bn), `approval_status`, `hub_id`, trash fields.
- **Hub = central contest** (`hubs`): rules (dates, points, policies `yes`/`no`/`project`) forced onto its child contests (`apply_central_rules`, `locked_fields`); one child contest per wiki project; announcements; trash fields.
- **IndexBook** (`index_books`): one Index page (book) of a contest; custom points, excluded pages, optional own time window (must lie inside the contest window).
- **ContestPage** (`contest_pages`): one row per (book, page number). Status lifecycle: `unproofread` → `eligible` (counts, not submitted yet) → `pending_review` → `accepted` / `needs_fix` / `rejected`; `reverted` = was listed, then stopped qualifying (kept forever, 0 points). `notified_at` records that the proofreader was messaged about the page.
- Others: `AuditLog` (append-only; the ORM blocks update/delete), `SlugAlias` (old slugs redirect), `Enrollment`, `ToolAdmin`, `OAuthLogin` (tokens encrypted at rest).

**Eligibility** (`wiki_engine.analyze_page_history`): only the *first time ever* a page reaches Proofread (level ≥ 3) can count; that upgrade must fall inside the contest/book window; the upgrader must have changed the page text themselves; credit never moves; the page must still be Proofread now.

**Points:** book points (custom if allowed, else default). With review: only `accepted` counts; without review `pending_review` counts too. All statistics are SQL aggregates.

**Roles** (always enforced in the backend, the UI only hides buttons): tool admin (`PROOFSOURCE_SUPER_ADMINS` + `tool_admins` table) › wiki sysop (approves contests of that wiki) › contest admin › reviewer › participant › visitor.

**Rules that exist because they caused conflicts — keep them:**
- Once a contest has **started**: start time, **link slug** and **Wikisource project** can no longer change (server returns `start_locked`, `slug_locked`, `project_locked`); the end can move but not into the past (`end_in_past`). Central contests: start fixed; projects may be added, not removed.
- A book's own start/end must be inside the contest window (`book_dates_outside_contest`).
- Risky edits of a running contest (points, excluded users, review mode, submission mode, reviewers, shortening the end) are allowed but the confirmation shows a ⚠ explanation.
- **Deleting** a contest/central contest moves it to the **trash** (`deleted_at`); only tool admins see it (`/api/admin/trash`), it can be restored, and a throttled sweep removes it for good after 7 days. Always fetch contests through `live_contest(cid)` / `live_hub(slug)` / `find_contest_by_slug` so trashed items stay invisible; list queries filter `deleted_at IS NULL`.
- Reviewers/admins can **message a proofreader** about `needs_fix`/`rejected` pages: prefilled editable wikitext posted to `User talk:<name>`; a page is offered again only after a newer review decision.
- Notices shown to ordinary users say "tool admin", never a personal name.

## 4. Conventions that matter

**Backend**
- Times are **naive UTC**. Use `models.utcnow()` — never `datetime.utcnow()` (deprecated; zero deprecation warnings is a goal).
- Errors: `err("snake_case_code", status, **extra)` → `{"success": false, "error_code": ..., "error": ...}`. The frontend translates `error_code` through `err_<code>` keys.
- Every state-changing route checks the caller (`get_current_user()`, `is_contest_admin`, `is_reviewer`, `is_super_admin`…). New route ⇒ add the check **and** a test for the forbidden case.
- Input is validated (`to_int`, `clip`, enum checks); bad input is 400, never 500. Text is length-capped (`LEN_*`).
- All wiki traffic goes through `wiki_engine.get_api_url()` (Wikisource allowlist). Never send a request or token anywhere else.
- Schema changes: add the column to the model; `migrate_schema()` adds it to existing databases. Never drop/rename columns or rewrite data.
- Audit every meaningful change with `log_action(...)`.
- Several uWSGI workers run at once: rate limits and caches are per worker; use conditional UPDATEs for locks (see the sync lock).

**Frontend (`templates/index.html`)**
- One Vue app, options API. Add text to **both** `I18N.en` and `I18N.bn`; no hard-coded UI text. Keys used dynamically: `err_*`, `act_*`, `mode_*`, `cfg_*`, `pol_*`, `pv_*`, `revert_*`, `warn_*`.
- Styling: Tailwind utility classes + tokens in `static/src/app.css`. After changing the template or that file run `npm install && npm run build:css` and **commit `static/css/app.css`** (Toolforge has no Node).
- **Phone first**: below 640px wide tables become cards (`ps-stack` + a `data-label` on every cell; `ps-lb` leaderboard card, `ps-up` participant-page card). No horizontal scrolling on phones. Dates are UTC with Bangla digits in Bangla. The `AA` button scales the root font size, so use `rem`/relative sizes.
- Phone header and leaderboard rows are *measured* (`fitUserMenu`, `fitLb`, like `fitJuries`): re-run them after anything that changes widths (language, text size, fonts, data).
- Required fields carry a red `*`, errors appear on the field (`validateConfig`, `validateCentral`); the server enforces the same rules.
- The create-contest form is a wizard (Next/Back, only the last tab saves); the edit form keeps drafts across tabs and its confirmation lists only the changes.
- Jinja caches the template: **restart the dev server after editing `index.html`**.

**Security rules (do not weaken)**
- CSRF token (`X-CSRF-Token`) on every non-GET; CSP with nonce; `frame-ancestors 'none'`; no third-party assets; wiki HTML sanitised with DOMPurify; links from user input must be `http(s)`.
- OAuth tokens encrypted at rest; never in snapshots/backups/logs. Backups are HMAC-signed; admins cannot restore forged files; approval/owner/wiki/slug are never restored from a backup.
- Secret key is shared by all workers (atomic key file). Uploads are capped (default 32 MB) because backups are parsed in RAM. `ProxyFix` makes `request.remote_addr` the real visitor; anonymous rate-limit buckets are per address.
- Accepted risks are listed in `docs/SECURITY_AUDIT.md` (e.g. `unsafe-eval` for Vue's runtime compiler).

## 5. Configuration (environment variables)

`OAUTH2_CLIENT_ID`, `OAUTH2_CLIENT_SECRET`, `OAUTH2_REDIRECT_URI`, `SECRET_KEY` (auto-generated file otherwise), `TOOLSDB_DATABASE` (production) or `DATABASE_URL` (SQLAlchemy URL; SQLite by default), `PROOFSOURCE_SUPER_ADMINS` (permanent tool admins, default `MS Sakib`), `PROOFSOURCE_BACKUP_DIR`, `PROOFSOURCE_SNAPSHOT_HOURS/KEEP`, `PROOFSOURCE_JSON_BACKUPS_KEEP`, `PROOFSOURCE_FORCED_SYNC_SECONDS`, `PROOFSOURCE_SYNC_SECONDS`, `PROOFSOURCE_MAX_UPLOAD_MB` (32), `PROOFSOURCE_PROXY_HOPS` (1), `PROOFSOURCE_PHONE_LEADERBOARD` (`cards`; `table` restores the old scrolling leaderboard on phones), `PROOFSOURCE_INSECURE_COOKIES=1` (local http only). Full table: `ARCHITECTURE.md` §2.

## 6. Everyday commands

```bash
pip install -r requirements.txt pytest          # once
python3 -m pytest -q                            # all tests (network is mocked; ~12 s)
python3 -m pytest -q tests/test_v10.py          # one file
npm install && npm run build:css                # after template/CSS changes (commit static/css/app.css)
PROOFSOURCE_INSECURE_COOKIES=1 python3 app.py   # local server (OAuth needs real credentials; see Testing below)
python3 manage.py check-db | snapshot | sync | copy-db --from <src>
```

## 7. Testing

- **No test may touch the network.** `tests/test_app.py` replaces `requests` in `wiki_engine` and stubs `fetch_live_book_pages_from_wiki`, `fetch_index_page_count`, `edit_wikisource_live`, `is_user_wiki_admin`, …; copy its pattern.
- Use the `Base` class (fresh DB per test), `self.login("Name")`, and `self.make_contest(entries={page: (user, level, rev)}, **overrides)`. The client sends CSRF automatically. `make_contest` creates a contest that **has already started** (5 days ago): pass `start_time`/`end_time` for a future one when testing pre-start behaviour.
- A change is not done until: tests added/updated, `python3 -m pytest -q` green, `python3 -m pyflakes <files>` clean, README/ARCHITECTURE updated when behaviour changed.
- For UI changes, verify in a browser (Playwright/Chromium) at phone width (≈390 px) **and** desktop, light and dark; check there is no horizontal scroll and no console errors. For a demo without OAuth, seed a temp SQLite DB, insert an `OAuthLogin` row and put its id in a signed Flask session cookie (`session["login_id"]`).
- Wikisource is unreachable from some sandboxes: say clearly what was only tested with mocks.

## 8. Deployment (Toolforge) in short

Repo = `~/www/python/src`; `deploy/uwsgi.ini` → `~/www/python/uwsgi.ini`; venv in `~/www/python/venv`; secrets via `toolforge envvars`; start with `toolforge webservice python3.11 start`; tables are created/upgraded automatically. Recommended scheduled jobs: `manage.py sync` every 15 min and `manage.py snapshot` every 12 h. Full steps: `ARCHITECTURE.md` §9.

## 9. Working rules for the AI

1. **Read before editing.** Search the code for the thing you touch; reuse helpers (`live_contest`, `err`, `log_action`, `rate_limit`, `to_int`, `clip`, `utcnow`).
2. **Smallest change that solves the problem**, in the existing style. No new dependencies, no build step for JS, no CDN.
3. **Bilingual always:** every new UI string in English **and** Bangla (natural, polite standard Bangla); update `README.md` **and** `README_bn.md` for user-visible changes, and `ARCHITECTURE.md` for design/config changes.
4. **Phone matters:** design for ≈360–390 px first; desktop must not regress.
5. **Security is part of every task:** permission check in the backend, input validation, no secrets in logs/URLs, no weakening of the rules in §4.
6. **Never destroy data:** no destructive migrations; deletion goes through the trash; backups must stay restorable.
7. **Report honestly:** list what you ran and the result, what you could not test, and any decision you made that the owner did not specify.
8. **Git:** work on the branch you are given; commit messages say *why*; do not open a pull request unless asked; never force-push shared branches. Summaries to the owner may be in Bangla if they write in Bangla.
9. **Ask only when blocked** by a decision that is really the owner's (for example, lock vs. warn for a risky setting); otherwise choose the sensible default, mention it, and continue.

## 10. Decisions already made by the owner (do not reopen)

- Pending/rejected contests are readable by ID without login (data is public wiki data); only the *lists* hide them.
- Slug, start time and project are locked after the start; other risky changes only warn.
- Central contests have notices; the Bangla notice appears only when a Bangla Wikisource is involved (or text already exists).
- Footer on phones is one line: `ProofSource · Created by … · Report an issue`.
- Phone leaderboard = cards; the old table stays available through `PROOFSOURCE_PHONE_LEADERBOARD=table`.
- Reviewer statistics on phones: name plus count chips (zero counts hidden).
- Trash keeps deleted contests 7 days; only tool admins see it.

## 11. Ideas not done yet (candidates for future prompts)

- Real-device and real-Wikisource verification of the talk-page message and the phone cards.
- Locking more settings after the start instead of only warning (points, excluded users, review mode) if conflicts keep appearing.
- A global (cross-worker) rate limiter; precompiled Vue templates to remove `unsafe-eval` from the CSP.
