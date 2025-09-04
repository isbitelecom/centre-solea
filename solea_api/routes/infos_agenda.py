# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    parse_date_any, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta
)

bp = Blueprint("infos_agenda", __name__)
SRC = "https://www.centresolea.org/agenda"


def following_text_after(node) -> str:
    """
    Récupère le texte **non gras** qui suit un nœud <strong>/<b>/<span bold>
    en concaténant les frères du parent jusqu'à rencontrer un nouveau séparateur
    (nouveau titre/gras/hr). On nettoie le préfixe ':', '—', '-'.
    """
    buff = []

    # 1) texte immédiatement après le nœud (dans le même parent)
    if node.next_sibling and isinstance(node.next_sibling, NavigableString):
        buff.append(str(node.next_sibling))

    # 2) puis les frères suivants du parent
    parent = node.parent or node
    for sib in parent.next_siblings:
        nm = (getattr(sib, "name", "") or "").lower()
        # on s'arrête si on croise une nouvelle "section"
        if nm in {"strong", "b", "h1", "h2", "h3", "hr"}:
            break
        if nm == "br":
            buff.append("\n")
            continue
        if hasattr(sib, "get_text"):
            # retirer le gras éventuel dans ce bloc
            for t in sib.find_all(["strong", "b"]):
                t.decompose()
            buff.append(sib.get_text(" ", strip=True))
            continue
        if isinstance(sib, NavigableString):
            buff.append(str(sib))

    txt = " ".join([normalize_text(x) for x in buff if normalize_text(x)])
    txt = re.sub(r"^\s*[:—–\-]\s*", "", txt)
    return txt.strip()


def is_bold_date_text(s: str) -> bool:
    """
    Détermine si le texte 's' (issu d'un <strong>/<b>/span bold) est une date.
    On s'appuie sur parse_date_any (FR jours/mois, formats numériques).
    """
    s_norm = normalize_text(s or "")
    if not s_norm:
        return False
    ddmmyyyy = parse_date_any(s_norm)
    return bool(ddmmyyyy)


@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # 1) nœuds en gras (Wix utilise aussi des <span style="font-weight:700">)
        bold_nodes = list(soup.select("strong, b"))
        for sp in soup.find_all("span"):
            style = (sp.get("style") or "").lower()
            if "font-weight" in style and any(w in style for w in ["700", "bold"]):
                bold_nodes.append(sp)

        items, seen = [], set()

        for node in bold_nodes:
            strong_txt = normalize_text(node.get_text(" ", strip=True))
            if not strong_txt:
                continue

            # ne garder que les *dates en gras*
            ddmmyyyy = parse_date_any(strong_txt)
            if not ddmmyyyy:
                continue

            # texte associé (en minuscules)
            desc = following_text_after(node)
            if not desc:
                # fallback: texte du parent après la date si avec séparateur
                parent_text = normalize_text(node.parent.get_text(" ", strip=True)) if node.parent else ""
                if parent_text and parent_text != strong_txt:
                    parts = re.split(r"\s*[:—–-]\s*", parent_text, maxsplit=1)
                    if len(parts) == 2:
                        desc = parts[1].strip()

            desc_lower = (desc or "").lower()

            keyi = (ddmmyyyy, desc_lower[:160])
            if keyi in seen:
                continue
            seen.add(keyi)

            items.append({
                "date": ddmmyyyy,
                "date_spoken": ddmmyyyy_to_spoken(ddmmyyyy),
                "texte": desc_lower,     # ← le texte en minuscules comme demandé
                "source_bloc": strong_txt  # (optionnel) le contenu exact du gras d'origine
            })

        # 2) tri par date croissante
        def k(e):
            try:
                dd, mm, yyyy = e["date"].split("/")
                return (int(yyyy), int(mm), int(dd))
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
            # renvoyer la dernière bonne donnée si dispo
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
