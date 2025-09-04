# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
import unicodedata
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


def nz(s):  # None -> ''
    return s if isinstance(s, str) else ""

def norm(s):  # normalize_text sûr
    return normalize_text(nz(s))

def strip_accents(s: str) -> str:
    s = nz(s)
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        page_text = norm(soup.get_text("\n", strip=True))

        # ===== Saison pour inférence d'année =====
        season_years = []
        m_season = re.search(r"\b(20\d{2})\s*[-/]\s*(20\d{2})\b", page_text or "")
        if m_season:
            season_years = [int(m_season.group(1)), int(m_season.group(2))]

        # ===== Mois (plein + abréviations, avec/ sans accents/points/MAJ) =====
        MOIS_FULL = {
            "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
            "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
            "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12
        }
        MOIS_ABBR = {
            "jan": 1, "janv": 1,
            "fev": 2, "fevr": 2, "fevr": 2, "fev.": 2, "fév": 2, "fév.": 2,
            "mar": 3,
            "avr": 4,
            "mai": 5,
            "jun": 6, "juin": 6,  # au cas où
            "jul": 7, "juil": 7, "juil.": 7,
            "aou": 8, "aoû": 8, "août": 8, "aout": 8,
            "sep": 9, "sept": 9, "sept.": 9,
            "oct": 10, "oct.": 10,
            "nov": 11, "nov.": 11,
            "dec": 12, "déc": 12, "déc.": 12, "dec.": 12,
        }
        # regex “mots mois” large
        MONTH_WORD_GROUP = (
            r"janvier|janv\.?|février|fevrier|févr\.?|fevr\.?|mars|avril|avr\.?|mai|juin|"
            r"juillet|juil\.?|août|aout|septembre|sept\.?|octobre|oct\.?|novembre|nov\.?|"
            r"décembre|decembre|déc\.?|dec\.?"
        )
        JOURS = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"

        DATE_RX_WORDS = re.compile(
            rf"(?:(?:du|le|les)?\s*)?(?:{JOURS}\s+)?"
            rf"(\d{{1,2}}(?:er)?)\s+({MONTH_WORD_GROUP})(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        ET_DATE_RX = re.compile(
            rf"\bet\s+(?:{JOURS}\s+)?(\d{{1,2}}(?:er)?)\s+({MONTH_WORD_GROUP})(?:\s+(20\d{{2}}))?",
            re.IGNORECASE
        )
        RANGE_RX = re.compile(
            rf"\bdu\s+(?:{JOURS}\s+)?(\d{{1,2}}(?:er)?)\s+({MONTH_WORD_GROUP})(?:\s+(20\d{{2}}))?"
            rf"\s+au\s+(?:{JOURS}\s+)?(\d{{1,2}}(?:er)?)\s+({MONTH_WORD_GROUP})?(?:\s+(20\d{{2}}))?",
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

        def month_from_token(tok: str) -> int:
            if not tok:
                return 0
            t = strip_accents(tok).lower().replace(".", "").strip()
            if t in MOIS_FULL:
                return MOIS_FULL[t]
            if t in MOIS_ABBR:
                return MOIS_ABBR[t]
            # formes 3 lettres maj (“OCT”, “NOV”, “DEC”)
            if re.fullmatch(r"(jan|fev|mar|avr|mai|jun|jul|aou|sep|oct|nov|dec)", t):
                return {
                    "jan": 1, "fev": 2, "mar": 3, "avr": 4, "mai": 5, "jun": 6,
                    "jul": 7, "aou": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
                }[t]
            return 0

        def to_ddmmyyyy_from_words(day_s: str, month_s: str, year_s: str | None) -> str:
            if not day_s or not month_s:
                return ""
            try:
                day = int(re.sub(r"er$", "", day_s, flags=re.IGNORECASE))
            except Exception:
                return ""
            mm = month_from_token(month_s)
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
            mm1 = month_from_token(m1)
            mm2 = month_from_token(m2 or m1)
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

        def any_date_in(text: str) -> list[str]:
            t = nz(text)
            if not t:
                return []
            out = []
            for mr in RANGE_RX.finditer(t):
                out.extend(expand_range(mr))
            for m1 in DATE_RX_WORDS.finditer(t):
                d = to_ddmmyyyy_from_words(m1.group(1), m1.group(2), m1.group(3))
                if d: out.append(d)
            for m2 in ET_DATE_RX.finditer(t):
                d = to_ddmmyyyy_from_words(m2.group(1), m2.group(2), m2.group(3))
                if d: out.append(d)
            for mn in DATE_RX_NUM.finditer(t):
                d = to_ddmmyyyy_from_num(mn.group(1), mn.group(2), mn.group(3))
                if d: out.append(d)
            # uniq preserving order
            seen_d, uniq = set(), []
            for d in out:
                if d not in seen_d:
                    seen_d.add(d); uniq.append(d)
            return uniq

        # ====== DOM: extraire dates éclatées autour du titre ======
        def collect_tokens_around(node, up_levels=3, forward_siblings=8):
            """
            Remonte jusqu'à un conteneur raisonnable, puis collecte:
             - textes descendants (chaque petit span)
             - textes des frères suivants (quelques pas)
             - attributs utiles: datetime, title, aria-label
            """
            # remonter
            anc = node
            for _ in range(up_levels):
                if not anc.parent:
                    break
                anc = anc.parent
                # heuristique: si ce conteneur contient <time> ou beaucoup de petits spans, on s'arrête
                if getattr(anc, "find", None) and (anc.find("time") or len(list(anc.find_all("span"))) >= 3):
                    break

            tokens = []
            def push_token(s):
                s = norm(s)
                if s:
                    tokens.append(s)

            # descendants (texte de petits spans)
            if getattr(anc, "find_all", None):
                for el in anc.find_all(True, limit=120):
                    # time tag
                    if el.name == "time":
                        dt = el.get("datetime")
                        if dt: push_token(dt)
                    # attributs utiles
                    for attr in ("aria-label", "title", "data-title", "data-date", "data-datetime"):
                        v = el.get(attr)
                        if v: push_token(v)
                    # texte
                    txt = el.get_text(" ", strip=True)
                    push_token(txt)

            # quelques frères suivants du parent direct du titre
            parent = node.parent or node
            steps = 0
            for sib in getattr(parent, "next_siblings", []):
                if steps >= forward_siblings: break
                steps += 1
                if hasattr(sib, "get_text"):
                    push_token(sib.get_text(" ", strip=True))
                elif isinstance(sib, NavigableString):
                    push_token(str(sib))

            return tokens

        NUM_RX = re.compile(r"^\d{1,2}$")
        YEAR_RX = re.compile(r"^(20\d{2})$")

        def dates_from_tokens(tokens):
            out = []
            # 1) tout texte “classique”
            big_text = " \n ".join(tokens)
            out.extend(any_date_in(big_text))
            # 2) reconstruction “jour + mois (+ année)” en tokens consécutifs
            for i in range(len(tokens)-1):
                a, b = tokens[i], tokens[i+1]
                if NUM_RX.match(a) and month_from_token(b):
                    dd = int(a)
                    mm = month_from_token(b)
                    yy = None
                    if i+2 < len(tokens) and YEAR_RX.match(tokens[i+2]):
                        yy = int(tokens[i+2])
                    else:
                        yy = infer_year(mm, None)
                    if 1 <= dd <= 31 and mm and yy:
                        out.append(f"{dd:02d}/{mm:02d}/{yy}")
                # plage: “11 … au 12 OCT”
                if NUM_RX.match(a) and tokens[i+1].lower() == "au":
                    # chercher “12”, puis mois
                    if i+2 < len(tokens) and NUM_RX.match(tokens[i+2]):
                        dd1 = int(a); dd2 = int(tokens[i+2])
                        # mois après (ou avant)
                        mm = 0
                        # après
                        if i+3 < len(tokens):
                            mm = month_from_token(tokens[i+3])
                        # sinon, tenter le mois juste avant
                        if mm == 0 and i-1 >= 0:
                            mm = month_from_token(tokens[i-1])
                        if mm:
                            yy1 = infer_year(mm, None)
                            yy2 = yy1
                            if yy1 and yy2:
                                # étendre la plage
                                try:
                                    start = date(yy1, mm, dd1)
                                    end = date(yy2, mm, dd2)
                                    cur = start
                                    while cur <= end:
                                        out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
                                        cur += timedelta(days=1)
                                except Exception:
                                    pass
            # uniq
            seen, uniq = set(), []
            for d in out:
                if d not in seen:
                    seen.add(d); uniq.append(d)
            return uniq

        def extract_lieu(txt: str) -> str:
            t = nz(txt)
            parts = re.split(r"\s+(?:-|—|–|@)\s+", t)
            if len(parts) >= 2:
                return parts[-1].strip()
            return ""

        # ====== Détail ======
        def parse_detail(url: str):
            try:
                h = fetch_html(url)
                sp = soup_from_html(h)
                txt = norm(sp.get_text("\n", strip=True))
            except Exception:
                return [], "", ""
            dates = any_date_in(txt)
            hr = nz(extract_time_from_text(txt))
            lieu = ""
            for line in re.split(r"\n+", txt or ""):
                if re.search(r"(Marseille|Rue|France|130\d{2})", line or "", re.IGNORECASE):
                    lieu = line.strip(); break
            return dates, hr, lieu

        def find_related_href(node):
            try:
                if getattr(node, "name", "") == "a" and node.get("href"):
                    return urljoin(BASE, nz(node.get("href")))
                a = getattr(node, "find", lambda *_: None)("a")
                if a and a.get("href"):
                    return urljoin(BASE, nz(a.get("href")))
                parent = node.parent or node
                # scrute quelques frères
                steps = 0
                for sib in parent.next_siblings:
                    steps += 1
                    if steps > 8: break
                    if getattr(sib, "name", "") and hasattr(sib, "select"):
                        for cand in sib.select("a"):
                            href = nz(cand.get("href"))
                            if href:
                                return urljoin(BASE, href)
                # scrute ancêtres
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

        # ====== Sélection des titres “tablao” ======
        title_nodes = []
        for n in soup.select("h1, h2, h3, h4, h5, h6, strong, b, a, span, p"):
            try:
                txt = norm(n.get_text(" ", strip=True))
            except Exception:
                continue
            if txt and re.search(r"\btablao?s?\b", txt, re.IGNORECASE):
                title_nodes.append(n)

        items, seen = [], set()

        for node in title_nodes:
            title = norm(node.get_text(" ", strip=True)) or "Tablao"

            # 1) extraire autour du titre (tokens éclatés, <time>, attributs…)
            tokens = collect_tokens_around(node)
            dates = dates_from_tokens(tokens)

            # 2) texte “local” pour heure / lieu
            local_text = " \n ".join(tokens)
            hr = nz(extract_time_from_text(local_text))
            lieu = extract_lieu(local_text)

            # 3) fallback : page détail si pas de date
            if not dates:
                href = find_related_href(node)
                if href:
                    d2, hr2, lieu2 = parse_detail(href)
                    if d2: dates = d2
                    if hr2 and not hr: hr = hr2
                    if lieu2 and not lieu: lieu = lieu2

            titre_norm = sanitize_for_voice(title)
            lieu_norm = sanitize_for_voice(lieu)

            # 4) si toujours pas de date, on garde quand même l'info (date vide) — à enlever si tu veux filtrer
            if not dates:
                k = ("", titre_norm.lower()[:160], hr)
                if k not in seen:
                    seen.add(k)
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

            # 5) une entrée par date
            for dd in dates:
                k = (dd, titre_norm.lower()[:160], hr)
                if k in seen: continue
                seen.add(k)
                items.append({
                    "type": "tablao",
                    "date": dd,
                    "date_spoken": ddmmyyyy_to_spoken(dd),
                    "heure": hr,
                    "heure_vocal": remplacer_h_par_heure(hr),
                    "titre": titre_norm,
                    "lieu": lieu_norm,
                })

        # ===== Tri : avec date d'abord =====
        def sort_key(e):
            d = nz(e.get("date"))
            if d:
                try:
                    dd, mm, yy = d.split("/")
                    return (0, int(yy), int(mm), int(dd))
                except Exception:
                    pass
            return (1, 9999, 12, 31)

        items.sort(key=sort_key)

        # ===== Version vocale =====
        tablaos_vocal = []
        for e in items:
            if e.get("date_spoken"):
                parts = [f"Tablao le {e['date_spoken']}"]
            else:
                parts = ["Tablao"]
            if e.get("heure_vocal"): parts.append(f"à {e['heure_vocal']}")
            if e.get("lieu"): parts.append(f"au {e['lieu']}")
            parts.append(f": {e['titre']}")
            tablaos_vocal.append(sanitize_for_voice(" ".join(parts)))

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
