# solea_api/routes/infos_stage_solea.py
from flask import Blueprint, jsonify, request
import re
import unicodedata
from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, classify_type, ddmmyyyy_to_spoken, fmt_date,
    infer_school_year_for_month, parse_date_any,
    cache_key, cache_get, cache_set, cache_meta
)

bp = Blueprint("infos_stage_solea", __name__)
SRC = "https://www.centresolea.org/stages"

KEYWORDS = re.compile(r"(?i)\b(stages?|atelier[s]?|master\s*-?\s*class|masterclass)\b")
MONTH_WORD = r"(janv\.?|janvier|févr\.?|fevr\.?|février|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"

RE_RANGE  = re.compile(rf"(?i)\bdu\s+(\d{{1,2}})\s+au\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_DUO    = re.compile(rf"(?i)\b(\d{{1,2}})\s+et\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_SINGLE = re.compile(rf"(?i)\b(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?\b")
RE_NUM    = re.compile(r"(?i)\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b")

RE_PRICE_ANY = re.compile(r"€")
RE_TARIF_CAT = re.compile(r"(?i)\b(adh[ée]rents?|non\s*adh[ée]rents?|[ée]l[eè]ves?)\b[^0-9]{0,20}([0-9 ][0-9 ]*)\s*€")


# ---------- Helpers robustes ----------
def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def month_to_int_any_fr(m) -> int:
    """
    Convertit n'importe quelle forme FR de mois (abrégé/long, avec/ sans point, avec/ sans accents)
    en entier 1..12. Lève ValueError si inconnu.
    """
    if isinstance(m, int):
        if 1 <= m <= 12:
            return m
        raise ValueError(f"Mois hors plage: {m}")
    s = str(m).strip().lower().replace('.', '')
    s_ascii = _strip_accents(s)

    # ex: "10" → 10
    if s_ascii.isdigit():
        mi = int(s_ascii)
        if 1 <= mi <= 12:
            return mi
        raise ValueError(f"Mois hors plage: {m}")

    # mapping large
    MAP = {
        # jan
        "jan": 1, "janv": 1, "janvier": 1,
        # fev
        "fev": 2, "fevr": 2, "fevrier": 2, "fevri": 2, "fe": 2, "fevrie": 2,  # tolérances
        # mar
        "mar": 3, "mars": 3,
        # avr
        "avr": 4, "avril": 4,
        # mai
        "mai": 5,
        # juin/juil
        "juin": 6,
        "juil": 7, "juillet": 7,
        # aout/août
        "aout": 8, "aou": 8, "aoutre": 8, "aouut": 8, "aoutt": 8,  # tolérances
        # sept
        "sep": 9, "sept": 9, "septembre": 9,
        # oct
        "oct": 10, "octobre": 10,
        # nov
        "nov": 11, "novembre": 11,
        # dec/déc
        "dec": 12, "decembre": 12, "de": 12, "décembre": 12  # "décembre" restera si accents non retirés
    }

    # formes exactes courantes après strip accents & points
    COMMON = {
        "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
        "juin": 6, "juillet": 7, "aout": 8, "septembre": 9,
        "octobre": 10, "novembre": 11, "decembre": 12
    }

    if s_ascii in COMMON:
        return COMMON[s_ascii]
    if s_ascii in MAP:
        return MAP[s_ascii]

    # essais sur les 3 premières lettres pour cas du style "jan", "oct"
    if len(s_ascii) >= 3 and s_ascii[:3] in MAP:
        return MAP[s_ascii[:3]]

    raise ValueError(f"Mois invalide: {m!r}")

def _ensure_day_int(d):
    return int(str(d).strip())

def safe_fmt_date(y, m, d):
    """Appelle fmt_date avec mois/jour garantis en int."""
    mi = month_to_int_any_fr(m)
    di = _ensure_day_int(d)
    return fmt_date(y, mi, di)


def detect_date_block(s: str):
    # du X au Y mois [année]
    m = RE_RANGE.search(s)
    if m:
        d1, d2 = m.group(1), m.group(2)
        mon_txt = m.group(3)  # ex: 'oct', 'oct.', 'octobre'
        y = m.group(4)
        y = int(y) if y else None
        return safe_fmt_date(y, mon_txt, d1), safe_fmt_date(y, mon_txt, d2)

    # "12 et 13 mois [année]"
    m = RE_DUO.search(s)
    if m:
        d1, d2 = m.group(1), m.group(2)
        mon_txt = m.group(3)
        y = m.group(4)
        y = int(y) if y else None
        return safe_fmt_date(y, mon_txt, d1), safe_fmt_date(y, mon_txt, d2)

    # "12 mois [année?]"
    m = RE_SINGLE.search(s)
    if m:
        d = m.group(1)
        mon_txt = m.group(2)
        y = m.group(3)
        y = int(y) if y else None
        return safe_fmt_date(y, mon_txt, d), ""

    # "12/10[/2024]" etc.
    m = RE_NUM.search(s)
    if m:
        d, mo, yy = m.group(1), m.group(2), m.group(3)
        y = int(yy) if yy else None
        if y is not None and y < 100:
            y += 2000
        return safe_fmt_date(y, int(mo), d), ""

    return "", ""


@bp.get("/infos-stage-solea")
def infos_stage_solea():
    key = cache_key("infos-stage-solea", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        raw_lines = []
        for el in soup.find_all(["p", "li", "h3", "h4", "div", "span"]):
            t = normalize_text(el.get_text(" ", strip=True))
            if t:
                raw_lines.append(t)

        items = []
        tariffs = {"adherent": [], "non_adherent": [], "eleve": [], "reduit": [], "autre": []}
        current_event = None

        for line in raw_lines:
            # tarifs (agrégés, ne créent pas d'item)
            if RE_PRICE_ANY.search(line):
                for cat, prix in RE_TARIF_CAT.findall(line):
                    p = f"{prix.replace(' ', '')}€"
                    cl = cat.lower()
                    if "non" in cl and "adh" in cl:
                        if p not in tariffs["non_adherent"]:
                            tariffs["non_adherent"].append(p)
                    elif "adh" in cl:
                        if p not in tariffs["adherent"]:
                            tariffs["adherent"].append(p)
                    elif "lèv" in cl or "élè" in cl or "eleve" in cl:
                        if p not in tariffs["eleve"]:
                            tariffs["eleve"].append(p)
                    else:
                        if p not in tariffs["autre"]:
                            tariffs["autre"].append(p)

            kw = KEYWORDS.search(line)
            if not kw and not current_event:
                continue

            d1, d2 = detect_date_block(line)
            hr = extract_time_from_text(line)
            typ = classify_type(line)

            if typ not in {"stage", "masterclass", "atelier"}:
                if current_event and hr and not current_event.get("heure"):
                    current_event["heure"] = hr
                if not kw:
                    continue

            titre = sanitize_for_voice(line)

            if not kw and not d1 and not hr:
                continue

            if current_event and not d1 and (hr or typ == current_event["type"]):
                if hr and not current_event.get("heure"):
                    current_event["heure"] = hr
                continue

            if current_event:
                items.append(current_event)
                current_event = None

            if kw or d1:
                current_event = {
                    "type": typ if typ in {"stage", "masterclass", "atelier"} else ("stage" if "stage" in line.lower() else typ),
                    "titre": titre[:240],
                    "date": d1,
                    "date_fin": d2,
                    "date_spoken": (f"du {ddmmyyyy_to_spoken(d1)} au {ddmmyyyy_to_spoken(d2)}" if d1 and d2
                                    else (ddmmyyyy_to_spoken(d1) if d1 else "")),
                    "heure": hr,
                    "heure_vocal": hr.replace("h", " heure").replace("-", " - ") if hr else ""
                }

        if current_event:
            items.append(current_event)

        # nettoyage + dédup
        cleaned = []
        for it in items:
            if it["type"] in {"stage", "masterclass", "atelier"} and (it["date"] or it["date_fin"] or it["heure"]):
                if it.get("date") and it.get("date_fin"):
                    it["date_spoken"] = f"du {ddmmyyyy_to_spoken(it['date'])} au {ddmmyyyy_to_spoken(it['date_fin'])}"
                elif it.get("date") and not it.get("date_spoken"):
                    it["date_spoken"] = ddmmyyyy_to_spoken(it["date"])
                cleaned.append(it)

        seen = set()
        uniq = []
        for it in cleaned:
            keyu = (it.get("type", ""), it.get("titre", "")[:140], it.get("date", ""), it.get("date_fin", ""))
            if keyu in seen:
                continue
            seen.add(keyu)
            uniq.append(it)

        payload = {
            "source": SRC,
            "count": len(uniq),
            "items": uniq,
            "items_vocal": uniq,
            "tarifs": tariffs
        }
        cache_set(key, payload, ttl_seconds=45)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
