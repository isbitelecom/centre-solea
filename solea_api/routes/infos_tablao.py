# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, timedelta
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, remplacer_h_par_heure
)

bp = Blueprint("infos_tablao", __name__)
SRC = "https://www.centresolea.org/"  # on filtre "tablao" depuis la page principale


@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # ========= 0) Aides & Regex =========
        PAGE_TEXT = soup.get_text("\n", strip=True)

        # Saison (ex. "AGENDA 2025-2026") pour inférer l'année si absente
        season_years = []
        m_season = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", PAGE_TEXT)
        if m_season:
            y1, y2 = int(m_season.group(1)), int(m_season.group(2))
            season_years = [y1, y2]

        # Mois (mots + abréviations FR courantes)
        MOIS = {
            "janvier": 1, "janv": 1,
            "février": 2, "fevrier": 2, "févr": 2, "fevr": 2,
            "mars": 3,
            "avril": 4, "avr": 4,
            "mai": 5,
            "juin": 6,
            "juillet": 7, "juil": 7,
            "août": 8, "aout": 8,
            "septembre": 9, "sept": 9,
            "octobre": 10, "oct": 10,
            "novembre": 11, "nov": 11,
            "décembre": 12, "decembre": 12, "déc": 12, "dec": 12,
        }
        MONTH_WORD_GROUP = (
            r"janvier|janv\.?|février|fevrier|févr\.?|fevr\.?|mars|avril|avr\.?|mai|juin|"
            r"juillet|juil\.?|août|aout|septembre|sept\.?|octobre|oct\.?|novembre|nov\.?|"
            r"décembre|decembre|déc\.?|dec\.?"
        )
        JOURS = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"

        # Dates en toutes lettres: "Vendredi 26 septembre 2025", "Samedi 1er nov.", ...
        DATE_RX_WORDS = re.compile(
            rf"(?:(?:du|le|les)?\s*)?(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        # Deuxième date dans la même phrase : "... et Samedi 27 septembre ..."
        ET_DATE_RX = re.compile(
            rf"\bet\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        # Plage: "du Vendredi 25 octobre (2025) au Samedi 26 octobre (2025)"
        RANGE_RX = re.compile(
            rf"\bdu\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})"
            rf"(?:\s+(20\d{{2}}))?"
            rf"\s+au\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})?"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        # Formats numériques français: "26/09", "26/09/2025", "26-09", "26-09-2025"
        DATE_RX_NUM = re.compile(
            r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b"
        )

        def infer_year(month_num: int, explicit_year: int | None) -> int | None:
            if explicit_year:
                return explicit_year
            if season_years:
                y1, y2 = season_years  # sept–déc -> y1 ; jan–août -> y2
                return y1 if month_num >= 9 else y2
            return None

        def to_ddmmyyyy_from_words(day_s: str, month_s: str, year_s: str | None) -> str:
            day = int(re.sub(r"er$", "", day_s, flags=re.IGNORECASE))
            mkey = (month_s or "").lower().replace(".", "")
            mm = MOIS.get(mkey, 0)
            yy = int(year_s) if year_s else infer_year(mm, None)
            if mm and yy:
                return f"{day:02d}/{mm:02d}/{yy}"
            return ""

        def to_ddmmyyyy_from_num(day_s: str, month_s: str, year_s: str | None) -> str:
            dd = int(day_s)
            mm = int(month_s)
            yy = int(year_s) if year_s else infer_year(mm, None)
            if 1 <= dd <= 31 and 1 <= mm <= 12 and yy:
                return f"{dd:02d}/{mm:02d}/{yy}"
            return ""

        def expand_range(mr) -> list[str]:
            d1, m1, y1 = mr.group(1), mr.group(2), mr.group(3)
            d2, m2, y2 = mr.group(4), mr.group(5), mr.group(6)
            dd1 = int(re.sub(r"er$", "", d1, flags=re.IGNORECASE))
            mkey1 = (m1 or "").lower().replace(".", "")
            mm1 = MOIS.get(mkey1, 0)
            yy1 = int(y1) if y1 else infer_year(mm1, None)

            mkey2 = (m2 or m1 or "").lower().replace(".", "")
            mm2 = MOIS.get(mkey2, 0)
            dd2 = int(re.sub(r"er$", "", d2, flags=re.IGNORECASE))
            yy2 = int(y2) if y2 else infer_year(mm2, None)

            if not (mm1 and mm2 and yy1 and yy2):
                return []
            try:
                start = date(yy1, mm1, dd1)
                end = date(yy2, mm2, dd2)
                if end < start:
                    return []
            except Exception:
                return []
            out, cur = [], start
            while cur <= end:
                out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
                cur += timedelta(days=1)
            return out

        def extract_times(txt: str) -> str:
            return extract_time_from_text(txt) or ""

        def extract_lieu(txt: str) -> str:
            # Heuristique: dernier segment après " - " | " — " | "–" | " @ "
            parts = re.split(r"\s+(?:-|—|–|@)\s+", txt)
            if len(parts) >= 2:
                return parts[-1].strip()
            return ""

        # ========= 1) Repérer les "blocs TABLAO" par le DOM =========
        # On considère comme "titre" tout élément qui contient "tablao" (h*, strong, b, a, span fort)
        title_nodes = []
        candidates = soup.select("h1, h2, h3, h4, h5, h6, strong, b, a, span, p")
        for n in candidates:
            try:
                txt = normalize_text(n.get_text(" ", strip=True))
            except Exception:
                continue
            if not txt:
                continue
            if re.search(r"\btablao?s?\b", txt, re.IGNORECASE):
                title_nodes.append(n)

        def gather_block_text(node) -> tuple[str, str]:
            """
            Retourne (titre, bloc_texte) à partir d'un noeud titre "tablao".
            On concatène le texte du noeud + ses frères suivants jusqu'à un "separateur".
            """
            title = normalize_text(node.get_text(" ", strip=True)) or "Tablao"
            pieces = [title]

            # Stop si on tombe sur un nouveau "gros titre" ou HR
            STOP_TAGS = {"h1", "h2", "h3", "h4", "hr"}

            # On parcourt d'abord les frères suivants du parent (souvent, Wix encapsule par paragraphes)
            parent = node if node.name in {"h1", "h2", "h3", "h4", "p"} else node.parent
            if not parent:
                parent = node

            for sib in parent.next_siblings:
                nm = getattr(sib, "name", "") or ""
                if nm.lower() in STOP_TAGS:
                    break
                if hasattr(sib, "get_text"):
                    pieces.append(normalize_text(sib.get_text(" ", strip=True)))
                elif isinstance(sib, NavigableString):
                    pieces.append(normalize_text(str(sib)))

            block = "\n".join([p for p in pieces if p])
            return title, block

        # ========= 2) Extraction d'événements (uniquement titres contenant 'tablao') =========
        items, seen = [], set()

        for node in title_nodes:
            title, block = gather_block_text(node)

            # Heuristique: si le "bloc" est trop court (juste un mot), on regarde aussi le parent précédent
            if len(block) < 10 and node.parent and node.parent.previous_sibling:
                prev = node.parent.previous_sibling
                if hasattr(prev, "get_text"):
                    block = normalize_text(prev.get_text(" ", strip=True)) + "\n" + block

            # heures / lieu (sur tout le bloc)
            hr = extract_times(block)
            lieu = extract_lieu(block)

            # 2.a Plages "du ... au ..."
            for mr in RANGE_RX.finditer(block):
                for dd in expand_range(mr):
                    keyi = (dd, title.lower()[:160], hr)
                    if keyi in seen:
                        continue
                    seen.add(keyi)
                    items.append({
                        "type": "tablao",
                        "date": dd,
                        "date_spoken": ddmmyyyy_to_spoken(dd),
                        "heure": hr,
                        "heure_vocal": remplacer_h_par_heure(hr),
                        "titre": sanitize_for_voice(title),
                        "lieu": sanitize_for_voice(lieu),
                    })

            # 2.b Dates "mots" principales + "et ..."
            m1 = DATE_RX_WORDS.search(block)
            if m1:
                d1 = to_ddmmyyyy_from_words(m1.group(1), m1.group(2), m1.group(3))
                if d1:
                    keyi = (d1, title.lower()[:160], hr)
                    if keyi not in seen:
                        seen.add(keyi)
                        items.append({
                            "type": "tablao",
                            "date": d1,
                            "date_spoken": ddmmyyyy_to_spoken(d1),
                            "heure": hr,
                            "heure_vocal": remplacer_h_par_heure(hr),
                            "titre": sanitize_for_voice(title),
                            "lieu": sanitize_for_voice(lieu),
                        })
                for m2 in ET_DATE_RX.finditer(block):
                    d2 = to_ddmmyyyy_from_words(m2.group(1), m2.group(2), m2.group(3))
                    if d2:
                        keyi = (d2, title.lower()[:160], hr)
                        if keyi not in seen:
                            seen.add(keyi)
                            items.append({
                                "type": "tablao",
                                "date": d2,
                                "date_spoken": ddmmyyyy_to_spoken(d2),
                                "heure": hr,
                                "heure_vocal": remplacer_h_par_heure(hr),
                                "titre": sanitize_for_voice(title),
                                "lieu": sanitize_for_voice(lieu),
                            })

            # 2.c Formats numériques (dd/mm[/yyyy]) éventuels
            for mn in DATE_RX_NUM.finditer(block):
                dnum = to_ddmmyyyy_from_num(mn.group(1), mn.group(2), mn.group(3))
                if dnum:
                    keyi = (dnum, title.lower()[:160], hr)
                    if keyi not in seen:
                        seen.add(keyi)
                        items.append({
                            "type": "tablao",
                            "date": dnum,
                            "date_spoken": ddmmyyyy_to_spoken(dnum),
                            "heure": hr,
                            "heure_vocal": remplacer_h_par_heure(hr),
                            "titre": sanitize_for_voice(title),
                            "lieu": sanitize_for_voice(lieu),
                        })

        # ========= 3) Tri chronologique =========
        def k(e):
            if e.get("date"):
                try:
                    dd, mm, yyyy = e["date"].split("/")
                    return (int(yyyy), int(mm), int(dd))
                except Exception:
                    return (9999, 12, 31)
            return (9999, 12, 31)

        items.sort(key=k)

        # ========= 4) Version "vocale" =========
        tablaos_vocal = []
        for e in items:
            parts = [f"Tablao le {e['date_spoken']}"]
            if e.get("heure_vocal"):
                parts.append(f"à {e['heure_vocal']}")
            if e.get("lieu"):
                parts.append(f"au {e['lieu']}")
            parts.append(f": {e['titre']}")
            tablaos_vocal.append(sanitize_for_voice(" ".join(p for p in parts if p).strip()))

        payload = {
            "source": SRC,
            "count": len(items),
            "tablaos": items,
            "tablaos_vocal": tablaos_vocal
        }
        cache_set(key, payload, ttl_seconds=120)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
