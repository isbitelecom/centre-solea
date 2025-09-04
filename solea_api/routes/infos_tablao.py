# solea_api/routes/infos_tablao.py
from flask import Blueprint, jsonify, request
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import NavigableString

from ..utils import (
    fetch_html, soup_from_html, normalize_text, sanitize_for_voice,
    extract_time_from_text, ddmmyyyy_to_spoken,
    cache_key, cache_get, cache_set, cache_meta, remplacer_h_par_heure
)

bp = Blueprint("infos_tablao", __name__)

BASE = "https://www.centresolea.org"
SRC  = f"{BASE}/"  # on part de la home et on suit les liens /events/… contenant “tablao”


def _nz(s):
    return s if isinstance(s, str) else ""

def _norm(s):
    return normalize_text(_nz(s))

# ---- Dates FR ---------------------------------------------------------------

MONTHS = {
    # plein
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    # abréviations fréquentes (sans/avec point)
    "janv": 1, "janv.": 1, "jan": 1, "jan.": 1,
    "févr": 2, "févr.": 2, "fevr": 2, "fevr.": 2, "fév": 2, "fév.": 2, "fev": 2, "fev.": 2,
    "avr": 4, "avr.": 4,
    "juil": 7, "juil.": 7,
    "aou": 8, "aou.": 8,
    "sept": 9, "sept.": 9, "sep": 9, "sep.": 9,
    "oct": 10, "oct.": 10,
    "nov": 11, "nov.": 11,
    "déc": 12, "déc.": 12, "dec": 12, "dec.": 12,
}

JOURS = r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"

# “26 sept. 2025”, “26 septembre 2025”, “26 sept.”
RX_DATE_WORDS = re.compile(
    rf"(?:{JOURS}\s+)?(\d{{1,2}}(?:er)?)\s+([A-Za-zéèêëàâîïôöùûç\.]+)\s*(?:,?\s*(20\d{{2}}))?",
    re.IGNORECASE
)
# “26/09[/2025]”, “26-09[-2025]”
RX_DATE_NUM = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b")

# plages : “26 sept. 2025 … – 27 sept. 2025 …” ou “du 26 sept. au 27 sept. 2025”
RX_RANGE_WORDS = re.compile(
    rf"(?:du\s+)?(?:{JOURS}\s+)?(\d{{1,2}}(?:er)?)\s+([A-Za-zéèêëàâîïôöùûç\.]+)\s*(?:,?\s*(20\d{{2}}))?"
    r".{0,40}?(?:–|-|au)\s+(?:{JOURS}\s+)?(\d{1,2}(?:er)?)\s+([A-Za-zéèêëàâîïôöùûç\.]+)\s*(?:,?\s*(20\d{2}))?",
    re.IGNORECASE
)

def _infer_year(mm: int, explicit: int | None) -> int | None:
    if explicit:
        return explicit
    # défaut: année courante Europe/Madrid
    now = datetime.now(timezone.utc).astimezone()
    return now.year

def _month_from_token(tok: str) -> int:
    t = (_nz(tok)).strip().lower()
    return MONTHS.get(t, 0)

def _ddmmyyyy_from_words(day_s: str, month_s: str, year_s: str | None) -> str:
    if not day_s or not month_s:
        return ""
    try:
        dd = int(re.sub(r"er$", "", day_s, flags=re.IGNORECASE))
    except Exception:
        return ""
    mm = _month_from_token(month_s)
    yy = int(year_s) if year_s else _infer_year(mm, None)
    if 1 <= dd <= 31 and 1 <= mm <= 12 and yy:
        return f"{dd:02d}/{mm:02d}/{yy}"
    return ""

def _ddmmyyyy_from_num(day_s: str, month_s: str, year_s: str | None) -> str:
    try:
        dd = int(day_s); mm = int(month_s)
    except Exception:
        return ""
    yy = int(year_s) if year_s else _infer_year(mm, None)
    if 1 <= dd <= 31 and 1 <= mm <= 12 and yy:
        return f"{dd:02d}/{mm:02d}/{yy}"
    return ""

def _expand_range_words(m: re.Match) -> list[str]:
    d1, m1, y1 = m.group(1), m.group(2), m.group(3)
    d2, m2, y2 = m.group(4), m.group(5), m.group(6)
    s = _ddmmyyyy_from_words(d1, m1, y1)
    e = _ddmmyyyy_from_words(d2, m2 or m1, y2)
    if not (s and e):
        return []
    try:
        sd, sm, sy = [int(x) for x in s.split("/")]
        ed, em, ey = [int(x) for x in e.split("/")]
        start = date(sy, sm, sd); end = date(ey, em, ed)
        if end < start:
            return []
        out, cur = [], start
        while cur <= end:
            out.append(f"{cur.day:02d}/{cur.month:02d}/{cur.year}")
            cur += timedelta(days=1)
        return out
    except Exception:
        return []

