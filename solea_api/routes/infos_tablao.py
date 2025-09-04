# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, timedelta
from urllib.parse import urljoin
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, remplacer_h_par_heure
)

bp = Blueprint("infos_tablao", __name__)
BASE = "https://www.centresolea.org"
SRC = f"{BASE}/"  # on filtre "tablao" depuis la page principale


def nz(s):
    """None -> '', garde une str sinon."""
    return s if isinstance(s, str) else ""

def norm(s):
    """normalize_text sur une str toujours non nulle."""
    return normalize_text(nz(s))

@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # ========= Aides & Regex =========
        page_text = norm(soup.get_text("\n", strip=True))

        # Saison (ex. "AGENDA 2025-2026") pour inférer l'année si absente
        season_years = []
        m_season = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", page_text or "")
        if m_season:
            y1, y2 = int(m_season.group(1)), int(m_season.group(2))
            season_years = [y1, y2]

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

        DATE_RX_WORDS = re.compile(
            rf"(?:(?:du|le|les)?\s*)?(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        ET_DATE_RX = re.compile(
            rf"\bet\s+(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+"
            rf"({MONTH_WORD_GROUP})"
            rf"(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
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
        DATE_RX_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b")

        def infer_year(month_num: int, explicit_year: int | None) -> int | None:
            if explicit_year:
                return explicit_year
            if season_years:
                y1, y2 = season_years  # sept–déc -> y1 ; jan–août -> y2
                return y1 if month_num >= 9 else y2
            return None

        def to_ddmmyyyy_from_words(day_s: str, month_s: str, year_s: str | None) -> str:
            if not day_s or not month_s:
                return ""
            try:
                day = int(re.sub(r"er$", "", day_s, flags=re.IGNORECASE))
            except Exception:
                return ""
            mkey = nz(month_s).lower().replace(".", "")
            mm = MOIS.get(mkey, 0)
            yy = int(year_s) if year_s else infer_year(mm, None)
            if mm and yy:
                return f"{day:02d}/{mm:02d}/{yy}"
            return ""

        def to_ddmmyyyy_from_num(day_s: str, month_s: str, year_s: str | None) -> str:
            try:
                dd = int(day_s); mm = int(month_s)
            except Exception:
                return ""
            yy = int(year_s) if year_s else infer_year(mm, None)
            if 1 <= dd <= 31 and 1 <= mm <= 12 and yy:
                return f"{dd:02d}/{mm:02d}/{yy}"
            return ""

        def expand_range(mr) -> list[str]:
            try:
                d1, m1, y1 = mr.group(1), mr.group(2), mr.group(3)
                d2, m2, y2 = mr.group(4), mr.group(5), mr.group(6)
            except Exception:
                return []
            d1 = nz(d1); d2 = nz(d2); m1 = nz(m1); m2 = nz(m2)
            try:
                dd1 = int(re.sub(r"er$", "", d1, flags=re.IGNORECASE))
                dd2 = int(re.sub(r"er$", "", d2, flags=re.IGNORECASE))
            except Exception:
                return []
            mm1 = MOIS.get(m1.lower().replace(".", ""), 0)
            mm2 = MOIS.get((m2 or m1).lower().replace(".", ""), 0)
            yy1 = int(y1) if y1 else infer_year(mm1, None)
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

        def extract_lieu(txt: str) -> str:
            t = nz(txt)
            if not t:
                return ""
            parts = re.split(r"\s+(?:-|—|–|@)\s+", t)
            if len(parts) >= 2:
                return parts[-1].strip()
            return ""

        def any_date_in(text: str) -> list[str]:
            t = nz(text)
            if not t:
                return []
            out = []
            # Plages
            for mr in RANGE_RX.finditer(t):
                out.extend(expand_range(mr))
            # Dates mots
            for m1 in DATE_RX_WORDS.finditer(t):
                d = to_ddmmyyyy_from_words(m1.group(1), m1.group(2), m1.group(3))
                if d:
                    out.append(d)
            for m2 in ET_DATE_RX.finditer(t):
                d = to_ddmmyyyy_from_words(m2.group(1), m2.group(2), m2.group(3))
                if d:
                    out.append(d)
            # Numériques
            for mn in DATE_RX_NUM.finditer(t):
                d = to_ddmmyyyy_from_num(mn.group(1), mn.group(2), mn.group(3))
                if d:
                    out.append(d)
            # déduplique en conservant l'ordre
            seen_d, uniq = set(), []
            for d in out:
                if d not in seen_d:
                    seen_d.add(d)
                    uniq.append(d)
            return uniq

        # ========= Sélection des titres contenant "tablao" =========
        title_nodes = []
        for n in soup.select("h1, h2, h3, h4, h5, h6, strong, b, a, span, p"):
            try:
                txt = norm(n.get_text(" ", strip=True))
            except Exception:
                continue
            if txt and re.search(r"\btablao?s?\b", txt, re.IGNORECASE):
                title_nodes.append(n)

        def find_related_href(node):
            """Trouve une URL de détails proche du titre."""
            try:
                if getattr(node, "name", "") == "a" and node.get("href"):
                    return urljoin(BASE, nz(node.get("href")))
                a = getattr(node, "find", lambda *_: None)("a")
                if a and a.get("href"):
                    return urljoin(BASE, nz(a.get("href")))
                parent = node.parent or node
                steps = 0
                for sib in parent.next_siblings:
                    steps += 1
                    if steps > 6:
                        break
                    if getattr(sib, "name", "") and hasattr(sib, "select"):
                        for cand in sib.select("a"):
                            href = nz(cand.get("href"))
                            if href:
                                return urljoin(BASE, href)
                anc = node.parent
                depth = 0
                while getattr(anc, "select", None) and depth < 4:
                    depth += 1
                    for cand in anc.select("a"):
                        href = nz(cand.get("href"))
                        if href:
                            return urljoin(BASE, href)
                    anc = anc.parent
            except Exception:
                return None
            return None

        def gather_local_block(node) -> str:
            """Concatène le texte du titre + 6 frères suivants de son parent (cartes Wix)."""
            pieces = [norm(node.get_text(" ", strip=True))]
            parent = node if getattr(node, "name", "") in {"h1", "h2", "h3", "h4", "p"} else (node.parent or node)
            steps = 0
            for sib in getattr(parent, "next_siblings", []):
                if steps >= 6:
                    break
                steps += 1
                nm = nz(getattr(sib, "name", ""))
                if nm.lower() in {"h1", "h2", "h3", "h4", "hr"}:
                    break
                if hasattr(sib, "get_text"):
                    pieces.append(norm(sib.get_text(" ", strip=True)))
                elif isinstance(sib, NavigableString):
                    pieces.append(norm(str(sib)))
            return "\n".join([p for p in pieces if p])

        def parse_detail(url: str) -> tuple[list[str], str, str]:
            """Retourne (dates[], heure, lieu) depuis la page détail si possible."""
            try:
                h = fetch_html(url)
                sp = soup_from_html(h)
                txt = norm(sp.get_text("\n", strip=True))
            except Exception:
                return [], "", ""
            dates = any_date_in(txt)
            hr = nz(extract_time_from_text(txt))  # extract_time_from_text tolère str vide
            # Lieu : heuristique simple — une ligne ressemblant à une adresse
            lieu = ""
            for line in re.split(r"\n+", txt or ""):
                if re.search(r"(Marseille|Rue|France|130\d{2})", line or "", re.IGNORECASE):
                    lieu = line.strip()
                    break
            return dates, hr, lieu

        # ========= Extraction =========
        items, seen = [], set()

        for node in title_nodes:
            title = norm(node.get_text(" ", strip=True)) or "Tablao"

            # 1) bloc local
            block = gather_local_block(node)
            dates = any_date_in(block)
            hr = nz(extract_time_from_text(block))
            lieu = extract_lieu(block)

            # 2) fallback : page détail
            if not dates:
                href = find_related_href(node)
                if href:
                    d2, hr2, lieu2 = parse_detail(href)
                    if d2:
                        dates = d2
                    if hr2 and not hr:
                        hr = hr2
                    if lieu2 and not lieu:
                        lieu = lieu2

            # 3) Si aucune date trouvée, on retourne tout de même l'info (date vide)
            titre_norm = sanitize_for_voice(title)
            lieu_norm = sanitize_for_voice(lieu)

            if not dates:
                keyi = ("", titre_norm.lower()[:160], hr)
                if keyi not in seen:
                    seen.add(keyi)
                    items.append({
                        "type": "tablao",
                        "date": "",
                        "date_spoken": "",
                        "heure": hr,
                        "heure_vocal": remplacer_h_par_heure(hr),
                        "titre": titre_norm,
                        "lieu": lieu_norm,
                    })
                continue

            # Sinon, une entrée par date
            for dd in dates:
                keyi = (dd, titre_norm.lower()[:160], hr)
                if keyi in seen:
                    continue
                seen.add(keyi)
                items.append({
                    "type": "tablao",
                    "date": dd,
                    "date_spoken": ddmmyyyy_to_spoken(dd),
                    "heure": hr,
                    "heure_vocal": remplacer_h_par_heure(hr),
                    "titre": titre_norm,
                    "lieu": lieu_norm,
                })

        # ========= Tri (les sans date en bas) =========
        def k(e):
            d = nz(e.get("date"))
            if d:
                try:
                    dd, mm, yyyy = d.split("/")
                    return (0, int(yyyy), int(mm), int(dd))
                except Exception:
                    pass
            return (1, 9999, 12, 31)

        items.sort(key=k)

        # ========= Version vocale =========
        tablaos_vocal = []
        for e in items:
            if e.get("date_spoken"):
                parts = [f"Tablao le {e['date_spoken']}"]
            else:
                parts = ["Tablao"]
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
        cache_set(key, payload, ttl_seconds=180)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
