import logging
import os
import re
import time
import difflib
from datetime import datetime
import requests

USER_AGENT = "ProofSource/5.0 (https://proofsource.toolforge.org; User:MS_Sakib)"


def canonical_username(name) -> str:
    """MediaWiki canonical form: '_' -> ' ', single spaces, first letter upper-case.
    Usernames are case-sensitive after the first letter, so 'MS Sakib' != 'Ms sakib'."""
    n = re.sub(r"[\s_]+", " ", str(name or "")).strip()
    return (n[:1].upper() + n[1:]) if n else ""


def _load_super_admins() -> frozenset:
    raw = os.environ.get("PROOFSOURCE_SUPER_ADMINS", "MS Sakib")
    return frozenset(canonical_username(u) for u in raw.split(",") if u.strip())


# Tool admins. Matched EXACTLY (canonical form), never case-insensitively, so a look-alike
# account such as "Ms sakib" can never gain tool-admin rights.
SUPER_ADMINS = _load_super_admins()


def is_super_admin_name(username) -> bool:
    return bool(username) and canonical_username(username) in SUPER_ADMINS


# Only Wikisource wikis may be contacted. This blocks SSRF and, above all, prevents a contest
# pointing at a foreign server that would receive reviewers' OAuth tokens or lie about who is
# a sysop there.
WIKISOURCE_DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]{1,30}\.)?wikisource\.org$")


