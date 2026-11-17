# Graph Report - WikiSource  (2026-11-17)

## Corpus Check
- 33 files · ~77,514 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 13 file(s) not represented in the graph (top: .woff2 9, .css 2, (none) 1)

## Summary
- 953 nodes · 2598 edges · 59 communities (43 shown, 16 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 183 edges (avg confidence: 0.94)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 13
- Community 14
- Community 15
- Community 16
- Community 17
- Community 18
- Community 19
- Community 20
- Community 21
- Community 22
- Community 23
- Community 24
- Community 25
- Community 26
- Community 27
- Community 28
- Community 29
- Community 30
- Community 31
- Community 32
- Community 33
- Community 34
- Community 35
- Community 36
- Community 37
- Community 38
- Community 39
- Community 40
- Community 41
- Community 42
- Community 43
- Community 44
- Community 45
- Community 46
- Community 47
- Community 48
- Community 49
- Community 50
- Community 51
- Community 52
- Community 53
- Community 54
- Community 55
- Community 56
- Community 57

## God Nodes (most connected - your core abstractions)
1. `Contest` - 77 edges
2. `so()` - 52 edges
3. `ContestPage` - 47 edges
4. `get_current_user()` - 43 edges
5. `err()` - 40 edges
6. `parse()` - 37 edges
7. `Base` - 35 edges
8. `contest_payload()` - 32 edges
9. `setup()` - 32 edges
10. `log_action()` - 31 edges

## Surprising Connections (you probably didn't know these)
- `tool_admin_names()` --uses--> `ToolAdmin`  [INFERRED]
  app.py → models.py
- `is_contest_admin()` --uses--> `Contest`  [INFERRED]
  app.py → models.py
- `is_reviewer()` --uses--> `Contest`  [INFERRED]
  app.py → models.py
- `generate_unique_slug()` --uses--> `Contest`  [INFERRED]
  app.py → models.py
- `get_book_points()` --uses--> `IndexBook`  [INFERRED]
  app.py → models.py

## Import Cycles
- None detected.

## Communities (59 total, 16 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (65): add_books_from_multiline(), _pick_spaced_page(), A random unproofread page of `book`, as far from other people's work as the…, Returns True if the wiki data was complete. All changes are committed at once., sync_book_with_wikisource(), tool_admin_names(), datetime, difflib (+57 more)

### Community 1 - "Community 1"
Cohesion: 0.08
Nodes (50): cc(), cleanup(), enterRCDATA(), fastForwardTo(), finish(), handleAttrNameEnd(), handleAttrStart(), handleInAttrValue() (+42 more)

### Community 2 - "Community 2"
Cohesion: 0.09
Nodes (45): api_bulk_candidates(), api_contest_by_slug(), api_index_search(), api_jury_next_page(), api_session(), api_tool_admin_remove(), api_tool_admins(), api_wikis() (+37 more)

### Community 3 - "Community 3"
Cohesion: 0.09
Nodes (46): an(), ci(), Co(), Cr(), Do(), eo(), fi(), Hn() (+38 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (40): _accepted_expr(), allows_bonus(), allows_custom_points(), api_enroll(), api_export_csv(), api_my_eligible(), api_submit_pages(), apply_submission_mode() (+32 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (44): aa(), ac(), ca(), da(), ea(), ec(), fa(), gu() (+36 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (36): argparse, configparser, build_mysql_url(), engine_options(), Database configuration. Production on Toolforge should use ToolsDB (MariaDB).…, URL safe to print (password hidden)., redact(), _replica_cnf_credentials() (+28 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (26): ae(), At(), Ct(), dn(), _e(), et(), fe(), Ge() (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.09
Nodes (39): api_snapshots(), backup_dir(), _checksum(), csv_safe(), _dt(), dump_database_json(), export_contest(), _integrity_ok() (+31 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (32): api_approve_central(), api_approve_contest(), api_audit(), api_backup_json(), api_backup_pages_csv(), api_batch_revert(), api_central_delete(), api_central_delete_request() (+24 more)

### Community 10 - "Community 10"
Cohesion: 0.08
Nodes (11): json, Base, OAuthTests, AbuseTests, CSRFTests, HeaderTests, Regression tests for the security audit fixes (no network: every wiki call is…, WikiDomainTests (+3 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (10): Tool administrators added from the UI (in addition to PROOFSOURCE_SUPER_ADMINS)., ToolAdmin, CentralPolicyTests, DeepCheckTests, ProofreadAnyPageTests, Tests for submission modes (auto / enrol / manual), reverted pages that stay…, The Wikisource list comes from Meta-Wiki's site matrix: a new wiki needs no…, _Resp (+2 more)

### Community 13 - "Community 13"
Cohesion: 0.13
Nodes (25): _a(), eu(), ga(), Gl(), ha(), hc(), Ja(), Jl() (+17 more)

### Community 14 - "Community 14"
Cohesion: 0.12
Nodes (5): BackupTests, ConfigTests, ContestLinkTests, MyPagesTests, SyncTests

### Community 15 - "Community 15"
Cohesion: 0.18
Nodes (23): api_add_contest_book(), api_bulk_review(), api_create_contest(), api_jury_lock(), api_jury_preview(), api_jury_submit(), api_update_contest_book(), api_update_contest_config() (+15 more)

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (23): api_create_central(), api_get_central(), api_list_central(), api_list_contests(), api_update_central(), apply_central_rules(), can_approve_central(), central_brief() (+15 more)

### Community 17 - "Community 17"
Cohesion: 0.12
Nodes (5): IdentityTests, fake_post(), LoginResilienceTests, R, Login must keep working after a deploy that forgot `pip install -r…

### Community 18 - "Community 18"
Cohesion: 0.14
Nodes (18): Ai(), er(), es(), hs(), Ii(), kn(), kr(), ls() (+10 more)

### Community 19 - "Community 19"
Cohesion: 0.18
Nodes (5): live(), Build a fake fetch_live_book_pages_from_wiki returning the given pages., RevertedPageTests, statuses(), SubmissionModeTests

### Community 20 - "Community 20"
Cohesion: 0.21
Nodes (5): Bn(), en(), $s(), t(), xi

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (11): atexit, flask_testing, FlaskClient, io, shutil, sqlite3, tempfile, CSRFClient (+3 more)

### Community 22 - "Community 22"
Cohesion: 0.22
Nodes (4): IndexBook, AssignmentTests, CentralTests, DeleteTests

### Community 23 - "Community 23"
Cohesion: 0.23
Nodes (16): beforeUpdate(), Bi(), created(), has(), Hi(), f(), In(), ji() (+8 more)

### Community 24 - "Community 24"
Cohesion: 0.15
Nodes (11): after_request, add_security_headers(), base64, flask, hashlib, hmac, importlib_util, secrets (+3 more)

### Community 25 - "Community 25"
Cohesion: 0.18
Nodes (10): as(), be(), Bs(), cs(), ds(), fs(), Ms(), us() (+2 more)

### Community 26 - "Community 26"
Cohesion: 0.27
Nodes (11): _enc(), _expiry_from(), _fetch_username(), get_login_record(), get_valid_access_token(), oauth_callback(), purge_expired_logins(), _token_request() (+3 more)

### Community 27 - "Community 27"
Cohesion: 0.22
Nodes (5): ce(), de(), he(), pe(), ue

### Community 28 - "Community 28"
Cohesion: 0.20
Nodes (9): description, devDependencies, tailwindcss, name, private, scripts, build:css, watch:css (+1 more)

### Community 31 - "Community 31"
Cohesion: 0.22
Nodes (9): csrf_protect(), db_config_warnings(), init_db(), Every state-changing request must carry the session's CSRF token in X-CSRF-…, before_request, is_sqlite(), crypto_available(), csrf_failure_reason() (+1 more)

### Community 32 - "Community 32"
Cohesion: 0.29
Nodes (8): api_get_contest(), api_sync_contest(), Returns cached data immediately. ?sync=1 forces a blocking sync (manual button)., A sync walks the wiki history of every changed page; never run them back to…, Background sync called by the frontend after showing cached data., Guarded so two workers never sync the same contest at the same time. With a…, sync_contest_books(), _sync_too_soon()

### Community 33 - "Community 33"
Cohesion: 0.33
Nodes (7): api_contest_user_pages(), row(), can_delete(), is_listed_admin(), Admin by name (creator or in the admin list) — super-admin powers are NOT…, Only the tool admin deletes, and only when a listed admin asked for it — or…, same_user()

### Community 34 - "Community 34"
Cohesion: 0.29
Nodes (7): handle_server_error(), handle_value_error(), _plain(), Plain-text error page: nothing from the URL is ever rendered as HTML., Unexpected errors: the traceback is in the server log; the API answers with…, Bad user input (non-numeric numbers, invalid domain...) is a 400, never a 500…, errorhandler

### Community 35 - "Community 35"
Cohesion: 0.38
Nodes (4): D(), O(), R(), v()

### Community 36 - "Community 36"
Cohesion: 0.29
Nodes (3): DataIntegrityTests, fake_get(), A page first proofread after its 500th revision used to be judged on a cut-off…

### Community 37 - "Community 37"
Cohesion: 0.33
Nodes (6): _dec(), decrypt_secret(), _fernet(), Fernet instance, or None when `cryptography` is missing (an update deployed…, Decrypts a stored token. Legacy plaintext rows are returned unchanged. Returns…, _warn_no_crypto()

### Community 38 - "Community 38"
Cohesion: 0.33
Nodes (3): RateLimiter, Sliding-window limiter, per process. Good enough to stop one user (or a script)…, Records a hit; returns False when the key is over its limit.

### Community 40 - "Community 40"
Cohesion: 0.40
Nodes (5): br(), bt(), Pt(), xt(), Yo()

### Community 41 - "Community 41"
Cohesion: 0.40
Nodes (5): or(), qo(), tr(), Uo(), wo()

### Community 43 - "Community 43"
Cohesion: 0.50
Nodes (4): asset_url(), /static URL with a content hash, so browsers never keep a stale CSS/JS after a…, template_helpers(), context_processor

### Community 44 - "Community 44"
Cohesion: 0.50
Nodes (3): generate_unique_slug(), Old slugs keep working after a contest's slug is changed (they redirect to the…, SlugAlias

### Community 45 - "Community 45"
Cohesion: 0.50
Nodes (4): G(), H(), rr(), z()

### Community 46 - "Community 46"
Cohesion: 0.50
Nodes (4): Gr(), Hr(), jr(), qr()

### Community 47 - "Community 47"
Cohesion: 0.67
Nodes (3): beforeMount(), beforeUnmount(), ni()

## Knowledge Gaps
- **7 isolated node(s):** `name`, `private`, `description`, `build:css`, `watch:css` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 232 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **16 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Base` connect `Community 10` to `Community 4`, `Community 36`, `Community 6`, `Community 42`, `Community 11`, `Community 14`, `Community 17`, `Community 19`, `Community 21`, `Community 22`, `Community 26`, `Community 30`?**
  _High betweenness centrality (0.059) - this node is a cross-community bridge._
- **Why does `Contest` connect `Community 4` to `Community 0`, `Community 33`, `Community 2`, `Community 32`, `Community 6`, `Community 9`, `Community 10`, `Community 44`, `Community 14`, `Community 15`, `Community 16`, `Community 21`, `Community 22`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `ContestPage` connect `Community 4` to `Community 0`, `Community 33`, `Community 2`, `Community 6`, `Community 9`, `Community 14`, `Community 15`, `Community 16`, `Community 21`, `Community 22`, `Community 30`?**
  _High betweenness centrality (0.015) - this node is a cross-community bridge._
- **Are the 70 inferred relationships involving `Contest` (e.g. with `add_books_from_multiline()` and `allows_bonus()`) actually correct?**
  _`Contest` has 70 INFERRED edges - model-reasoned connections that need verification._
- **Are the 39 inferred relationships involving `ContestPage` (e.g. with `_accepted_expr()` and `api_backup_json()`) actually correct?**
  _`ContestPage` has 39 INFERRED edges - model-reasoned connections that need verification._
- **What connects `name`, `private`, `description` to the rest of the system?**
  _7 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.056314699792960665 - nodes in this community are weakly interconnected._