# AGENTS.md — ProofSource

Welcome to **ProofSource**. This document outlines system architecture, design invariants, operational workflows, and engineering guardrails for AI coding agents and human developers working in this repository.

---

## 1. Project Overview & Mission

**ProofSource** is a web tool for running **proofreading contests on Wikisource** (supporting all language editions). It tracks pages proofread during contest periods, provides a review interface for juries to accept or reject submissions, maintains real-time leaderboards, and supports multi-wiki **central contests** with unified scoring.

- **Production URL:** `https://proofsource.toolforge.org`
- **Hosting:** Wikimedia Toolforge (Kubernetes webservice, `python3.11`, uWSGI)
- **Stack:** Python 3.11 · Flask 3.0 · Flask-SQLAlchemy 3.1 (SQLAlchemy 2.x) · ToolsDB (MariaDB in prod, SQLite in dev) · Vue 3 SPA · Tailwind CSS v3
- **Authentication:** MediaWiki OAuth 2.0 (via `meta.wikimedia.org`)
- **UI Localization:** English (default) and Bengali (`bn`), synchronized via central translation table

---

## 2. Repository Structure

```
WikiSource/
├── app.py                # Main Flask app, routes, auth, jury reviews, sync logic
├── models.py             # SQLAlchemy 2.x models & schema migrations
├── wiki_engine.py        # Wikisource & MediaWiki API engine, revision tracking
├── backup_engine.py      # Automated JSON/CSV exports, signed backups, restore validation
├── security.py           # CSRF protection, CSP headers, AES token encryption, rate limiting
├── db_config.py          # Database URI resolution (SQLite dev / ToolsDB production)
├── manage.py             # CLI utilities (check-db, copy-db, snapshot)
├── requirements.txt      # Python dependencies
├── package.json          # Tailwind CSS build tooling
├── tailwind.config.js    # Tailwind configuration (Wikisource theme & Codex styling)
├── static/
│   ├── src/app.css       # Tailwind source stylesheet
│   └── css/app.css       # Minified compiled stylesheet (committed for Toolforge)
├── templates/
│   └── index.html        # Single-page Vue 3 application template
└── tests/                # Automated pytest suite
```

---

## 3. Engineering Guardrails & Tooling

### A. Ponytail Engineering Discipline (Lazy Senior Dev Mode)
* **The Principle:** Lazy means disciplined, lean, and efficient. The best code is code you never had to write.
* **Climb the Ladder Before Writing Code:**
  1. **YAGNI:** Does this feature, parameter, or abstraction actually need to exist?
  2. **Internal reuse:** Check if a helper, query, or utility already exists in `app.py`, `models.py`, or `wiki_engine.py`.
  3. **Standard library:** Reach for Python stdlib (`urllib`, `dataclasses`, `datetime`, `re`) before pulling third-party packages.
  4. **Native platform:** Leverage SQLite/MariaDB SQL features and modern HTML/CSS directly.
  5. **Installed dependencies:** Rely on existing installed libraries (`Flask`, `SQLAlchemy`, `requests`, `cryptography`).
  6. **Concise code:** Strive for clear, minimal implementations.
  7. **Minimum working diff:** Always prefer the shortest working diff once root cause is proven.
* **Anti-Bloat:** No unrequested abstractions, no speculative wrappers, no added dependencies without explicit user request.
* **Shortcut Accountability:** Mark any intentional shortcut with a `# ponytail:` comment detailing reason and upgrade path.

### B. Modern Web Guidance (Web Platform Standards)
* **Native Over Polyfill:**
  * Dialogs and modal overlays: Use native `<dialog>` with `.showModal()` and `::backdrop`.
  * Tooltips / popovers: Use HTML `popover` and `popovertarget` attributes where viable.
  * Transitions: Utilize CSS transitions and native View Transitions API.
* **Core Web Vitals & Responsiveness:**
  * Prevent Cumulative Layout Shifts (CLS) by giving images and icons explicit width/height.
  * Keep mobile layouts fully responsive (Wikisource and Codex design system guidelines).

### C. Wikimedia Compliance & Integrity
* **Strict MediaWiki Action API Integration:**
  * Always set a descriptive User-Agent header (format: `ProofSource/1.0 (https://proofsource.toolforge.org; <contact>)`).
  * Use real endpoints (`action=query&prop=revisions`, `action=parse`); never mock production network paths.
  * Respect Wikimedia rate limits (max 1 edit / 3 seconds for write actions; back off on `429` / `maxlag`).
* **OAuth 2.0 Security:**
  * Session cookies must be `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
  * Encrypt access tokens at rest using AES-256 (`security.py`).

### D. Translatewiki & Bengali i18n
* Maintain parity between English and Bengali strings.
* Bengali translations must follow natural, polite standard colloquial Bengali (চলিত রীতি) without machine translation artifacts.

---

## 4. Model Context Protocol (MCP) & Plugin Ecosystem

* **Workspace Declared Plugins (`.agents/plugins.json`):**
  * `modern-web-guidance` — Web platform standards & modern API patterns.
  * `ponytail` — Senior engineering restraint, code review, and anti-complexity checks.
  * `context-mode` — Context window management and output compression.
* **Global MCP Servers Configured:**
  * `shadcn` — Fast UI component reference.
  * `puppeteer` — Headless browser UI testing & screenshot verification.
  * `ponytail` — Minimalist rules and review prompt engine.
  * `context-mode` — FTS5 knowledge base and execution isolation.

---

## 5. Development Commands

### Tailwind CSS Build
```powershell
# Install dev dependencies
pnpm install

# Build minified stylesheet
pnpm run build:css

# Watch for CSS changes during dev
pnpm run watch:css
```

### Python App Execution & Tests
```powershell
# Install dependencies
pip install -r requirements.txt

# Run test suite
pytest

# Run local development server
python app.py
```