def normalize_wiki_domain(raw):
    """Returns the clean domain (e.g. 'bn.wikisource.org') or None if it is not a Wikisource."""
    d = str(raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.rstrip("/")
    return d if WIKISOURCE_DOMAIN_RE.match(d) else None


def is_valid_wiki_domain(raw) -> bool:
    return normalize_wiki_domain(raw) is not None


PAGEQUALITY_TAG_REGEX = re.compile(r'<pagequality\s+level=["\'](\d)["\']\s+user=["\'](.*?)["\']\s*/>')
_PQ_PREFIX_RE = re.compile(r'<noinclude><pagequality level="(\d)" user="')
_PQ_TAG_END_RE = re.compile(r'"\s*/>')

NS_CACHE = {}
ADMIN_CACHE = {}
BOT_CACHE = {}
ADMIN_CACHE_TTL = 600   # seconds; a removed sysop loses approval rights quickly


def get_api_url(wiki_domain: str) -> str:
    domain = normalize_wiki_domain(wiki_domain)
    if not domain:
        raise ValueError("invalid_wiki_domain")
    return f"https://{domain}/w/api.php"

def parse_wiki_timestamp(ts_str: str) -> datetime:
    if not ts_str:
        return datetime.utcfromtimestamp(0)
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")

MAX_PAGE_NUMBER = 100000
MAX_EXCLUDED_PAGES = 20000


def parse_excluded_pages_spec(spec: str) -> set:
    result = set()
    if not spec:
        return result
    cleaned = spec.replace(";", ",").replace("\n", ",")
    for token in cleaned.split(","):
        part = token.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if bounds[0].strip().isdigit() and bounds[1].strip().isdigit():
                start_p = int(bounds[0].strip())
                end_p = int(bounds[1].strip())
                if start_p <= end_p <= MAX_PAGE_NUMBER and (end_p - start_p) <= 5000:
                    result.update(range(start_p, end_p + 1))
        elif part.isdigit() and int(part) <= MAX_PAGE_NUMBER:
            result.add(int(part))
        if len(result) > MAX_EXCLUDED_PAGES:
            break
    return result

def parse_excluded_pages_strict(spec: str, max_page: int = 0):
    """Returns (set_of_pages, list_of_invalid_tokens). Accepts '1-5, 12; 20-22'."""
    pages, errors = set(), []
    if not spec or not spec.strip():
        return pages, errors
    for token in re.split(r'[,;\n]+', spec):
        part = token.strip().replace("\u2013", "-").replace("\u2014", "-")
        if not part:
            continue
        m = re.fullmatch(r'(\d+)\s*-\s*(\d+)', part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < 1 or b < a or (b - a) > 5000 or b > MAX_PAGE_NUMBER or (max_page and a > max_page):
                errors.append(part)
                continue
            pages.update(range(a, b + 1))
        elif part.isdigit() and int(part) >= 1:
            if (max_page and int(part) > max_page) or int(part) > MAX_PAGE_NUMBER:
                errors.append(part)
                continue
            pages.add(int(part))
        else:
            errors.append(part[:40])
        if len(pages) > MAX_EXCLUDED_PAGES or len(errors) > 50:
            errors.append("too_many")
            break
    return pages, errors

def is_user_wiki_admin(wiki_domain: str, username: str) -> bool:
    if not username:
        return False
    u_clean = canonical_username(username)
    if is_super_admin_name(u_clean):
        return True
    if not is_valid_wiki_domain(wiki_domain):
        return False
    cache_key = f"{normalize_wiki_domain(wiki_domain)}:{u_clean}"
    hit = ADMIN_CACHE.get(cache_key)
    if hit and time.monotonic() - hit[1] < ADMIN_CACHE_TTL:
        return hit[0]
    api_url = get_api_url(wiki_domain)
    try:
        res = requests.get(api_url, params={
            "action": "query",
            "list": "users",
            "ususers": u_clean,
            "usprop": "groups",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=8).json()
        users = res.get("query", {}).get("users", [])
        if users:
            groups = set(users[0].get("groups", []))
            is_sysop = bool(groups.intersection({"sysop", "bureaucrat", "steward"}))
            ADMIN_CACHE[cache_key] = (is_sysop, time.monotonic())
            return is_sysop
    except Exception:
        pass
    return False

def identify_bot_users(wiki_domain: str, usernames: list) -> set:
    bots = set()
    to_check = []
    for u in set(usernames):
        if not u:
            continue
        ck = f"{wiki_domain}:{u.lower()}"
        if ck in BOT_CACHE:
            if BOT_CACHE[ck]:
                bots.add(u.lower())
        else:
            to_check.append(u)

    api_url = get_api_url(wiki_domain)
    for i in range(0, len(to_check), 40):
        batch = to_check[i:i+40]
        try:
            res = requests.get(api_url, params={
                "action": "query",
                "list": "users",
                "ususers": "|".join(batch),
                "usprop": "groups",
                "format": "json"
            }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
            for u_obj in res.get("query", {}).get("users", []):
                name = u_obj.get("name", "")
                groups = set(u_obj.get("groups", []))
                is_bot = "bot" in groups
                BOT_CACHE[f"{wiki_domain}:{name.lower()}"] = is_bot
                if is_bot:
                    bots.add(name.lower())
        except Exception:
            pass
    return bots

def get_namespace_prefixes(wiki_domain: str) -> dict:
    if wiki_domain in NS_CACHE:
        return NS_CACHE[wiki_domain]
    api_url = get_api_url(wiki_domain)
    is_bn = wiki_domain.startswith("bn.")
    prefixes = {
        "page": "পাতা" if is_bn else "Page",
        "index": "নির্ঘণ্ট" if is_bn else "Index",
        "file": "চিত্র" if is_bn else "File",
        "page_ns_id": 104,
        "index_ns_id": 106,
        "all_index": ["Index", "নির্ঘণ্ট", "सूची", "Индекс", "Indice", "Livre"],
        "all_page": ["Page", "পাতা", "पृष्ठ", "Страница", "Pagina"]
    }
    try:
        res = requests.get(api_url, params={
            "action": "query",
            "meta": "siteinfo",
            "siprop": "namespaces|namespacealiases",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
        ns = res.get("query", {}).get("namespaces", {})
        for _, ns_obj in ns.items():
            canonical = ns_obj.get("canonical", "")
            content_model = ns_obj.get("defaultcontentmodel", "")
            local_name = ns_obj.get("*", "")
            ns_id = ns_obj.get("id")
            if canonical == "Page" or content_model == "proofread-page" or local_name in ("পাতা", "Page"):
                prefixes["page"] = local_name or "Page"
                prefixes["page_ns_id"] = int(ns_id)
                if local_name and local_name not in prefixes["all_page"]:
                    prefixes["all_page"].append(local_name)
            elif canonical == "Index" or content_model == "proofread-index" or local_name in ("নির্ঘণ্ট", "Index"):
                prefixes["index"] = local_name or "Index"
                prefixes["index_ns_id"] = int(ns_id)
                if local_name and local_name not in prefixes["all_index"]:
                    prefixes["all_index"].append(local_name)
            elif ns_id == 6 and local_name:
                prefixes["file"] = local_name
        NS_CACHE[wiki_domain] = prefixes
    except Exception:
        pass
    return prefixes

def extract_base_book_name(wiki_domain: str, raw_title: str) -> str:
    title = raw_title.strip()
    prefixes = get_namespace_prefixes(wiki_domain)
    known = prefixes["all_index"] + prefixes["all_page"] + ["File", "চিত্র", "Image", "প্রবেশদ্বার", "Portal"]
    for p in known:
        if title.lower().startswith(p.lower() + ":"):
            title = title.split(":", 1)[1].strip()
            break
    if ":" in title and title.split(":", 1)[0] in known:
        title = title.split(":", 1)[1].strip()
    return title

def normalize_index_title(wiki_domain: str, raw_title: str) -> str:
    prefixes = get_namespace_prefixes(wiki_domain)
    base = extract_base_book_name(wiki_domain, raw_title)
    return f"{prefixes['index']}:{base}"

def _split_proofread_page(text: str):
    """Linear-time equivalent of the old regex
    ^<noinclude><pagequality level="(\\d)" user="(.*?)"\\s*/>(.*?)</noinclude>(.*?)<noinclude>(.*?)</noinclude>$
    (DOTALL). The regex backtracked cubically on pages with many <noinclude> tags, so one crafted
    page on a wiki could freeze a worker during sync (ReDoS)."""
    m = _PQ_PREFIX_RE.match(text)
    if not m:
        return None
    end_tag = _PQ_TAG_END_RE.search(text, m.end())
    if not end_tag:
        return None
    user = text[m.end():end_tag.start()]
    h_start = end_tag.end()
    h_end = text.find("</noinclude>", h_start)
    if h_end < 0:
        return None
    b_start = h_end + len("</noinclude>")
    b_end = text.find("<noinclude>", b_start)
    if b_end < 0:
        return None
    f_start = b_end + len("<noinclude>")
    if not text.endswith("</noinclude>") or len(text) - len("</noinclude>") < f_start:
        return None
    return {
        "level": int(m.group(1)),
        "user": user,
        "header": text[h_start:h_end],
        "body": text[b_start:b_end],
        "footer": text[f_start:len(text) - len("</noinclude>")],
    }


def parse_proofread_page(wikitext: str) -> dict:
    text = (wikitext or "").strip()
    parsed = _split_proofread_page(text)
    if parsed:
        return parsed
    tag_match = PAGEQUALITY_TAG_REGEX.search(text)
    if tag_match:
        return {
            "level": int(tag_match.group(1)),
            "user": tag_match.group(2),
            "header": "",
            "body": PAGEQUALITY_TAG_REGEX.sub("", text).replace("<noinclude>", "").replace("</noinclude>", "").strip(),
            "footer": ""
        }
    return {"level": 1, "user": "", "header": "", "body": text, "footer": ""}

def build_proofread_wikitext(level: int, username: str, header: str, body: str, footer: str) -> str:
    level = int(level)
    if level not in (0, 1, 2, 3, 4):
        raise ValueError("invalid_level")
    user = str(username or "").replace('"', "&quot;")   # keep the attribute well-formed
    return f'<noinclude><pagequality level="{level}" user="{user}" />{header}</noinclude>{body}<noinclude>{footer}</noinclude>'

def fetch_index_page_count(wiki_domain: str, base_name: str) -> int:
    api_url = get_api_url(wiki_domain)
    try:
        res = requests.get(api_url, params={
            "action": "query",
            "titles": f"File:{base_name}",
            "prop": "imageinfo",
            "iiprop": "dimensions|size",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
        pages = res.get("query", {}).get("pages", {})
        for _, pdata in pages.items():
            ii = pdata.get("imageinfo", [{}])[0]
            if "pagecount" in ii:
                return int(ii["pagecount"])
    except Exception:
        pass
    return 0

def _content_key(parsed: dict) -> str:
    """Page text without the quality tag; used to detect 'status-only' edits."""
    return "\x00".join([(parsed.get("header") or "").strip(),
                         (parsed.get("body") or "").strip(),
                         (parsed.get("footer") or "").strip()])


def analyze_page_history(revisions: list, start_time: datetime, end_time: datetime) -> dict:
    """
    Contest eligibility rules (revisions must be oldest -> newest):
      1. Only the FIRST time a page ever reaches Proofread (level >= 3) counts.
         Pages that were proofread/validated before the contest never count.
      2. That first upgrade must happen inside the contest window.
      3. The person who makes that upgrade must have changed the page text themselves — in the
         upgrading edit or in any of their earlier edits (creating the page counts). Someone who only
         flips the status on text written entirely by others does not get it.
      4. Credit goes to the person who made that first upgrade. Later backward/forward
         status changes (e.g. validated -> proofread) never move the credit.
      5. The page must still be Proofread/Validated now.
      6. Validation credit: the first upgrade to level 4 inside the window, after rule 1 is met.
    """
    if not revisions:
        return {"eligible": False}
    prev_level = 1
    prev_key = None
    first = None
    validator = None
    text_editors = set()          # users who changed the text at least once so far
    for rev in revisions:
        parsed = parse_proofread_page(rev.get("slots", {}).get("main", {}).get("*", "") or "")
        lvl = parsed["level"]
        key = _content_key(parsed)
        rev_dt = parse_wiki_timestamp(rev.get("timestamp"))
        actor = rev.get("user") or parsed["user"] or "Unknown"
        if prev_key is None or key != prev_key:
            text_editors.add(actor.strip().lower())
        if first is None and prev_level < 3 and lvl >= 3:
            first = {
                "proofreader": actor,
                "proofread_time": rev_dt,
                "rev_id": rev.get("revid"),
                "in_window": start_time <= rev_dt <= end_time,
                "content_changed": actor.strip().lower() in text_editors,
            }
            if lvl == 4 and first["in_window"]:
                validator = actor
        elif first is not None and validator is None and prev_level == 3 and lvl == 4:
            if start_time <= rev_dt <= end_time:
                validator = actor
        prev_level = lvl
        prev_key = key

    current_level = prev_level
    if not first:
        return {"eligible": False, "current_level": current_level, "reason": "never_proofread"}
    if not first["in_window"]:
        return {"eligible": False, "current_level": current_level, "reason": "first_proofread_outside_window"}
    if not first["content_changed"]:
        return {"eligible": False, "current_level": current_level, "reason": "status_only_edit"}
    if current_level < 3:
        return {"eligible": False, "current_level": current_level, "reason": "reverted"}
    return {
        "eligible": True,
        "current_level": current_level,
        "proofreader": first["proofreader"],
        "proofread_time": first["proofread_time"],
        "rev_id": first["rev_id"],
        "validator": validator,
    }


def fetch_revision_history(api_url: str, page_title: str, max_revs: int = 500) -> list:
    """Oldest -> newest revisions with content (paged). Raises on network/API error."""
    revs, cont = [], None
    while len(revs) < max_revs:
        params = {
            "action": "query", "titles": page_title, "prop": "revisions",
            "rvprop": "ids|timestamp|user|content", "rvslots": "main",
            "rvlimit": 50, "rvdir": "newer", "format": "json"
        }
        if cont:
            params["rvcontinue"] = cont
        res = requests.get(api_url, params=params, headers={"User-Agent": USER_AGENT}, timeout=15).json()
        if "error" in res:
            raise RuntimeError(res["error"].get("info", "api error"))
        pages = res.get("query", {}).get("pages", {})
        p_obj = next(iter(pages.values()), {})
        revs.extend(p_obj.get("revisions", []))
        cont = res.get("continue", {}).get("rvcontinue")
        if not cont:
            break
    return revs


def verify_page_in_contest_window(api_url: str, page_title: str, start_time: datetime, end_time: datetime) -> dict:
    try:
        return analyze_page_history(fetch_revision_history(api_url, page_title), start_time, end_time)
    except Exception:
        return {"eligible": False, "error": True}


def fetch_all_existing_page_levels(wiki_domain: str, base_name: str) -> dict:
    api_url = get_api_url(wiki_domain)
    prefixes = get_namespace_prefixes(wiki_domain)
    levels = {}
    gapcontinue = None
    while True:
        params = {
            "action": "query",
            "generator": "allpages",
            "gapnamespace": prefixes["page_ns_id"],
            "gapprefix": f"{base_name}/",
            "gaplimit": 50,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json"
        }
        if gapcontinue:
            params["gapcontinue"] = gapcontinue
        try:
            res = requests.get(api_url, params=params, headers={"User-Agent": USER_AGENT}, timeout=12).json()
            pages = res.get("query", {}).get("pages", {})
            for _, pdata in pages.items():
                title = pdata.get("title", "")
                parts = title.rsplit("/", 1)
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                p_num = int(parts[1])
                revs = pdata.get("revisions", [])
                if not revs:
                    continue
                content = revs[0].get("slots", {}).get("main", {}).get("*", "")
                parsed = parse_proofread_page(content)
                levels[p_num] = parsed["level"]
            if "continue" in res and "gapcontinue" in res["continue"]:
                gapcontinue = res["continue"]["gapcontinue"]
            else:
                break
        except Exception:
            break
    return levels

def fetch_live_book_pages_from_wiki(wiki_domain: str, base_name: str, start_time: datetime, end_time: datetime, known_revs: dict = None):
    """
    Returns (results, complete).
    - results[page_num] = {...}; pages whose latest revision equals known_revs[page_num]
      are returned as {"unchanged": True, "rev_latest": ...} without extra API calls (incremental sync).
    - complete is False if any API request failed; callers must then NOT treat missing
      pages as removed (prevents wiping points on a network glitch).
    """
    api_url = get_api_url(wiki_domain)
    prefixes = get_namespace_prefixes(wiki_domain)
    known_revs = known_revs or {}
    results = {}
    complete = True
    gapcontinue = None

    while True:
        params = {
            "action": "query",
            "generator": "allpages",
            "gapnamespace": prefixes["page_ns_id"],
            "gapprefix": f"{base_name}/",
            "gaplimit": 50,
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|content",
            "rvslots": "main",
            "format": "json"
        }
        if gapcontinue:
            params["gapcontinue"] = gapcontinue

        try:
            res = requests.get(api_url, params=params, headers={"User-Agent": USER_AGENT}, timeout=20).json()
            if "error" in res:
                complete = False
                break
            pages = res.get("query", {}).get("pages", {})
            for _, pdata in pages.items():
                title = pdata.get("title", "")
                parts = title.rsplit("/", 1)
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                page_num = int(parts[1])
                revs = pdata.get("revisions", [])
                if not revs:
                    continue
                rev = revs[0]
                rev_latest = rev.get("revid")

                if rev_latest and known_revs.get(page_num) == rev_latest:
                    results[page_num] = {"page_number": page_num, "page_title": title,
                                         "unchanged": True, "rev_latest": rev_latest}
                    continue

                latest_dt = parse_wiki_timestamp(rev.get("timestamp"))
                content = rev.get("slots", {}).get("main", {}).get("*", "")
                parsed = parse_proofread_page(content)
                lvl = parsed["level"]

                if lvl < 3:
                    results[page_num] = {
                        "page_number": page_num,
                        "page_title": title,
                        "level": lvl,
                        "eligible": False,
                        "rev_latest": rev_latest
                    }
                    continue

                if latest_dt < start_time:
                    continue

                check = verify_page_in_contest_window(api_url, title, start_time, end_time)
                if check.get("error"):
                    complete = False
                    continue
                if check.get("eligible"):
                    results[page_num] = {
                        "page_number": page_num,
                        "page_title": title,
                        "level": check["current_level"],
                        "eligible": True,
                        "proofreader": check["proofreader"],
                        "validator": check.get("validator"),
                        "rev_id": check["rev_id"],
                        "proofread_time": check["proofread_time"],
                        "rev_latest": rev_latest
                    }
                else:
                    results[page_num] = {
                        "page_number": page_num,
                        "page_title": title,
                        "level": check.get("current_level", 1),
                        "eligible": False,
                        "rev_latest": rev_latest
                    }

            if "continue" in res and "gapcontinue" in res["continue"]:
                gapcontinue = res["continue"]["gapcontinue"]
            else:
                break
        except Exception:
            complete = False
            break

    return results, complete

MAX_DIFF_CHARS = 60000


def compute_word_diff(first_text: str, latest_text: str) -> list:
    if len(first_text or "") > MAX_DIFF_CHARS or len(latest_text or "") > MAX_DIFF_CHARS:
        return []   # difflib is quadratic; very large pages are shown without a word diff
    a = re.split(r'(\s+)', first_text or "")
    b = re.split(r'(\s+)', latest_text or "")
    matcher = difflib.SequenceMatcher(None, a, b)
    tokens = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            tokens.append({"type": "equal", "text": "".join(a[i1:i2])})
        elif tag == 'delete':
            tokens.append({"type": "delete", "text": "".join(a[i1:i2])})
        elif tag == 'insert':
            tokens.append({"type": "insert", "text": "".join(b[j1:j2])})
        elif tag == 'replace':
            tokens.append({"type": "delete", "text": "".join(a[i1:i2])})
            tokens.append({"type": "insert", "text": "".join(b[j1:j2])})
    return tokens

def render_wikitext_preview(wiki_domain: str, page_title: str, level: int, username: str, header: str, body: str, footer: str) -> str:
    api_url = get_api_url(wiki_domain)
    full_text = build_proofread_wikitext(level, username or "User", header, body, footer)
    try:
        res = requests.post(api_url, data={
            "action": "parse",
            "title": page_title,
            "text": full_text,
            "contentmodel": "proofread-page",
            "prop": "text",
            "pst": 1,
            "disableeditsection": 1,
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=12).json()
        return res.get("parse", {}).get("text", {}).get("*", "")
    except Exception:
        return ""

def fetch_page_content_and_image(wiki_domain: str, base_name: str, page_number: int, page_title: str) -> dict:
    api_url = get_api_url(wiki_domain)
    header, body, footer, first_body, rev_id, first_rev_id = "", "", "", "", None, None
    first_user, latest_user = "", ""
    curr_level = 3

    try:
        res_latest = requests.get(api_url, params={
            "action": "query",
            "titles": page_title,
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|content",
            "rvlimit": 1,
            "rvdir": "older",
            "rvslots": "main",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
        pages = res_latest.get("query", {}).get("pages", {})
        page_data = next(iter(pages.values()), {})
        revs = page_data.get("revisions", [])
        if revs:
            rev_id = revs[0].get("revid")
            latest_user = revs[0].get("user", "")
            curr_raw = revs[0].get("slots", {}).get("main", {}).get("*", "")
            parsed_curr = parse_proofread_page(curr_raw)
            curr_level = parsed_curr["level"]
            header = parsed_curr["header"]
            body = parsed_curr["body"]
            footer = parsed_curr["footer"]

        res_first = requests.get(api_url, params={
            "action": "query",
            "titles": page_title,
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|content",
            "rvlimit": 1,
            "rvdir": "newer",
            "rvslots": "main",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
        f_pages = res_first.get("query", {}).get("pages", {})
        f_data = next(iter(f_pages.values()), {})
        f_revs = f_data.get("revisions", [])
        if f_revs:
            first_rev_id = f_revs[0].get("revid")
            first_user = f_revs[0].get("user", "")
            first_raw = f_revs[0].get("slots", {}).get("main", {}).get("*", "")
            parsed_first = parse_proofread_page(first_raw)
            first_body = parsed_first["body"]
    except Exception:
        pass

    image_url = ""
    try:
        img_res = requests.get(api_url, params={
            "action": "query",
            "titles": f"File:{base_name}",
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": 1000,
            "iiurlparam": f"page{page_number}-1000px",
            "format": "json"
        }, headers={"User-Agent": USER_AGENT}, timeout=10).json()
        img_pages = img_res.get("query", {}).get("pages", {})
        img_info = next(iter(img_pages.values()), {}).get("imageinfo", [{}])[0]
        image_url = img_info.get("thumburl") or img_info.get("url") or ""
    except Exception:
        pass

    rendered_html = render_wikitext_preview(wiki_domain, page_title, curr_level, latest_user, header, body, footer)
    diff_tokens = compute_word_diff(first_body, body)

    return {
        "header": header,
        "body": body,
        "footer": footer,
        "first_body": first_body,
        "first_rev_id": first_rev_id,
        "first_user": first_user,
        "diff_tokens": diff_tokens,
        "rendered_html": rendered_html,
        "image_url": image_url,
        "rev_id": rev_id
    }

def edit_wikisource_live(
    wiki_domain: str,
    page_title: str,
    level: int,
    username: str,
    header: str,
    body: str,
    footer: str,
    summary: str,
    oauth_creds: dict = None,
    fetch_existing_if_empty: bool = False
) -> dict:
    if not oauth_creds or not oauth_creds.get("access_token"):
        return {"saved_on_wiki": False, "error": "Please log in with MediaWiki Login first."}

    api_url = get_api_url(wiki_domain)
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Authorization": "Bearer " + oauth_creds["access_token"]
    })

    try:
        ui_res = s.get(api_url, params={
            "action": "query",
            "meta": "userinfo",
            "uiprop": "rights|groups",
            "format": "json"
        }, timeout=10).json()
        userinfo = ui_res.get("query", {}).get("userinfo", {})
        if "anon" in userinfo or not userinfo.get("name"):
            return {"saved_on_wiki": False, "error": "MediaWiki OAuth session expired. Please log in again."}

        actual_username = userinfo["name"]
        user_rights = set(userinfo.get("rights", []))

        rev_res = s.get(api_url, params={
            "action": "query",
            "titles": page_title,
            "prop": "revisions",
            "rvprop": "user|content",
            "rvlimit": 1,
            "rvslots": "main",
            "format": "json"
        }, timeout=10).json()
        pages = rev_res.get("query", {}).get("pages", {})
        p_obj = next(iter(pages.values()), {})
        revs = p_obj.get("revisions", [])
        curr_parsed = {"level": 1, "user": "", "header": "", "body": "", "footer": ""}
        if revs:
            raw_text = revs[0].get("slots", {}).get("main", {}).get("*", "")
            curr_parsed = parse_proofread_page(raw_text)
            if fetch_existing_if_empty and not body:
                header = curr_parsed["header"]
                body = curr_parsed["body"]
                footer = curr_parsed["footer"]

        if level == 4 and curr_parsed["level"] == 3:
            if curr_parsed["user"].strip().lower() == actual_username.strip().lower() and "pagequality-admin" not in user_rights:
                return {
                    "saved_on_wiki": False,
                    "error": "You cannot validate a page that you proofread yourself."
                }

        target_level = level
        if level == 4 and curr_parsed["level"] < 3 and "pagequality-admin" not in user_rights:
            target_level = 3

        r3 = s.get(api_url, params={"action": "query", "meta": "tokens", "format": "json"}, timeout=10).json()
        csrf_token = r3.get("query", {}).get("tokens", {}).get("csrftoken")
        if not csrf_token or csrf_token == "+\\":
            return {"saved_on_wiki": False, "error": "Could not obtain MediaWiki edit token. Please log in again."}

        full_wikitext = build_proofread_wikitext(target_level, actual_username, header, body, footer)

        r4 = s.post(api_url, data={
            "action": "edit",
            "title": page_title,
            "text": full_wikitext,
            "contentmodel": "proofread-page",
            "summary": summary,
            "token": csrf_token,
            "format": "json"
        }, timeout=15).json()

        if r4.get("edit", {}).get("result") == "Success":
            return {"saved_on_wiki": True, "revid": r4["edit"].get("newrevid"), "user": actual_username, "level": target_level}
        err_info = r4.get("error", {}).get("info") or str(r4.get("error") or r4)
        return {"saved_on_wiki": False, "error": err_info}
    except Exception:
        logging.getLogger("proofsource").exception("wiki edit failed on %s", wiki_domain)
        return {"saved_on_wiki": False, "error": "Could not reach the wiki. Please try again in a moment."}
