# solea_api/utils.py
from __future__ import annotations
import re, json, time, os
from datetime import datetime
from typing import Any
import requests
from bs4 import BeautifulSoup, Tag, NavigableString

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# =========================
# Config & HTTP
# =========================
DEFAULT_TZ = "Europe/Madrid"
REQ_TIMEOUT = (6, 10)
HEADERS_A = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}
HEADERS_B = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# =========================
# Cache mémoire simple
# =========================
_CACHE: dict[str, dict[str, Any]] = {}

def cache_key(name: str, params: dict | None = None) -> str:
    params = params or {}
    return name + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)

def cache_set(key: str, data: dict, ttl_seconds: int = 60) -> None:
    _CACHE[key] = {"data": data, "ts": time.time(), "ttl": ttl_seconds}

def cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    if (time.time() - entry["ts"]) > entry["ttl"]:
        return None
    return entry

def cache_meta(fresh: bool, prev_entry):
    return {
        "fresh": fresh,
        "generated_at": datetime.now(ZoneInfo(DEFAULT_TZ) if ZoneInfo else None).isoformat(),
        "age_seconds": 0 if fresh or not prev_entry else int(time.time() - prev_entry["ts"])
    }

# =========================
# Texte & Dates
# =========================
NBSP = u"\xa0"
MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "janv.": 1, "févr": 2, "févr.": 2, "fevr": 2, "fevr.": 2,
    "sept": 9, "sept.": 9, "oct": 10, "oct.": 10, "nov": 11, "nov.": 11,
    "déc": 12, "déc.": 12, "dec": 12, "dec.": 12
}
MONTHS_FR_SPOKEN = [
    "", "janvier","février","mars","avril","mai","juin",
    "juillet","août","septembre","octobre","novembre","décembre"
]
ACRONYM_WHITELIST = {"PDF","URL","FAQ","SMS","TTC","TVA","API","GPS","USB","NFC","HTTP","HTTPS"}

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "\n").replace("\t", " ")
    s = s.replace(NBSP, " ").replace("–", "-").replace("—", "-").replace(u"\u2011", "-")
    s = re.sub(r"[ \u2009\u202f]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def ddmmyyyy_to_spoken(ddmmyyyy: str) -> str:
    if not ddmmyyyy:
        return ""
    try:
        d, m, y = ddmmyyyy.split("/")
        return f"{int(d)} {MONTHS_FR_SPOKEN[int(m)]} {int(y)}"
    except Exception:
        return ddmmyyyy

def infer_school_year_for_month(mon: int | None) -> int:
    now = datetime.now(ZoneInfo(DEFAULT_TZ) if ZoneInfo else None)
    if not isinstance(mon, int):
        return now.year
    if mon >= 9:
        return now.year
    if now.month >= 9:
        return now.year + 1
    return now.year

def month_to_int_any(m) -> int | None:
    if m is None:
        return None
    if isinstance(m, int):
        return m
    s = str(m).strip().lower().rstrip(".")
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return MONTHS_FR.get(s)

def fmt_date(y, m, d) -> str:
    """Formate dd/mm/yyyy en tolérant le mois texte (ex: 'oct', 'oct.')."""
    mi = month_to_int_any(m)
    if mi is None:
        return ""
    try:
        di = int(d)
    except Exception:
        return ""
    if y is None:
        y = infer_school_year_for_month(mi)
    try:
        yi = int(y)
    except Exception:
        return ""
    return f"{di:02d}/{mi:02d}/{yi:04d}"

def parse_date_any(s: str) -> str:
    """Retourne dd/mm/yyyy si trouvée dans s (formats 12/10/2024, 12 oct. 2024, etc.)."""
    s0 = (s or "").strip().lower().replace("1er", "1")
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", s0)
    if m:
        d, mon, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if y else None
        if y is not None and y < 100:
            y += 2000
        if y is None:
            y = infer_school_year_for_month(mon)
        return f"{d:02d}/{mon:02d}/{y:04d}"
    m2 = re.search(r"(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*(\d{1,2})\s+([a-zéûîôàèùç\.]{3,12})\.?\s*(\d{4})?", s0, flags=re.IGNORECASE)
    if m2:
        d = int(m2.group(1))
        mon = month_to_int_any(m2.group(2))
        if mon:
            y = int(m2.group(3)) if m2.group(3) else infer_school_year_for_month(mon)
            return f"{d:02d}/{mon:02d}/{y:04d}"
    return ""

# =========================
# HTTP helpers
# =========================
def fetch_html(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS_A, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        if r.encoding is None:
            r.encoding = "utf-8"
        txt = r.text
        # si très peu de texte (Wix pre-render), on réessaie
        if len(normalize_text(BeautifulSoup(txt, "lxml").get_text(" ", strip=True))) < 200:
            r2 = requests.get(url, headers=HEADERS_B, timeout=(REQ_TIMEOUT[0], max(REQ_TIMEOUT[1], 14)))
            r2.raise_for_status()
            r2.encoding = r2.encoding or "utf-8"
            return r2.text
        return txt
    except Exception:
        r = requests.get(url, headers=HEADERS_B, timeout=(REQ_TIMEOUT[0], max(REQ_TIMEOUT[1], 14)))
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text

def soup_from_html(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html or "", "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return soup

# =========================
# Heures (regex)
# =========================
H_RANGE_1 = re.compile(r"\b(\d{1,2})\s*h\s*([0-5]?\d)?\s*[-–—]\s*(\d{1,2})\s*h\s*([0-5]?\d)?\b", re.IGNORECASE)
H_RANGE_2 = re.compile(r"\b(\d{1,2})\s*:\s*([0-5]\d)\s*[-–—]\s*(\d{1,2})\s*:\s*([0-5]\d)\b")
H_AT = re.compile(r"\b(?:à|a)\s*(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?\b", re.IGNORECASE)
H_ANY = re.compile(r"\b(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?\b", re.IGNORECASE)

def extract_time_from_text(s: str) -> str:
    s = s or ""
    m = H_RANGE_1.search(s) or H_RANGE_2.search(s)
    if m:
        def fmt(h, mn): return f"{int(h)}h{int(mn):02d}" if mn else f"{int(h)}h"
        return f"{fmt(m.group(1), m.group(2))} - {fmt(m.group(3), m.group(4))}"
    m = H_AT.search(s)
    if m:
        return f"{int(m.group(1))}h{int(m.group(2)):02d}" if m.group(2) else f"{int(m.group(1))}h"
    m = H_ANY.search(s)
    if m:
        return f"{int(m.group(1))}h{int(m.group(2)):02d}" if m.group(2) else f"{int(m.group(1))}h"
    return ""

# =========================
# Sanitize TTS & Types
# =========================
def _fix_known_words(word: str) -> str | None:
    if re.fullmatch(r"TABLAOS?", word, flags=re.IGNORECASE):
        return "Tablao" if word.lower().endswith("o") else "Tablaos"
    if word.upper() in {"VIVACITE", "VIVACITÉ"}:
        return "Vivacité"
    return None

def sanitize_for_voice(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\bTABLAOS?\b", lambda m: _fix_known_words(m.group(0)) or m.group(0), text, flags=re.IGNORECASE)
    text = re.sub(r"\bVIVACIT[ÉE]\b", lambda m: _fix_known_words(m.group(0)) or m.group(0), text, flags=re.IGNORECASE)
    def repl_caps(m: re.Match) -> str:
        w = m.group(0)
        if w in ACRONYM_WHITELIST:
            return w
        fix = _fix_known_words(w)
        if fix:
            return fix
        return w.capitalize()
    return re.sub(r"\b[A-ZÉÈÀÙÂÊÎÔÛÄËÏÖÜÇ]{3,}\b", repl_caps, text)

TYPE_PATTERNS = [
    ("festival", re.compile(r"\bfestival\b", re.IGNORECASE)),
    ("atelier immersion", re.compile(r"atelier\s+d[’']immersion|immersion", re.IGNORECASE)),
    ("masterclass", re.compile(r"master\s*-?\s*class|masterclass", re.IGNORECASE)),
    ("stage", re.compile(r"\bstage[s]?\b", re.IGNORECASE)),
    ("tablao", re.compile(r"\btablao[xs]?\b", re.IGNORECASE)),
    ("atelier", re.compile(r"\batelier[s]?\b", re.IGNORECASE)),
    ("spectacle", re.compile(r"\bspectacle\b", re.IGNORECASE)),
]

def classify_type(*texts) -> str:
    t = " ".join([x for x in texts if x]).lower()
    for label, pat in TYPE_PATTERNS:
        if pat.search(t):
            return label
    return "evenement"

# =========================
# JSON-LD helper
# =========================
def extract_ldjson_events(html: str) -> list[dict]:
    out = []
    try:
        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(tag.text)
            except Exception:
                continue
            candidates = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for d in candidates:
                if isinstance(d, dict) and "@type" in d and "Event" in str(d.get("@type")):
                    out.append(d)
                if isinstance(d, dict):
                    for k in ("@graph", "events", "itemListElement"):
                        if isinstance(d.get(k), list):
                            out.extend([x for x in d[k] if isinstance(x, dict)])
    except Exception:
        pass
    return out

def norm_event_from_ld(d: dict) -> dict:
    name = (d.get("name") or "").strip()
    descr = (d.get("description") or "").strip()
    when = d.get("startDate") or d.get("startTime") or ""
    ddmmyyyy = ""
    heure = ""
    if when:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*?(\d{2}):(\d{2}))?", when)
        if m:
            ddmmyyyy = f"{int(m.group(3)):02d}/{int(m.group(2)):02d}/{int(m.group(1)):04d}"
            if m.group(4):
                heure = f"{int(m.group(4))}h{int(m.group(5)):02d}"
        else:
            ddmmyyyy = parse_date_any(when)
            heure = extract_time_from_text(when)
    if not ddmmyyyy:
        ddmmyyyy = parse_date_any(name + " " + descr)
    if not heure:
        heure = extract_time_from_text(name + " " + descr)
    loc = ""
    locobj = d.get("location")
    if isinstance(locobj, dict):
        loc = locobj.get("name") or locobj.get("address") or ""
    elif isinstance(locobj, str):
        loc = locobj
    return {
        "name": name,
        "description": descr,
        "date": ddmmyyyy,
        "date_spoken": ddmmyyyy_to_spoken(ddmmyyyy),
        "heure": heure,
        "heure_vocal": remplacer_h_par_heure(heure),
        "location": loc
    }

def remplacer_h_par_heure(texte: str) -> str:
    if not texte:
        return ""
    def repl(m: re.Match) -> str:
        h = int(m.group(1)); mn = m.group(2)
        if mn is None or re.fullmatch(r"0+", mn or ""):
            return f"{h} heure"
        return f"{h} heure {int(mn)}"
    texte = re.sub(r"\b(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?\b", repl, texte, flags=re.IGNORECASE)
    texte = re.sub(r"\s?[-–—]\s?", " - ", texte)
    texte = re.sub(r"\s{2,}", " ", texte).strip()
    return texte
