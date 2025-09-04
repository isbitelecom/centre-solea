# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, classify_type, parse_date_any, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, remplacer_h_par_heure
)
from bs4 import NavigableString

bp = Blueprint("infos_tablao", __name__)
SRC = "https://www.centresolea.org/agenda"  # on filtre "tablao" depuis l'agenda

def following_text_after(node) -> str:
    buff = []
    if node.next_sibling and isinstance(node.next_sibling, NavigableString):
        buff.append(str(node.next_sibling))
    for sib in node.parent.next_siblings:
        nm = getattr(sib, "name", "") or ""
        nl = nm.lower()
        if nl in {"strong","b","h1","h2","h3","hr"}:
            break
        if nl == "br":
            buff.append("\n"); continue
        if hasattr(sib, "get_text"):
            for t in sib.find_all(["strong","b"]):
                t.decompose()
            buff.append(sib.get_text(" ", strip=True)); continue
        if isinstance(sib, NavigableString):
            buff.append(str(sib))
    txt = " ".join([normalize_text(x) for x in buff if normalize_text(x)])
    txt = re.sub(r"^\s*[:—–\-]\s*", "", txt)
    return txt.strip()

@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # collecte similaire à l'agenda
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
            ddmmyyyy = parse_date_any(strong_txt)
            if not ddmmyyyy:
                continue
            desc = following_text_after(node)
            if not desc:
                parent_text = normalize_text(node.parent.get_text(" ", strip=True))
                if parent_text and parent_text != strong_txt:
                    parts = re.split(r"\s*[:—–-]\s*", parent_text, maxsplit=1)
                    if len(parts) == 2:
                        desc = parts[1].strip()
            if not desc:
                continue

            desc = sanitize_for_voice(desc)
            typ = classify_type(desc)
            if typ != "tablao":
                continue

            hr = extract_time_from_text(desc)
            keyi = (ddmmyyyy, desc.lower()[:140])
            if keyi in seen:
                continue
            seen.add(keyi)
            items.append({
                "type": typ,
                "date": ddmmyyyy,
                "date_spoken": ddmmyyyy_to_spoken(ddmmyyyy),
                "heure": hr,
                "heure_vocal": remplacer_h_par_heure(hr),
                "titre": desc,
                "lieu": ""
            })

        # tri
        def k(e):
            if e.get("date"):
                try:
                    dd, mm, yyyy = e["date"].split("/")
                    return (int(yyyy), int(mm), int(dd))
                except Exception:
                    return (9999,12,31)
            return (9999,12,31)
        items.sort(key=k)

        payload = {
            "source": SRC,
            "count": len(items),
            "tablaos": items,
            "tablaos_vocal": items
        }
        cache_set(key, payload, ttl_seconds=90)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
