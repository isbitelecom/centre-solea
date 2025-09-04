# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, timedelta, datetime
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta
)

bp = Blueprint("infos_agenda", __name__)
SRC = "https://www.centresolea.org/agenda"

# ---------------------------------------------------------------------------
# Mois FR (plein + abréviations)
MONTHS = {
    "janvier": 1, "janv": 1, "jan": 1,
    "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "fév": 2, "fev": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "août": 8, "aout": 8, "aou": 8,
    "septembre": 9, "sept": 9, "sep": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12
}

DAY_WORDS = {
    "lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche",
    "lun.","mar.","mer.","jeu.","ven.","sam.","dim.",
    "lun","mar","mer","jeu","ven","sam","dim"
}

INLINE_SEP_RX = re.compile(r"\s*(?:[:—–\-]\s+)", re.UNICODE)  # sépare "date : titre"

RX_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b", re.IGNORECASE)
RX_YEAR = re.compile(r"\b(20\d{2})\b")

# ---------------------------------------------------------------------------
# Helpers parsing robustes

def _year_default(mm: int | None) -> int:
    # défaut simple: année courante
    return datetime.now().year

def _month_from_token(tok: str) -> int:
    t = (tok or "").strip().lower().rstrip(".")
    return MONTHS.get(t, 0)

def _is_day_word(tok: str) -> bool:
    return (tok or "").strip().lower().rstrip(".") in DAY_WORDS

def _expand_range(d1: int, m1: int, y1: int, d2: int, m2: int, y2: int) -> list[str]:
    try:
        start = date(y1, m1, d1)
        end = date(y2, m2, d2)
        if end < start:
            return []
    except Exception:
        return []
    out, cur = [], start
    while cur <= end:
        out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
        cur += timedelta(days=1)
    return out

def _tokenize(s: str) -> list[str]:
    # tokens alphanum (garde les points finaux style "sept.")
    s = normalize_text(s or "")
    s = s.replace("–", " au ").replace("—", " au ").replace("-", " au ")
    return re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9\.]+", s)

def _parse_range_smart(date_part: str) -> list[str]:
    """
    Parse des formes :
      - "Du lundi 27 au vendredi 31 octobre [2025]"
      - "Dimanche 26 au jeudi 30 octobre[: ...]"
      - "27 au 31 oct. 2025"
    Mois obligatoire sur la seconde date, le premier hérite sinon.
    Année : si absente → année courante.
    """
    toks = _tokenize(date_part)
    if not toks:
        return []

    # repérer 'au' comme séparateur principal
    try:
        i_sep = [i for i,t in enumerate(toks) if t.lower() == "au"][0]
    except IndexError:
        return []

    # zone gauche (avant 'au') : trouver dernier jour et éventuellement un mois juste après
    left = toks[:i_sep]
    right = toks[i_sep+1:]

    # jour gauche = dernier nombre dans left
    d1 = None; m1 = None
    for i in range(len(left)-1, -1, -1):
        t = left[i]
        if t.isdigit():
            try:
                d = int(t)
                if 1 <= d <= 31:
                    d1 = d
                    # si le token juste après est un mois → m1
                    if i+1 < len(left):
                        m1 = _month_from_token(left[i+1])
                    break
            except Exception:
                pass

    # droite : premier nombre = d2, puis le mois m2 (le premier mois après d2)
    d2 = None; m2 = None; y2 = None
    for i, t in enumerate(right):
        if t.isdigit():
            try:
                d = int(t)
                if 1 <= d <= 31:
                    d2 = d
                    # chercher un mois dans les 3 tokens suivants
                    for j in range(i+1, min(i+4, len(right))):
                        mm = _month_from_token(right[j])
                        if mm:
                            m2 = mm
                            # année explicite si présente juste après
                            if j+1 < len(right) and RX_YEAR.match(right[j+1] or ""):
                                y2 = int(RX_YEAR.match(right[j+1]).group(1))
                            break
                    break
            except Exception:
                pass

    if d1 is None or d2 is None or m2 is None:
        return []

    if m1 is None:
        m1 = m2

    if y2 is None:
        y2 = _year_default(m2)
    y1 = y2

    return _expand_range(d1, m1, y1, d2, m2, y2)

