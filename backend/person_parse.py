"""Parsing of legacy free-text person names into structured fields, plus the
shared display-name computation and relation vocabulary.

Used once by the Phase-1 migration in :mod:`backend.db` and reused by the API
when a person is created from a typed name.
"""
import re
import unicodedata

# Relation noun (lowercased) -> canonical English preset key.
RELATION_WORDS = {
    "māte": "mother", "mamma": "mother", "mammu": "mother",
    "tēvs": "father", "tētis": "father", "tēts": "father",
    "māsa": "sister",
    "brālis": "brother", "brāļa": "brother",
    "vīrs": "husband", "sieva": "wife",
    "meita": "daughter", "dēls": "son",
    "vecmāte": "grandmother", "vectēvs": "grandfather",
    "draugs": "friend", "draudzene": "friend",
}

# Built-in relation types offered in the UI (plus the "custom" sentinel).
RELATION_PRESETS = [
    "son", "daughter", "father", "mother", "husband", "wife",
    "brother", "sister", "grandfather", "grandmother", "friend",
]

PROFESSION_STEMS = ["skolotāj", "direktor", "audzinātāj", "pārzin", "pavār", "sanitār"]

_CAP = "A-ZĀČĒĢĪĶĻŅŠŪŽ"
_LOW = r"\wāčēģīķļņšūž"


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def genitive_to_nominative(word: str) -> str:
    """Naive Latvian genitive -> nominative guess, only a hint for linking."""
    for suf, rep in (("as", "a"), ("es", "e"), ("ļa", "lis"), ("a", "s")):
        if word.endswith(suf):
            return word[: -len(suf)] + rep
    return word


def _is_profession(text: str) -> bool:
    low = text.lower()
    return any(stem in low for stem in PROFESSION_STEMS)


def _find_relations(text: str):
    """Yield (preset_key, target_raw, note) for each relation phrase in text."""
    out = []
    for chunk in re.split(r",|\bun\b", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        tokens = chunk.split()
        rel_key = rel_idx = None
        for i, tok in enumerate(tokens):
            low = tok.lower().strip(".,)")
            if low in RELATION_WORDS:
                rel_key, rel_idx = RELATION_WORDS[low], i
                break
        if rel_key is None:
            continue
        target_tokens = [t for t in tokens[:rel_idx] if t[:1].isupper()]
        note_tokens = tokens[rel_idx + 1:]
        out.append((rel_key, " ".join(target_tokens), " ".join(note_tokens)))
    return out


def _split_professions(text: str):
    """Return (titles, place) from a profession phrase."""
    place = ""
    m = re.search(rf"\b([{_CAP}][{_LOW}]+(?:os|ās))\b", text)
    if m:
        place = m.group(1)
        text = text.replace(place, "").strip()
    titles = []
    for chunk in re.split(r",|\bun\b", text):
        chunk = chunk.strip(" .,")
        if chunk and _is_profession(chunk):
            titles.append(chunk)
    return titles, place


def parse_name(raw: str) -> dict:
    """Parse a legacy name string into structured parts.

    Returns a dict with keys: given (str), surnames (list of (value, kind)),
    professions (list of (title, place, start_year, end_year, note)),
    relations (list of (kind, target_raw, target_guess, note)),
    notes (list of str), placeholder (bool).
    """
    r = {
        "given": "",
        "surnames": [],
        "professions": [],
        "relations": [],
        "notes": [],
        "placeholder": False,
    }
    s = (raw or "").strip()

    # 1) quoted nickname(s)
    for m in re.finditer(r'"([^"]+)"', s):
        r["surnames"].append((m.group(1).strip(), "nickname"))
    s = re.sub(r'"[^"]*"', " ", s)

    # 2) parentheticals
    parens = re.findall(r"\(([^)]*)\)", s)
    s_noparen = re.sub(r"\([^)]*\)", " ", s)
    tail_after_paren = bool(re.search(rf"\)[^)]*[{_CAP}]", s))

    for p in parens:
        p = p.strip().strip(")").strip()
        if not p:
            continue
        low = p.lower()
        rels = _find_relations(p)
        if rels:
            for key, target_raw, note in rels:
                guess = " ".join(genitive_to_nominative(t) for t in target_raw.split())
                r["relations"].append((key, target_raw, guess, note))
            continue
        if _is_profession(p):
            titles, place = _split_professions(p)
            for t in titles:
                r["professions"].append((t, place, None, None, ""))
            continue
        if low.startswith("kādreiz") or low.startswith("dzim"):
            r["surnames"].append((p.split()[-1], "maiden"))
            continue
        if re.fullmatch(rf"[{_CAP}][{_LOW}]+", p):
            r["surnames"].append((p, "nickname" if tail_after_paren else "maiden"))
            continue
        r["notes"].append(p)

    # 3) remaining bare tokens (drop punctuation-only leftovers from typos)
    rest = re.sub(r"\s+", " ", s_noparen).strip()
    rest = " ".join(t for t in rest.split() if re.search(rf"[{_CAP}{_LOW}]", t))

    bare_rel = _find_relations(rest)
    rest_tokens = rest.split()
    extra_names = [
        t for t in rest_tokens[1:]
        if t[:1].isupper() and t.lower() not in RELATION_WORDS
    ]
    if bare_rel and not extra_names:
        for key, target_raw, note in bare_rel:
            guess = " ".join(genitive_to_nominative(t) for t in target_raw.split())
            r["relations"].append((key, target_raw, guess, note))
        r["placeholder"] = True
        return r

    if rest and _is_profession(rest) and not (rest_tokens and rest_tokens[0][:1].isupper()
                                               and not _is_profession(rest_tokens[0])):
        titles, place = _split_professions(rest)
        if titles:
            for t in titles:
                r["professions"].append((t, place, None, None, ""))
            r["placeholder"] = True
            return r

    if rest_tokens:
        r["given"] = rest_tokens[0]
        for t in rest_tokens[1:]:
            r["surnames"].append((t, "surname"))
    return r


def compute_display(given: str, names, raw: str = "") -> str:
    """Build a clean display label from structured name parts.

    names is an iterable of (value, kind). Falls back to raw for placeholders
    with neither a given name nor a surname.
    """
    nick = [v for v, k in names if k == "nickname"]
    surn = [v for v, k in names if k == "surname"]
    maiden = [v for v, k in names if k == "maiden"]
    parts = []
    if given:
        parts.append(given)
    parts += [f'"{n}"' for n in nick]
    parts += surn
    disp = " ".join(parts).strip()
    if maiden:
        disp = (disp + " (" + ", ".join(maiden) + ")").strip()
    return disp or (raw or "").strip()
