# solea_api/routes/infos_agenda.py
from flask import Blueprint, jsonify, request
import re
from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, classify_type, parse_date_any, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, extract_ldjson_events,
    norm_event_from_ld, remplacer_h_par_heure
)
from bs4 import NavigableString

bp = Blueprint("infos_agenda", __name__)
SRC = "https://www.centresolea.org/agenda"

def following_text_after(node) -> str:
    buff = []
    # même parent : texte juste après le gras
    if node.next_sibling and isinstance(node.next_sibling, NavigableString):
        buff.append(str(node.next_sibling))
    # siblings suivants du parent
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

@bp.get("/infos-agenda")
def infos_agenda():
    key = cache_key("infos-agenda", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # nœuds en gras (Wix)
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
            hr = extract_time_from_text(desc)
            typ = classify_type(desc)
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

        # fallback JSON-LD
        if not items:
            for d in extract_ldjson_events(html):
                ev = norm_event_from_ld(d)
                typ = classify_type(ev["name"], ev.get("description",""))
                titre = sanitize_for_voice(ev["name"])
                keyi = (ev["date"], titre.lower()[:140])
                if keyi in seen:
                    continue
                seen.add(keyi)
                items.append({
                    "type": typ,
                    "date": ev["date"],
                    "date_spoken": ev["date_spoken"],
                    "heure": ev["heure"],
                    "heure_vocal": ev["heure_vocal"],
                    "titre": titre,
                    "lieu": ev.get("location","")
                })

        # tri par date croissante
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
            "evenements": items,
            "evenements_vocal": items
        }
        cache_set(key, payload, ttl_seconds=90)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
