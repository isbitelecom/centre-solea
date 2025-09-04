# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
from datetime import date
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta
)

bp = Blueprint("infos_agenda", __name__)
SRC = "https://www.centresolea.org/agenda"

# --- Mois FR (plein + abréviations usuelles, en minuscules, sans point final)
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

# Exemple de plage DANS LE MÊME BLOC GRAS :
# "Du lundi 27 au vendredi 31 octobre 2025"
# "Dimanche 26 au jeudi 30 octobre"
RANGE_RX = re.compile(
    rf"(?i)^\s*(?:du\s+)?(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)"      # jour 1
    rf"(?:\s+([a-zéèêàâîïôöùûç\.]+))?\s*"                          # mois 1 (optionnel)
    rf"(?:\s+(20\d{{2}}))?\s*"                                      # année 1 (optionnelle)
    rf"(?:au|–|—|-)\s+(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)\s+"     # 'au' + jour 2
    rf"([a-zéèêàâîïôöùûç\.]+)\s*"                                  # mois 2 (obligatoire)
    rf"(?:\s*(20\d{{2}}))?\s*$",                                    # année 2 (optionnelle)
    re.UNICODE
)

# Date simple DANS LE GRAS :
# "Samedi 27 septembre 2025" / "sam. 27 sept." / "27/09[/2025]"
SIMPLE_WORD_RX = re.compile(
    rf"(?i)^\s*(?:{DAY_WORD}\s+)?(\d{{1,2}}(?:er)?)\s+([a-zéèêàâîïôöùûç\.]+)(?:\s+(20\d{{2}}))?\s*$"
)
SIMPLE_NUM_RX = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\s*$", re.IGNORECASE)

INLINE_SEP_RX = re.compile(r"\s*(?:[:—–\-]\s+)", re.UNICODE)  # pour séparer "date : titre" DANS le même bloc


def _month_from_token(tok: str) -> int:
    t = (tok or "").strip().lower().rstrip(".")
    return MONTHS.get(t, 0)


def _norm_txt(s: str) -> str:
    return normalize_text(s or "")


def _following_text_after(node) -> str:
    """Texte non-gras qui suit le bloc gras (frères du parent), en retirant le séparateur en tête."""
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
    txt = " ".join([_norm_txt(x) for x in buff if _norm_txt(x)])
    txt = re.sub(r"^\s*[:—–\-]\s*", "", txt)
    return txt.strip()


def _parse_bold_date_exact(bold_txt: str):
    """
    Ne regarde QUE le contenu du bloc en gras.
    Retourne (date_start, date_end) normalisées "dd/mm/yyyy" si l'année est explicite dans le gras.
    Sinon, retourne ('','') pour ne pas inventer.
    """
    s = _norm_txt(bold_txt)

    # 1) Plage dans le même bloc
    m = RANGE_RX.match(s)
    if m:
        d1_s, m1_s, y1_s, d2_s, m2_s, y2_s = m.groups()
        # jours
        try:
            d1 = int(re.sub(r"er$", "", d1_s or "", flags=re.IGNORECASE))
            d2 = int(re.sub(r"er$", "", d2_s or "", flags=re.IGNORECASE))
        except Exception:
            return "", ""
        # mois (m1 hérite de m2 si absent)
        m2 = _month_from_token(m2_s)
        m1 = _month_from_token(m1_s) if m1_s else m2
        if not (m1 and m2):
            return "", ""
        # année : on n'utilise QUE ce qui est écrit dans le gras
        y = y2_s or y1_s
        if not y:
            return "", ""
        try:
            y = int(y)
        except Exception:
            return "", ""
        try:
            start = date(y, m1, d1)
            end   = date(y, m2, d2)
            # si l'ordre est incohérent, on ne renvoie rien
            if end < start:
                return "", ""
            return f"{start.day:02d}/{start.month:02d}/{start.year}", f"{end.day:02d}/{end.month:02d}/{end.year}"
        except Exception:
            return "", ""

    # 2) Date simple "mots" dans le même bloc
    m = SIMPLE_WORD_RX.match(s)
    if m:
        d_s, mon_s, y_s = m.groups()
        try:
            dd = int(re.sub(r"er$", "", d_s or "", flags=re.IGNORECASE))
        except Exception:
            return "", ""
        mm = _month_from_token(mon_s)
        if not mm:
            return "", ""
        if not y_s:
            return "", ""   # pas d'année dans le gras → on ne crée pas de yyyy
        try:
            yy = int(y_s)
            return f"{dd:02d}/{mm:02d}/{yy}", f"{dd:02d}/{mm:02d}/{yy}"
        except Exception:
            return "", ""

    # 3) Date simple numérique "dd/mm[/yyyy]" dans le même bloc
    m = SIMPLE_NUM_RX.match(s)
    if m:
        try:
            dd = int(m.group(1)); mm = int(m.group(2))
        except Exception:
            return "", ""
        y_s = m.group(3)
        if not (1 <= dd <= 31 and 1 <= mm <= 12):
            return "", ""
        if not y_s:
            return "", ""   # année non écrite → on ne fabrique pas
        yy = int(y_s)
        return f"{dd:02d}/{mm:02d}/{yy}", f"{dd:02d}/{mm:02d}/{yy}"

    # rien de reconnu
    return "", ""


@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # nœuds en gras (inclut <span style="font-weight:700">)
        bold_nodes = list(soup.select("strong, b"))
        for sp in soup.find_all("span"):
            style = (sp.get("style") or "").lower()
            if "font-weight" in style and any(w in style for w in ["700", "bold"]):
                bold_nodes.append(sp)

        items, seen = [], set()

        for node in bold_nodes:
            strong_txt = _norm_txt(node.get_text(" ", strip=True))
            if not strong_txt:
                continue

            # ⇢ si le même bloc contient "date : titre", séparer
            desc_lower = ""
            parts = INLINE_SEP_RX.split(strong_txt, maxsplit=1)
            if len(parts) == 2:
                bold_date_part = parts[0].strip()
                desc_lower = (parts[1] or "").lower()
            else:
                bold_date_part = strong_txt

            # dates normalisées STRICTEMENT à partir du gras
            date_start, date_end = _parse_bold_date_exact(bold_date_part)

            # si pas de desc inline, prendre le non-gras qui suit
            if not desc_lower:
                tail = _following_text_after(node)
                if tail:
                    desc_lower = tail.lower()

            keyi = (bold_date_part, desc_lower[:220])
            if keyi in seen:
                continue
            seen.add(keyi)

            item = {
                # EXACTEMENT ce qui est écrit en gras :
                "date_bold": bold_date_part,
                # Texte associé en minuscules :
                "texte": desc_lower,
                # Normalisations (uniquement si l'année est explicitement écrite dans le gras) :
                "date_start": date_start,   # "" si inconnue
                "date_end": date_end,       # "" si inconnue
                # Optionnel utile debug :
                "source_bloc": strong_txt
            }

            # Confort : si c'est une date simple (start==end non vide), on ajoute un parlé
            if date_start and date_end and date_start == date_end:
                item["date_spoken"] = ddmmyyyy_to_spoken(date_start)
            else:
                item["date_spoken"] = ""

            items.append(item)

        # Tri : si date_start existe on trie dessus, sinon on laisse en ordre d'apparition
        def k(e):
            ds = e.get("date_start") or ""
            try:
                d, m, y = ds.split("/")
                return (0, int(y), int(m), int(d))
            except Exception:
                return (1, 9999, 12, 31)

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
