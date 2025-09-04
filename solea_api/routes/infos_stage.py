# solea_api/routes/infos_stage.py
from flask import Blueprint, jsonify
import re
import requests
from bs4 import BeautifulSoup
from ..utils import normalize_text, ddmmyyyy_to_spoken

bp = Blueprint("infos_stage", __name__)
SRC = "https://www.centresolea.org/stages"

# ----------- Regex & helpers -----------
MONTH_WORD = r"(janv\.?|janvier|févr\.?|fevr\.?|février|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"
RE_RANGE  = re.compile(rf"(?i)\bdu\s+(\d{{1,2}})\s+au\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_DUO    = re.compile(rf"(?i)\b(\d{{1,2}})\s+et\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_SINGLE = re.compile(rf"(?i)\b(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?\b")
RE_NUM    = re.compile(r"(?i)\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b")
RE_ANYDATE = re.compile(rf"(?i)\b(?:du\s+\d{{1,2}}\s+au\s+\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?|\d{{1,2}}\s+et\s+\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?|\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?|\d{{1,2}}[\/\-.]\d{{1,2}}(?:[\/\-.]\d{{2,4}})?)\b")

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "janv.": 1, "févr": 2, "févr.": 2, "fevr": 2, "fevr.": 2,
    "sept": 9, "sept.": 9, "oct": 10, "oct.": 10, "nov": 11, "nov.": 11,
    "déc": 12, "déc.": 12, "dec": 12, "dec.": 12
}

KEYWORDS = re.compile(r"(?i)\b(master\s*-?\s*class|masterclass|stage[s]?|atelier\s+d[’']immersion|atelier[s]?)\b")
RE_PRICE_ANY = re.compile(r"€")
RE_TARIF_LINE = re.compile(r"(?i)\b(adh[ée]rents?|non\s*adh[ée]rents?|[ée]l[eè]ves?|élèves?|eleves?)\b.*?\d+\s*€")

# Heures → forme vocale (“20h00” → “20 heures”, “16h30” → “16 heures 30”)
RE_HOUR = re.compile(r"\b(\d{1,2})\s*[h:]\s*([0-5]?\d)?\b")
def heure_vocale(s: str) -> str:
    def repl(m):
        h = int(m.group(1))
        mn = m.group(2)
        if not mn or re.fullmatch(r"0+", mn):
            return f"{h} heures"
        return f"{h} heures {int(mn)}"
    return RE_HOUR.sub(repl, s)

def month_to_int_any(m):
    if m is None: return None
    if isinstance(m, int): return m
    s = str(m).strip().lower().rstrip(".")
    if s.isdigit():
        v = int(s)
        return v if 1 <= v <= 12 else None
    return MONTHS_FR.get(s)

def fmt_date(y, m, d) -> str:
    mi = month_to_int_any(m)
    if not mi: return ""
    try: di = int(d)
    except Exception: return ""
    yi = int(y) if y else None
    return f"{di:02d}/{mi:02d}/{(yi if yi else 0):04d}".replace("/0000", "")

def detect_date_block(s: str):
    s = s or ""
    m = RE_RANGE.search(s)
    if m:
        d1, d2, mon, y = m.group(1), m.group(2), m.group(3), m.group(4)
        y = int(y) if y else None
        return fmt_date(y, mon, d1), fmt_date(y, mon, d2)
    m = RE_DUO.search(s)
    if m:
        d1, d2, mon, y = m.group(1), m.group(2), m.group(3), m.group(4)
        y = int(y) if y else None
        return fmt_date(y, mon, d1), fmt_date(y, mon, d2)
    m = RE_SINGLE.search(s)
    if m:
        d, mon, y = m.group(1), m.group(2), m.group(3)
        y = int(y) if y else None
        return fmt_date(y, mon, d), ""
    m = RE_NUM.search(s)
    if m:
        d, mo, yy = m.group(1), m.group(2), m.group(3)
        y = int(yy) if yy else None
        if y is not None and y < 100: y += 2000
        return fmt_date(y, int(mo), d), ""
    return "", ""

