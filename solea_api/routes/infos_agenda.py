# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
import time
from datetime import datetime, date, timedelta
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta,
    extract_ldjson_events,  # ← utilisé pour lire les Events intégrés
)

bp = Blueprint("infos_agenda", __name__)
BASE_SRC = "https://www.centresolea.org/agenda"

# --- Mois FR (plein + abréviations usuelles)
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
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12,
}
DAY_WORD = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"

# Plage DANS LE MÊME GRAS
RANGE_RX = re.compile(
    rf"(?i)^\s*(?:du\s+)?(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)"      # jour 1
    rf"(?:\s+([a-zéèêàâîïôöùûç\.]+))?\s*"                          # mois 1 (optionnel)
    rf"(?:\s*(20\d{{2}}))?\s*"                                      # année 1 (optionnelle)
    rf"(?:au|–|—|-)\s+(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)\s+"     # 'au' + jour 2
    rf"([a-zéèêàâîïôöùûç\.]+)\s*"                                  # mois 2 (obligatoire)
    rf"(?:\s*(20\d{{2}}))?\s*$",
    re.UNICODE
)
# Date simple “mots”
SIMPLE_WORD_RX = re.compile(
    rf"(?i)^\s*(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)\s+([a-zéèêàâîïôöùûç\.]+)(?:\s+(20\d{{2}}))?\s*$"
)
# Date simple “numérique”
SIMPLE_NUM_RX = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\s*$", re.IGNORECASE)

# Séparateur “date : titre” dans le même bloc
INLINE_SEP_RX = re.compile(r"\s*(?:[:—–\-]\s+)", re.UNICODE)

def _norm(s: str) -> str:
    return normalize_text(s or "")

def _month(tok: str) -> int:
    t = (tok or "").strip().lower().rstrip(".")
    return MONTHS.get(t, 0)

def _following_text_after(node) -> str:
    """Texte non-gras qui suit le bloc gras (frères du parent), séparateur en tête retiré."""
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
    txt = " ".join([_norm(x) for x in buff if _norm(x)])
    txt = re.sub(r"^\s*[:—–\-]\s*", "", txt)
    return txt.strip()

def _parse_bold_date_exact(bold_txt: str) -> tuple[str, str]:
    """
    Ne lit QUE le contenu du gras et normalise:
      - plage => (start,end)
      - simple => (d,d)
    Renseigne l'année si ET SEULEMENT SI elle apparaît DANS le gras (sur l'une des dates d'une plage, on l’applique aux deux).
    Sinon ('','') → on laissera JSON-LD corriger.
    """
    s = _norm(bold_txt)

    # Plage
    m = RANGE_RX.match(s)
    if m:
        d1_s, m1_s, y1_s, d2_s, m2_s, y2_s = m.groups()
        try:
            d1 = int(re.sub(r"er$", "", d1_s or "", flags=re.IGNORECASE))
            d2 = int(re.sub(r"er$", "", d2_s or "", flags=re.IGNORECASE))
        except Exception:
            return "", ""
        m2 = _month(m2_s)
        m1 = _month(m1_s) if m1_s else m2
        if not (m1 and m2):
            return "", ""
        y = y2_s or y1_s
        if not y:
            return "", ""
        try:
            y = int(y)
            start = date(y, m1, d1); end = date(y, m2, d2)
            if end < start:
                return "", ""
            return f"{start.day:02d}/{start.month:02d}/{start.year}", f"{end.day:02d}/{end.month:02d}/{end.year}"
        except Exception:
            return "", ""

    # Simple mots
    m = SIMPLE_WORD_RX.match(s)
    if m:
        d_s, mon_s, y_s = m.groups()
        try:
            dd = int(re.sub(r"er$", "", d_s or "", flags=re.IGNORECASE))
        except Exception:
            return "", ""
        mm = _month(mon_s)
        if not mm or not y_s:
            return "", ""
        yy = int(y_s)
        d = f"{dd:02d}/{mm:02d}/{yy}"
        return d, d

    # Simple numérique
    m = SIMPLE_NUM_RX.match(s)
    if m:
        dd, mm = int(m.group(1)), int(m.group(2))
        if not (1 <= dd <= 31 and 1 <= mm <= 12):
            return "", ""
        if not m.group(3):
            return "", ""
        yy = int(m.group(3))
        d = f"{dd:02d}/{mm:02d}/{yy}"
        return d, d

    return "", ""