def _parse_simple_smart(s: str) -> str:
    """
    Dates simples possibles :
      - "Samedi 27 septembre 2025"
      - "sam. 27 sept."
      - "27/09[/2025]"
    Retourne "dd/mm/yyyy" ou "".
    """
    s = normalize_text(s or "")

    # 1) numérique
    m = RX_NUM.search(s)
    if m:
        try:
            dd = int(m.group(1)); mm = int(m.group(2))
            yy = int(m.group(3)) if m.group(3) else _year_default(mm)
            if 1 <= dd <= 31 and 1 <= mm <= 12:
                return f"{dd:02d}/{mm:02d}/{yy}"
        except Exception:
            pass

    # 2) mots ("sam. 27 sept." / "27 septembre 2025" / avec jour)
    toks = _tokenize(s)
    # chercher un motif jourNumero + mois
    for i, t in enumerate(toks):
        if t.isdigit():
            try:
                dd = int(t)
                if 1 <= dd <= 31:
                    # mois peut être juste après ou à +1 (si un jour de la semaine est intercalé déjà pris en compte)
                    for j in (i+1, i+2):
                        if j < len(toks):
                            mm = _month_from_token(toks[j])
                            if mm:
                                # année éventuelle
                                yy = None
                                if j+1 < len(toks) and RX_YEAR.match(toks[j+1] or ""):
                                    yy = int(RX_YEAR.match(toks[j+1]).group(1))
                                if yy is None:
                                    yy = _year_default(mm)
                                return f"{dd:02d}/{mm:02d}/{yy}"
            except Exception:
                pass
    return ""

# ---------------------------------------------------------------------------
# Récupération du texte après le bloc gras (non-gras)
def following_text_after(node) -> str:
    buff = []
    if node.next_sibling and isinstance(node.next_sibling, NavigableString):
        buff.append(str(node.next_sibling))
    parent = node.parent or node
    for sib in parent.next_siblings:
        nm = (getattr(sib, "name", "") or "").lower()
        if nm in {"strong", "b", "h1", "h2", "h3", "hr"}:
            break
        if nm == "br":
            buff.append("\n"); continue
        if hasattr(sib, "get_text"):
            for t in sib.find_all(["strong", "b"]):
                t.decompose()
            buff.append(sib.get_text(" ", strip=True)); continue
        if isinstance(sib, NavigableString):
            buff.append(str(sib))
    txt = " ".join([normalize_text(x) for x in buff if normalize_text(x)])
    txt = re.sub(r"^\s*[:—–\-]\s*", "", txt)
    return txt.strip()

# ---------------------------------------------------------------------------

@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # nœuds "gras" (inclut <span style="font-weight:700">)
        bold_nodes = list(soup.select("strong, b"))
        for sp in soup.find_all("span"):
            style = (sp.get("style") or "").lower()
            if "font-weight" in style and any(w in style for w in ["700","bold"]):
                bold_nodes.append(sp)

        items, seen = [], set()

        for node in bold_nodes:
            strong_txt = normalize_text(node.get_text(" ", strip=True))
            if not strong_txt:
                continue

            dates: list[str] = []
            desc_lower = ""

            # Cas "date : titre" dans le même bloc
            parts = INLINE_SEP_RX.split(strong_txt, maxsplit=1)
            if len(parts) == 2:
                date_part, desc_part = parts[0].strip(), parts[1].strip()
                # 1) tenter plage
                dates = _parse_range_smart(date_part)
                # 2) sinon, date simple
                if not dates:
                    d = _parse_simple_smart(date_part)
                    if d:
                        dates = [d]
                desc_lower = (desc_part or "").lower()

            # Si pas déterminé via split, essayer avec tout le bloc
            if not dates:
                # plage
                dates = _parse_range_smart(strong_txt)
            if not dates:
                # simple
                d = _parse_simple_smart(strong_txt)
                if d:
                    dates = [d]

            # Si date mais pas de description, prendre le texte non-gras qui suit
            if dates and not desc_lower:
                tail = following_text_after(node)
                if tail:
                    desc_lower = tail.lower()

            if not dates:
                continue

            # Émettre une entrée par date
            for dd in dates:
                keyi = (dd, desc_lower[:200])
                if keyi in seen:
                    continue
                seen.add(keyi)
                items.append({
                    "date": dd,
                    "date_spoken": ddmmyyyy_to_spoken(dd),
                    "texte": desc_lower,
                    "source_bloc": strong_txt  # utile au debug; supprime si tu ne veux pas l'exposer
                })

        # Tri par date croissante
        def k(e):
            try:
                d, m, y = e["date"].split("/")
                return (int(y), int(m), int(d))
            except Exception:
                return (9999, 12, 31)
        items.sort(key=k)

        payload = {
            "source": SRC,
            "count": len(items),
            "evenements": items
        }
        cache_set(key, payload, ttl_seconds=120)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