# Lignes “bruit” (menus, footer, gros headers)
RE_NOISE = re.compile(r"(?i)^(l'?ecole|les cours|horaires(?: et tarifs)?|tarifs$|le lieu|infos ?/ ?contact|newsletter|suivez(-| )?nous|agenda 20|événements? ?(à|a) venir)$")
def is_noise(line: str) -> bool:
    if not line: return True
    if RE_NOISE.match(line): return True
    # lignes tout en MAJ courtes (menus)
    if len(line) <= 30 and re.fullmatch(r"[A-ZÉÈÀÙÂÊÎÔÛÄËÏÖÜÇ0-9 \-'/]+", line):
        return True
    return False

def extract_lines(html: str):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","noscript","iframe","svg"]):
        tag.decompose()
    lines = []
    for el in soup.find_all(["h1","h2","h3","h4","p","li","span","div"]):
        t = normalize_text(el.get_text(" ", strip=True))
        if t:
            lines.append(t)
    # dédoublonner les suites identiques
    dedup = []
    prev = None
    for t in lines:
        if t != prev:
            dedup.append(t)
        prev = t
    return dedup

def strip_date_from(text: str) -> str:
    """retire la première occurrence d'une date de la chaîne."""
    m = RE_ANYDATE.search(text or "")
    if not m: return text
    out = (text[:m.start()] + text[m.end():]).strip(" ,;:.-")
    return normalize_text(out)

def classify_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"master\s*-?\s*class|masterclass", t): return "master class"
    if re.search(r"atelier\s+d[’']immersion|immersion", t): return "atelier d'immersion"
    if re.search(r"\batelier[s]?\b", t): return "atelier"
    if re.search(r"\bstage[s]?\b", t): return "stage"
    return "evenement"

# ----------- Endpoint -----------
@bp.get("/infos-stage")
def infos_stage():
    r = requests.get(SRC, timeout=12)
    r.raise_for_status()
    lines = extract_lines(r.text)

    items = []
    current = None

    for raw in lines:
        line = raw.strip()
        if is_noise(line):
            continue

        # Nouveau bloc si mot-clé ou date détectée
        looks_like_header = KEYWORDS.search(line) or RE_ANYDATE.search(line)
        if looks_like_header:
            # finaliser le précédent s'il a du contenu réel
            if current and (current.get("titre") or current.get("date") or current.get("tarifs")):
                items.append(current)

            d1, d2 = detect_date_block(line)
            titre = strip_date_from(line)
            typ = classify_type(line if titre == "" else titre)

            current = {
                "type": typ if typ != "evenement" else ("stage" if "stage" in line.lower() else typ),
                "titre": titre if titre else None,
                "date": d1,
                "date_fin": d2,
                "date_spoken": (f"du {ddmmyyyy_to_spoken(d1)} au {ddmmyyyy_to_spoken(d2)}" if d1 and d2
                                else (ddmmyyyy_to_spoken(d1) if d1 else "")),
                "heures": [],
                "tarifs": [],
                "description": ""
            }
            continue

        # Si on n'est pas dans un bloc en cours, on ignore
        if not current:
            continue

        # Heures → vocal
        if re.search(r"\d{1,2}\s*[h:]\s*[0-5]?\d?", line):
            hv = heure_vocale(line)
            if hv not in current["heures"]:
                current["heures"].append(hv)
            continue

        # Tarifs (lignes avec € et “adhérents / non-adhérents / élèves”)
        if RE_TARIF_LINE.search(line) or RE_PRICE_ANY.search(line):
            if line not in current["tarifs"]:
                current["tarifs"].append(line)
            continue

        # Sinon, c'est de la description utile (mais pas les sous-menus)
        if not is_noise(line):
            if current["description"]:
                # éviter la boursouflure
                if len(current["description"]) < 800:
                    current["description"] += " " + line
            else:
                current["description"] = line

    # push final
    if current and (current.get("titre") or current.get("date") or current.get("tarifs")):
        items.append(current)

    # Nettoyage: retirer items trop vides, et ceux qui n'ont ni date ni type utile
    cleaned = []
    seen = set()
    for it in items:
        # eliminer vrais fantômes
        if not any([it.get("titre"), it.get("date"), it.get("tarifs"), it.get("heures")]):
            continue
        key = (it.get("type",""), (it.get("titre") or "")[:140], it.get("date",""), it.get("date_fin",""))
        if key in seen:
            continue
        seen.add(key)

        # si titre est vide mais on a une description courte → promouvoir début de description
        if not it.get("titre") and it.get("description"):
            it["titre"] = it["description"][:120]

        cleaned.append(it)

    return jsonify({
        "source": SRC,
        "count": len(cleaned),
        "items": cleaned
    })
