# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, timedelta

from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, classify_type, parse_date_any, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, remplacer_h_par_heure
)

bp = Blueprint("infos_tablao", __name__)
SRC = "https://www.centresolea.org/agenda"  # on filtre "tablao" depuis l'agenda


@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # === 1) Texte brut -> lignes normalisées ===
        page_text = soup.get_text("\n", strip=True)
        raw_lines = [normalize_text(l) for l in re.split(r"\n+|\r+|•", page_text)]
        lines = [l for l in raw_lines if l]

        # === 2) Saison (ex. "AGENDA 2025-2026") pour inférer l'année quand absente ===
        season_years = []
        m_season = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", page_text)
        if m_season:
            y1, y2 = int(m_season.group(1)), int(m_season.group(2))
            season_years = [y1, y2]

        MOIS = {
            "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
            "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
            "décembre": 12, "decembre": 12
        }
        JOURS = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"

        # "Le/Les Vendredi 26 septembre 2025", "Samedi 1er novembre", etc.
        DATE_RX = re.compile(
            rf"(?:(?:du|le|les)?\s*)?(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        # "… et Samedi 27 septembre (2025) …"
        ET_DATE_RX = re.compile(
            rf"\bet\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        # "du Vendredi 25 octobre (2025) au Samedi 26 octobre (2025)"
        RANGE_RX = re.compile(
            rf"\bdu\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)"
            rf"(?:\s+(20\d{{2}}))?"
            rf"\s+au\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)?"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )

        def infer_year(month_num: int, explicit_year: int | None) -> int | None:
            """Si l'année n'est pas présente, infère-la via la saison (sept-déc -> y1, jan-août -> y2)."""
            if explicit_year:
                return explicit_year
            if season_years:
                y1, y2 = season_years
                return y1 if month_num >= 9 else y2
            return None

        def to_ddmmyyyy(day_s: str, month_s: str, year_s: str | None) -> str:
            """Convertit '1er', 'novembre', '2025?' -> '01/11/2025' (en inférant l'année si absente)."""
            day = re.sub(r"er$", "", (day_s or ""), flags=re.IGNORECASE)
            mm = MOIS.get((month_s or "").lower(), 0)
            yy = int(year_s) if year_s else infer_year(mm, None)
            if mm and yy:
                return f"{int(day):02d}/{mm:02d}/{yy}"
            return ""

        def expand_range(d1: str, m1: str, y1: str | None, d2: str, m2: str | None, y2: str | None):
            """Génère une liste de dates (dd/mm/yyyy) pour 'du d1 m1 (y1) au d2 m2 (y2)'."""
            mm1 = MOIS.get((m1 or "").lower(), 0)
            mm2 = MOIS.get((m2 or m1 or "").lower(), 0)  # si m2 absent -> même mois
            yy1 = int(y1) if y1 else infer_year(mm1, None)
            yy2 = int(y2) if y2 else infer_year(mm2, None)
            if not (mm1 and mm2 and yy1 and yy2):
                return []

            # Construire des dates Python
            d1i = int(re.sub(r"er$", "", d1, flags=re.IGNORECASE))
            d2i = int(re.sub(r"er$", "", d2, flags=re.IGNORECASE))
            try:
                start = date(yy1, mm1, d1i)
                end = date(yy2, mm2, d2i)
                if end < start:
                    return []
            except Exception:
                return []

            out = []
            cur = start
            while cur <= end:
                out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
                cur += timedelta(days=1)
            return out

        def extract_lieu(desc: str) -> str:
            """Heuristique simple : prendre le dernier segment après ' - '."""
            parts = [p.strip() for p in re.split(r"\s+-\s+", desc)]
            if len(parts) >= 2:
                return parts[-1]
            return ""

        def cap(s: str) -> str:
            return (s or "").strip()

        items, seen = [], set()

        # === 3) Parcours des lignes ===
        for line in lines:
            if not line:
                continue

            # On cherche d'abord une plage "du ... au ..."
            mr = RANGE_RX.search(line)
            if mr:
                # Desc = après le premier ":" si présent, sinon la ligne complète
                desc = line.split(":", 1)[1].strip() if ":" in line else line
                typ = classify_type(desc)
                if typ != "tablao" and not re.search(r"\btablao?s?\b", desc, re.IGNORECASE):
                    # pas un tablao -> ignore la ligne
                    pass
                else:
                    typ = "tablao"
                    hr = extract_time_from_text(desc) or ""
                    lieu = extract_lieu(desc)
                    dates = expand_range(mr.group(1), mr.group(2), mr.group(3),
                                         mr.group(4), mr.group(5), mr.group(6))
                    for dd in dates:
                        keyi = (dd, desc.lower()[:160])
                        if keyi in seen:
                            continue
                        seen.add(keyi)
                        items.append({
                            "type": typ,
                            "date": dd,
                            "date_spoken": ddmmyyyy_to_spoken(dd),
                            "heure": hr,
                            "heure_vocal": remplacer_h_par_heure(hr),
                            "titre": sanitize_for_voice(desc),
                            "lieu": sanitize_for_voice(lieu),
                        })
                # même si plage trouvée, on continue pour repérer aussi d'éventuelles dates simples

            # Dates simples dans la ligne (éventuellement multiples via "… et …")
            m1 = DATE_RX.search(line)
            if not m1:
                continue

            desc = line.split(":", 1)[1].strip() if ":" in line else line
            typ = classify_type(desc)
            if typ != "tablao" and not re.search(r"\btablao?s?\b", desc, re.IGNORECASE):
                continue
            typ = "tablao"

            hr = extract_time_from_text(desc) or ""
            lieu = extract_lieu(desc)

            d1 = to_ddmmyyyy(m1.group(1), m1.group(2), m1.group(3))
            if d1:
                keyi = (d1, desc.lower()[:160])
                if keyi not in seen:
                    seen.add(keyi)
                    items.append({
                        "type": typ,
                        "date": d1,
                        "date_spoken": ddmmyyyy_to_spoken(d1),
                        "heure": hr,
                        "heure_vocal": remplacer_h_par_heure(hr),
                        "titre": sanitize_for_voice(desc),
                        "lieu": sanitize_for_voice(lieu),
                    })

            # Dates additionnelles dans la même ligne après "et ..."
            for m2 in ET_DATE_RX.finditer(line):
                d2 = to_ddmmyyyy(m2.group(1), m2.group(2), m2.group(3))
                if d2:
                    keyi = (d2, desc.lower()[:160])
                    if keyi not in seen:
                        seen.add(keyi)
                        items.append({
                            "type": typ,
                            "date": d2,
                            "date_spoken": ddmmyyyy_to_spoken(d2),
                            "heure": hr,
                            "heure_vocal": remplacer_h_par_heure(hr),
                            "titre": sanitize_for_voice(desc),
                            "lieu": sanitize_for_voice(lieu),
                        })

        # === 4) Tri chronologique ===
        def k(e):
            if e.get("date"):
                try:
                    dd, mm, yyyy = e["date"].split("/")
                    return (int(yyyy), int(mm), int(dd))
                except Exception:
                    return (9999, 12, 31)
            return (9999, 12, 31)

        items.sort(key=k)

        # === 5) Version "vocale" conviviale ===
        tablaos_vocal = []
        for e in items:
            parts = [f"Tablao le {cap(e['date_spoken'])}"]
            if e.get("heure_vocal"):
                parts.append(f"à {cap(e['heure_vocal'])}")
            if e.get("lieu"):
                parts.append(f"au {cap(e['lieu'])}")
            # titre en dernier, si utile pour plus de contexte
            titre = e.get("titre") or ""
            if titre:
                parts.append(f": {titre}")
            tablaos_vocal.append(sanitize_for_voice(" ".join(parts)))

        payload = {
            "source": SRC,
            "count": len(items),
            "tablaos": items,
            "tablaos_vocal": tablaos_vocal
        }
        cache_set(key, payload, ttl_seconds=90)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            # Retourne la dernière donnée en cache si dispo
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
