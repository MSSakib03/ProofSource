"""
Translations: loading and consistency checks.

The UI strings live in translations/<code>.json (en.json is the fallback and the reference). The server
injects them into the page (app.render_spa), so there is no extra request for the browser.

Run the checks without the test suite:   python3 manage.py check-i18n
The same checks run in tests/test_i18n.py.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSLATIONS_DIR = os.path.join(HERE, "translations")
TEMPLATE = os.path.join(HERE, "templates", "index.html")
APP_SOURCE = os.path.join(HERE, "app.py")
REFERENCE = "en"

# Keys the template builds at run time ("err_" + code, "act_" + action, ...): their prefixes.
DYNAMIC_PREFIXES = ("err_", "act_", "mode_", "cfg_", "pol_", "pv_", "revert_", "warn_", "lvl")
PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")


def load_translations(directory: str = TRANSLATIONS_DIR) -> dict:
    """{language code: {key: text}} from every translations/*.json. en.json is required."""
    out = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                out[name[:-5]] = json.load(f)
    if REFERENCE not in out:
        raise RuntimeError(f"translations/{REFERENCE}.json is missing")
    return out


def keys_used_by_template(path: str = TEMPLATE) -> set:
    """Literal keys passed to t('...') in the template (dynamic ones are covered by DYNAMIC_PREFIXES)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return set(re.findall(r"""\bt\(\s*['"]([A-Za-z0-9_]+)['"]\s*\)""", text))


def error_codes_of_server(path: str = APP_SOURCE) -> set:
    """Every literal code the server answers with: err("some_code", ...)."""
    with open(path, encoding="utf-8") as f:
        return set(re.findall(r'err\(\s*"([a-z_0-9]+)"', f.read()))


def problems(translations: dict = None) -> list:
    """Human-readable list of inconsistencies; empty when everything is in order."""
    tr = translations or load_translations()
    ref = tr[REFERENCE]
    found = []
    for lang, table in tr.items():
        if lang == REFERENCE:
            continue
        for key in sorted(set(ref) - set(table)):
            found.append(f"{lang}: missing key {key!r}")
        for key in sorted(set(table) - set(ref)):
            found.append(f"{lang}: key {key!r} does not exist in {REFERENCE}.json")
    for lang, table in tr.items():
        for key, text in table.items():
            if not isinstance(text, str) or not text.strip():
                found.append(f"{lang}: {key!r} is empty or not text")
            elif lang != REFERENCE and key in ref and set(PLACEHOLDER.findall(text)) != set(PLACEHOLDER.findall(ref[key])):
                found.append(f"{lang}: {key!r} uses different placeholders than {REFERENCE}.json")
    for key in sorted(keys_used_by_template() - set(ref)):
        if not key.startswith(DYNAMIC_PREFIXES):
            found.append(f"template uses t({key!r}) but {REFERENCE}.json has no such key")
    for code in sorted(error_codes_of_server()):
        if "err_" + code not in ref:
            found.append(f"server can answer {code!r} but {REFERENCE}.json has no 'err_{code}'")
    return found
