# solea_api/routes/infos_stage.py
from flask import Blueprint, jsonify
import re
import requests
from bs4 import BeautifulSoup
from ..utils import normalize_text, ddmmyyyy_to_spoken

bp = Blueprint("infos_stage", __name__)
SRC = "https://www.centresolea.org/stages"

# ------------------ Mois & dates ------------------
MONTH_WORD = r"(janv\.?|janvier|févr\.?|fevr\.?|février|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"
RE_RANGE  = re.compile(rf"(?i)\bdu\s+(\d{{1,2}})\s+au\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_DUO    = re.compile(rf"(?i)\b(\d{{1,2}})\s+et\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_SINGLE = re.compile(rf"(?i)\b(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?\b")
RE_NUM    = re.compile(r"(?i)\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b")

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "janv.": 1, "févr": 2, "févr.": 2, "fevr": 2, "fevr.": 2,
    "sept": 9, "sept.": 9, "oct": 10, "oct.": 10, "nov": 11, "nov.": 11,
    "déc": 12, "déc.": 12, "dec": 12, "dec.": 12
}

def month_to_int_any(m) -> int | None:
    if m is None:
        return None
    if isinstance(m, int):
        return m
    s = str(m).strip().lower().rstrip(".")
    if s.isdigit():
        try:
            v = int(s)
            return v if 1 <= v <= 12 else None
        except Exception:
            return None
    return MONTHS_FR.get(s)

def fmt_date(y, m, d) -> str:
    """Formate dd/mm/yyyy (tolérant mois texte)."""
    mi = month_to_int_any(m)
    if mi is None:
        return ""
    try:
        di = int(d)
    except Exception:
        return ""
    yi = int(y) if y else None
    # si pas d'année, on ne met rien (ou tu peux inférer ton année scolaire)
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
        if y is not None and y < 100:
            y += 2000
        return fmt_date(y, int(mo), d), ""

    return "", ""

# ------------------ Types & tarifs ------------------
KEYWORDS = re.compile(r"(?i)\b(stages?|atelier[s]?|atelier\s+d[’']immersion|master\s*-?\s*class|masterclass)\b")
def classify_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"master\s*-?\s*class|masterclass", t):
        return "master class"
    if re.search(r"atelier\s+d[’']immersion|immersion", t):
        return "atelier d'immersion"
    if re.search(r"\batelier[s]?\b", t):
        return "atelier"
    if re.search(r"\bstage[s]?\b", t):
        return "stage"
    return "evenement"

RE_PRICE_ANY = re.compile(r"€")
RE_TARIF_CAT = re.compile(r"(?i)\b(adh[ée]rents?|non\s*adh[ée]rents?|[ée]l[eè]ves?|élèves?|eleves?)\b[^0-9]{0,30}([0-9 ][0-9 ]*)\s*€")

# ------------------ Prononciation "jota" pour le TTS ------------------
SPANISH_J_WORDS = [
    # mots courants flamenco / espagnol
    "jaleo","jaleos","jaleo(s)?",
    "cajon","cajón","cajones",
    "jesus","jesús","jose","josé","juan","jota","jerez",
]
def jotaize(word: str) -> str:
    """Remplace J espagnol par une approximation TTS FR (kh)."""
    # simple heuristique: si mot est dans la liste (sans accents, lowercase) → J→Kh
    base = (
        word.lower()
        .replace("á","a").replace("é","e").replace("í","i")
        .replace("ó","o").replace("ú","u").replace("ü","u")
        .replace("ñ","n")
    )
    if any(base.startswith(w.replace("ó","o").replace("é","e")) for w in SPANISH_J_WORDS):
        # remplace j par kh
        out = []
        for ch in word:
            if ch in ("j","J"):
                out.append("kh" if ch=="j" else "Kh")
            else:
                out.append(ch)
        return "".join(out)
    return word

def tts_spanish_j(text: str) -> str:
    # remplace mot à mot (en gardant ponctuation)
    tokens = re.split(r"(\W+)", text or "")
    return "".join(jotaize(tok) if re.match(r"\w+", tok) else tok for tok in tokens)

# ------------------ Extraction principale ------------------
def extract_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","noscript","iframe","svg"]):
        tag.decompose()
    lines = []
    for el in soup.find_all(["h1","h2","h3","h4","p","li","span","div"]):
        t = normalize_text(el.get_text(" ", strip=True))
        if t:
            lines.append(t)
    return lines

@bp.get("/infos-stage-solea")  # tu peux aussi exposer /infos-stage si tu préfères
def infos_stage_solea():
    r = requests.get(SRC, timeout=12)
    r.raise_for_status()
    raw_lines = extract_text_lines(r.text)

    items = []
    tariffs_global = {"adherent": [], "non_adherent": [], "eleve": [], "autre": []}
    current = None

    for line in raw_lines:
        # tarifs (à la volée, global + si event ouvert)
        if RE_PRICE_ANY.search(line):
            for cat, prix in RE_TARIF_CAT.findall(line):
                p = f"{prix.replace(' ', '')}€"
                cl = cat.lower()
                bucket = "autre"
                if "non" in cl and "adh" in cl:
                    bucket = "non_adherent"
                elif "adh" in cl:
                    bucket = "adherent"
                elif "lèv" in cl or "élè" in cl or "eleve" in cl or "élèves" in cl:
                    bucket = "eleve"
                if p not in tariffs_global[bucket]:
                    tariffs_global[bucket].append(p)
                if current is not None:
                    current.setdefault("tarifs", {}).setdefault(bucket, [])
                    if p not in current["tarifs"][bucket]:
                        current["tarifs"][bucket].append(p)

        # dates
        d1, d2 = detect_date_block(line)
        # type
        typ = classify_type(line)
        kw = KEYWORDS.search(line)

        # si on tombe sur un nouveau bloc “titre”
        if kw or d1:
            if current:
                items.append(current)
            titre = line
            current = {
                "type": typ if typ != "evenement" else ("stage" if "stage" in line.lower() else typ),
                "titre": titre,
                "titre_vocal": tts_spanish_j(titre),
                "date": d1,
                "date_fin": d2,
                "date_spoken": (f"du {ddmmyyyy_to_spoken(d1)} au {ddmmyyyy_to_spoken(d2)}" if d1 and d2
                                else (ddmmyyyy_to_spoken(d1) if d1 else "")),
                "tarifs": {}
            }
            continue

        # si c'est un texte explicatif qui suit un item ouvert
        if current and not (kw or d1):
            # on ajoute un extrait de description (limité)
            current.setdefault("description", "")
            if len(current["description"]) < 800:
                current["description"] = normalize_text((current["description"] + " " + line).strip())

    if current:
        items.append(current)

    # nettoyage basique
    cleaned = []
    seen = set()
    for it in items:
        keyu = (it.get("type",""), it.get("titre","")[:140], it.get("date",""), it.get("date_fin",""))
        if keyu in seen:
            continue
        seen.add(keyu)
        # si pas de type mais titre contient stage/masterclass/atelier → corrige
        if it.get("type") in (None, "", "evenement"):
            it["type"] = classify_type(it.get("titre",""))
        cleaned.append(it)

    payload = {
        "source": SRC,
        "count": len(cleaned),
        "items": cleaned,
        "tarifs_global": tariffs_global
    }
    return jsonify(payload)
