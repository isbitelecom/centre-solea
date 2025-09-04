# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, timedelta, datetime
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    parse_date_any, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta
)

bp = Blueprint("infos_agenda", __name__)
SRC = "https://www.centresolea.org/agenda"

# --- Séparateurs dans le même bloc (date : titre)
INLINE_SEP_RX = re.compile(r"\s*(?:[:—–\-]\s+)", re.UNICODE)

# --- Mois FR (plein + quelques abréviations)
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
DAY_WORD = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"

# Plage dans le même bloc (ex: "Du lundi 27 au vendredi 31 octobre 2025")
RANGE_RX = re.compile(
    rf"(?i)^\s*(?:du\s+)?(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)"               # jour 1
    rf"(?:\s+([a-zéèêàâîïôöùûç\.]+))?\s*"                                   # mois 1 (optionnel)
    rf"(?:\s+(?:,?\s*20\d{{2}}))?\s*"                                       # année 1 (optionnelle, ignorée)
    rf"(?:au|-|—|–)\s+(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)\s+"              # 'au' + jour 2
    rf"([a-zéèêàâîïôöùûç\.]+)\s*"                                           # mois 2 (obligatoire)
    rf"(?:,?\s*(20\d{{2}}))?\s*$"                                           # année 2 (optionnelle)
)

# Date simple
LOOKS_LIKE_DATE_RX = re.compile(
    rf"(?i)^\s*(?:du|le|les)?\s*(?:{DAY_WORD}\s+)?\d{{1,2}}(?:er)?\s+[a-zéèêàâîïôöùûç\.]+(?:\s+20\d{{2}})?\s*$"
)

def month_to_int(tok: str) -> int:
    t = (tok or "").strip().lower().rstrip(".")
    return MONTHS.get(t, 0)

def expand_range(d1: int, m1: int, y1: int, d2: int, m2: int, y2: int) -> list[str]:
    try:
        start = date(y1, m1, d1)
        end   = date(y2, m2, d2)
        if end < start:
            return []
    except Exception:
        return []
    out, cur = [], start
    while cur <= end:
        out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
        cur += timedelta(days=1)
    return out

def parse_range_inline(date_part: str) -> list[str]:
    """Parse une plage '... 26 (mois?) au ... 30 octobre [2025]' et renvoie une liste de dd/mm/yyyy."""
    m = RANGE_RX.match(normalize_text(date_part))
    if not m:
        return []
    d1_s, m1_s, d2_s, m2_s, y2_s = m.groups()
    # jours
    try:
        d1 = int(re.sub(r"er$", "", d1_s or "", flags=re.IGNORECASE))
        d2 = int(re.sub(r"er$", "", d2_s or "", flags=re.IGNORECASE))
    except Exception:
        return []
    # mois
    m2 = month_to_int(m2_s)
    m1 = month_to_int(m1_s) if m1_s else m2
    if not (m1 and m2):
        return []
    # année
    if y2_s:
        y2 = int(y2_s)
    else:
        y2 = datetime.now().year
    y1 = y2  # même année dans 99% des cas sur l'agenda
    return expand_range(d1, m1, y1, d2, m2, y2)

def following_text_after(node) -> str:
    """Texte non-gras qui suit le bloc gras (frères du parent), séparateurs retirés."""
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


@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # Récupère les nœuds "gras" (Wix peut utiliser <span style="font-weight:700">)
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

            dates = []      # 0..N dates (si plage, on étend)
            desc_lower = "" # texte associé (minuscules)

            # 1) Cas "date : titre" DANS le même bloc gras
            parts = INLINE_SEP_RX.split(strong_txt, maxsplit=1)
            if len(parts) == 2:
                date_part, desc_part = parts[0].strip(), parts[1].strip()

                # 1.a) plage à l'intérieur du bloc
                dates = parse_range_inline(date_part)

                # 1.b) sinon, date simple
                if not dates and LOOKS_LIKE_DATE_RX.match(date_part):
                    ddmmyyyy = parse_date_any(date_part)
                    if ddmmyyyy:
                        dates = [ddmmyyyy]

                desc_lower = (desc_part or "").lower()

            # 2) Sinon (le bloc gras NE contient que la date)
            if not dates:
                # Essayer une plage complète même sans ':'
                dates = parse_range_inline(strong_txt)

            if not dates:
                # Essayer date simple
                ddmmyyyy = parse_date_any(strong_txt)
                if ddmmyyyy:
                    dates = [ddmmyyyy]

            # 3) Si pas de desc (date seule en gras), récupérer le texte qui suit (non-gras)
            if dates and not desc_lower:
                tail = following_text_after(node)
                if tail:
                    desc_lower = tail.lower()

            # 4) Rien de valide trouvé -> continuer
            if not dates:
                continue

            # 5) Émettre une entrée **par jour** (si plage)
            for dd in dates:
                keyi = (dd, desc_lower[:200])
                if keyi in seen:
                    continue
                seen.add(keyi)
                items.append({
                    "date": dd,
                    "date_spoken": ddmmyyyy_to_spoken(dd),
                    "texte": desc_lower,
                    "source_bloc": strong_txt  # utile en debug; supprime si tu ne veux pas l'exposer
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
        cache_set(key, payload, ttl_seconds=90)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