# ------------------- JSON-LD aide au “recadrage” des dates -------------------

def _iso_to_ddmmyyyy(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        _ = date(y, mo, d)
        return f"{d:02d}/{mo:02d}/{y}"
    except Exception:
        return ""

def _best_event_match(desc_lower: str, evs: list[dict]) -> dict | None:
    """
    Associe le bloc gras au bon Event JSON-LD via un score de recouvrement de mots (dans name/description).
    On enlève les mots très courts.
    """
    if not evs:
        return None
    import unicodedata
    def strip_acc(s):
        return "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))

    q_tokens = [t for t in re.findall(r"[a-z0-9]+", strip_acc(desc_lower.lower())) if len(t) >= 3]
    if not q_tokens:
        return None

    best, best_score = None, 0
    for ev in evs:
        name = strip_acc((_norm(ev.get("name"))).lower())
        desc = strip_acc((_norm(ev.get("description"))).lower())
        hay = f"{name} {desc}"
        score = sum(1 for t in q_tokens if t in hay)
        if score > best_score:
            best_score, best = score, ev
    return best if best_score > 0 else None

# ---------------------------------------------------------------------------

@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        # Bypass éventuels caches CDN
        html = fetch_html(f"{BASE_SRC}?cb={int(time.time())}")
        soup = soup_from_html(html)

        # Récupère les Events JSON-LD pour recadrer les dates
        ld_events = extract_ldjson_events(html) or []

        # nœuds en gras
        bold_nodes = list(soup.select("strong, b"))
        for sp in soup.find_all("span"):
            style = (sp.get("style") or "").lower()
            if "font-weight" in style and any(w in style for w in ["700","bold"]):
                bold_nodes.append(sp)

        items, seen = [], set()

        for node in bold_nodes:
            strong_txt = _norm(node.get_text(" ", strip=True))
            if not strong_txt:
                continue

            # Séparer “date : titre” dans le même bloc si présent
            desc_lower = ""
            parts = INLINE_SEP_RX.split(strong_txt, maxsplit=1)
            if len(parts) == 2:
                bold_date_part = parts[0].strip()
                desc_lower = (parts[1] or "").lower()
            else:
                bold_date_part = strong_txt

            # 1) Dates normalisées *strictement* depuis le gras
            date_start, date_end = _parse_bold_date_exact(bold_date_part)

            # 2) Si pas de desc inline -> récupérer le texte non-gras qui suit
            if not desc_lower:
                tail = _following_text_after(node)
                if tail:
                    desc_lower = tail.lower()

            # 3) Si (a) le gras n’a pas d’année, ou (b) on veut corriger une plage,
            #    on tente une *validation* via JSON-LD (name/description proches)
            if ld_events and desc_lower:
                ev = _best_event_match(desc_lower, ld_events)
                if ev:
                    s_iso = ev.get("startDate") or ev.get("start") or ""
                    e_iso = ev.get("endDate") or ev.get("end") or ""
                    s_fix = _iso_to_ddmmyyyy(s_iso)
                    e_fix = _iso_to_ddmmyyyy(e_iso) if e_iso else s_fix
                    # On utilise la date JSON-LD si le gras n’a pas d’année
                    # ou si la plage est incohérente / vide.
                    if not date_start or not date_end:
                        date_start, date_end = s_fix, e_fix

            keyi = (bold_date_part, desc_lower[:220])
            if keyi in seen:
                continue
            seen.add(keyi)

            item = {
                "date_bold": bold_date_part,      # EXACTEMENT ce qui est écrit en gras
                "texte": desc_lower,              # le texte associé en minuscules
                "date_start": date_start,         # normalisé (JSON-LD si dispo, sinon gras)
                "date_end": date_end,
            }
            # confort “parlé” si c’est une date simple
            if date_start and date_end and date_start == date_end:
                item["date_spoken"] = ddmmyyyy_to_spoken(date_start)
            else:
                item["date_spoken"] = ""

            items.append(item)

        # Tri : si date_start présente → tri chrono, sinon ordre d’apparition
        def k(e):
            ds = e.get("date_start") or ""
            try:
                d, m, y = ds.split("/")
                return (0, int(y), int(m), int(d))
            except Exception:
                return (1, 9999, 12, 31)
        items.sort(key=k)

        payload = {
            "source": BASE_SRC,
            "count": len(items),
            "evenements": items
        }
        cache_set(key, payload, ttl_seconds=120)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