def _any_dates(text: str) -> list[str]:
    t = _nz(text)
    out = []
    for mr in RX_RANGE_WORDS.finditer(t):
        out.extend(_expand_range_words(mr))
    for m in RX_DATE_WORDS.finditer(t):
        d = _ddmmyyyy_from_words(m.group(1), m.group(2), m.group(3))
        if d:
            out.append(d)
    for m in RX_DATE_NUM.finditer(t):
        d = _ddmmyyyy_from_num(m.group(1), m.group(2), m.group(3))
        if d:
            out.append(d)
    # uniq en conservant l'ordre
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return uniq

# ---- Parsing des pages “événement” ------------------------------------------

def _parse_event_page(url: str):
    """
    Retourne (titre, dates[], heure, lieu) pour une page /events/… Wix.
    On s'appuie sur le bloc “Heure et lieu” qui contient toujours la date avec l'année.
    """
    try:
        html = fetch_html(url)
        soup = soup_from_html(html)
    except Exception:
        return "", [], "", ""

    full = _norm(soup.get_text("\n", strip=True))

    # Titre: prioriser H1
    title = ""
    h1 = soup.find(["h1","h2"])
    if h1:
        title = _norm(h1.get_text(" ", strip=True))
    if not title:
        title = _norm(soup.title.get_text() if soup.title else "")

    # 1) Essayer directement les balises <time datetime=…>
    dates = []
    for t in soup.select("time[datetime]"):
        dt = _nz(t.get("datetime"))
        # formats ISO possibles: 2025-09-26T20:30:00.000Z
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", dt)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dates.append(f"{d:02d}/{mo:02d}/{y}")

    # 2) Sinon, extraire depuis le texte (notamment la ligne “Heure et lieu …”)
    if not dates:
        dates = _any_dates(full)

    # 3) Heure: on prend la première heure (20:30 / 20h30)
    hr = _nz(extract_time_from_text(full))

    # 4) Lieu: heuristique simple — la ligne contenant Marseille/adresse
    lieu = ""
    for line in re.split(r"\n+", full):
        if re.search(r"(Marseille|Rue|France|130\d{2})", line, re.IGNORECASE):
            lieu = _norm(line)
            break

    return _norm(title), dates, hr, _norm(lieu)

# ---- Collecte des liens tablao depuis la home --------------------------------

def _find_tablao_event_links(home_soup):
    urls = set()
    for a in home_soup.find_all("a"):
        href = _nz(a.get("href"))
        if not href:
            continue
        absu = urljoin(BASE, href)
        txt  = _norm(a.get_text(" ", strip=True))
        if "/events/" in absu and (re.search(r"\btablao\b", absu, re.IGNORECASE) or re.search(r"\btablao\b", txt, re.IGNORECASE)):
            urls.add(absu)
    return sorted(urls)

# ------------------------------------------------------------------------------
@bp.get("/infos-tablao")
def infos_tablao():
    key = cache_key("infos-tablao", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        # 1) Home -> liens “/events/…tablao…”
        html = fetch_html(SRC)
        soup = soup_from_html(html)
        event_links = _find_tablao_event_links(soup)

        items, seen = [], set()

        # 2) Pour chaque page événement, parser
        for url in event_links:
            titre, dates, hr, lieu = _parse_event_page(url)
            if not titre:
                # fallback: titre depuis l'ancre (si pas de H1)
                titre = "Tablao"

            # Si pas de date trouvée, ignorer (on veut uniquement les prochains tablaos)
            if not dates:
                continue

            # 3) Filtre “à venir” (>= aujourd’hui, fuseau local)
            today = datetime.now().date()
            for dd in dates:
                try:
                    d, m, y = [int(x) for x in dd.split("/")]
                    d_obj = date(y, m, d)
                except Exception:
                    continue
                if d_obj < today:
                    continue

                keyi = (dd, titre.lower()[:160])
                if keyi in seen:
                    continue
                seen.add(keyi)

                items.append({
                    "type": "tablao",
                    "date": dd,
                    "date_spoken": ddmmyyyy_to_spoken(dd),
                    "heure": hr,
                    "heure_vocal": remplacer_h_par_heure(hr),
                    "titre": sanitize_for_voice(titre),
                    "lieu": sanitize_for_voice(lieu),
                    "url": url
                })

        # 4) Tri chronologique
        def _k(e):
            try:
                dd, mm, yy = e["date"].split("/")
                return (int(yy), int(mm), int(dd))
            except Exception:
                return (9999, 12, 31)
        items.sort(key=_k)

        # 5) Version vocale
        tablaos_vocal = []
        for e in items:
            parts = [f"Tablao le {e['date_spoken']}"]
            if e.get("heure_vocal"):
                parts.append(f"à {e['heure_vocal']}")
            if e.get("lieu"):
                parts.append(f"au {e['lieu']}")
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
