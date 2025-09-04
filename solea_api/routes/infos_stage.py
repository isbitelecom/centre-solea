# solea_api/routes/infos_stage.py
from flask import Blueprint, jsonify
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

bp = Blueprint("infos_stage", __name__)
SRC = "https://www.centresolea.org/stages"
TZ = ZoneInfo("Europe/Madrid")

# ---------------- Utils texte ----------------
def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "\n").replace("\t", " ")
    s = s.replace("\u00a0", " ").replace("–", "-").replace("—", "-").replace("\u2011", "-")
    s = re.sub(r"[ \u2009\u202f]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def safe_int(x, default=None):
    try:
        return int(str(x))
    except Exception:
        return default

# ---------------- Mois & dates ----------------
MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "janv.": 1, "févr": 2, "févr.": 2, "fevr": 2, "fevr.": 2,
    "sept": 9, "sept.": 9, "oct": 10, "oct.": 10, "nov": 11, "nov.": 11,
    "déc": 12, "déc.": 12, "dec": 12, "dec.": 12
}
MONTH_WORD = r"(?:janv\.?|janvier|févr\.?|fevr\.?|février|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"

def month_to_int_any(m):
    if m is None:
        return None
    if isinstance(m, int):
        return m if 1 <= m <= 12 else None
    s = str(m).strip().lower().rstrip(".")
    if s.isdigit():
        v = safe_int(s)
        return v if v and 1 <= v <= 12 else None
    return MONTHS_FR.get(s)

def infer_school_year_for_month(mon: int) -> int:
    """Année scolaire: sept->août = année en cours; janv-août = année suivante si on est déjà ≥ sept."""
    now = datetime.now(TZ)
    if mon >= 9:
        return now.year
    return now.year + 1 if now.month >= 9 else now.year

def fmt_date(y, m, d) -> str:
    mi = month_to_int_any(m)
    if not mi:
        return ""
    di = safe_int(d)
    if di is None:
        return ""
    yi = safe_int(y)
    if yi is None:
        yi = infer_school_year_for_month(mi)
    return f"{di:02d}/{mi:02d}/{yi:04d}"

def spoken_date(ddmmyyyy: str) -> str:
    """'12/10/2025' -> '12 octobre 2025' ; tolère 2 ou 3 segments."""
    if not ddmmyyyy:
        return ""
    parts = ddmmyyyy.split("/")
    if len(parts) == 2:  # d/m -> on infère l'année scolaire
        d, m = parts
        m_int = safe_int(m) or month_to_int_any(m)
        if not m_int:
            return ddmmyyyy
        y = infer_school_year_for_month(m_int)
        parts = [d, str(m_int), str(y)]
    if len(parts) != 3:
        return ddmmyyyy
    d, m, y = parts
    d_i, m_i, y_i = safe_int(d), safe_int(m), safe_int(y)
    if not d_i or not m_i:
        return ddmmyyyy
    names = ["","janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]
    return f"{d_i} {names[m_i]} {y_i}" if y_i else f"{d_i} {names[m_i]}"

# ---------------- Heures ----------------
RE_HRANGE_1 = re.compile(r"\b(\d{1,2})\s*h\s*([0-5]?\d)?\s*[-–—]\s*(\d{1,2})\s*h\s*([0-5]?\d)?\b")
RE_HRANGE_2 = re.compile(r"\b(\d{1,2})\s*:\s*([0-5]?\d)\s*[-–—/]\s*(\d{1,2})\s*:\s*([0-5]?\d)\b")
RE_HSINGLE  = re.compile(r"\b(\d{1,2})\s*[:h]\s*([0-5]?\d)?\b")

def heures_from_line(line: str) -> list[str]:
    out = []
    for m in RE_HRANGE_1.finditer(line):
        h1, m1, h2, m2 = m.groups()
        seg = f"{int(h1)}h{int(m1):02d}" if m1 else f"{int(h1)}h"
        seg += " - "
        seg += f"{int(h2)}h{int(m2):02d}" if m2 else f"{int(h2)}h"
        out.append(seg)
    for m in RE_HRANGE_2.finditer(line):
        h1, m1, h2, m2 = m.groups()
        out.append(f"{int(h1)}h{int(m1):02d} - {int(h2)}h{int(m2):02d}")
    # éviter de rajouter des simples si un range est déjà présent sur la même ligne
    if not out:
        for m in RE_HSINGLE.finditer(line):
            h, mn = m.groups()
            out.append(f"{int(h)}h{int(mn):02d}" if mn else f"{int(h)}h")
    return out

def heure_vocale(s: str) -> str:
    def repl(m):
        h = safe_int(m.group(1), 0)
        mn = m.group(2)
        if not mn or re.fullmatch(r"0+", mn):
            return f"{h} heures"
        return f"{h} heures {safe_int(mn, 0)}"
    return RE_HSINGLE.sub(repl, s)

# ---------------- “Jota” pour TTS ----------------
SPANISH_J = {"jaleo","jaleos","cajon","cajón","jesus","jesús","jose","josé","juan","jota","jerez"}
def jotaize_word(w: str) -> str:
    base = (
        w.lower()
        .replace("á","a").replace("é","e").replace("í","i")
        .replace("ó","o").replace("ú","u").replace("ü","u").replace("ñ","n")
    )
    if base in SPANISH_J or any(base.startswith(x) for x in ("jaleo","cajon","jesu","jose","juan","jota","jerez")):
        return "".join(("Kh" if ch=="J" else "kh") if ch in ("J","j") else ch for ch in w)
    return w

def tts_jota(text: str) -> str:
    tokens = re.split(r"(\W+)", text or "")
    return "".join(jotaize_word(tok) if re.match(r"\w+", tok) else tok for tok in tokens)

# ---------------- Détection contenu ----------------
KEYWORDS = re.compile(r"(?i)\b(master\s*-?\s*class|masterclass|stage[s]?|atelier\s+d[’']immersion|atelier[s]?)\b")
RE_PRICE_ANY = re.compile(r"€")
RE_TARIF_LINE = re.compile(r"(?i)\b(adh[ée]rents?|non\s*adh[ée]rents?|[ée]l[eè]ves?|élèves?|eleves?)\b.*?\d+\s*€")

RE_RANGE  = re.compile(rf"(?i)\bdu\s+(\d{{1,2}})\s+au\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_DUO    = re.compile(rf"(?i)\b(\d{{1,2}})\s+et\s+(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?")
RE_SINGLE = re.compile(rf"(?i)\b(\d{{1,2}})\s+({MONTH_WORD})(?:\s+(\d{{4}}))?\b")
RE_NUM    = re.compile(r"(?i)\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b")

# Bruit dur (menus, footer, accessibilité, etc.)
RE_NOISE = re.compile(
    r"(?i)^(top of page|bottom of page|use tab to navigate|newsletter|abonnez vous|centre solea -|suivez(-| )?nous|"
    r"l'?ecole de danse|les cours|horaires(?: et tarifs)?|le lieu|infos ?/ ?contact|agenda 20|"
    r"événements? ?(à|a) venir|solea productions|les compagnies|festival flamenco azul)$"
)

def is_noise(line: str) -> bool:
    if not line:
        return True
    if RE_NOISE.match(line.strip()):
        return True
    # Entêtes tout en majuscules très courts
    if len(line) <= 30 and re.fullmatch(r"[A-ZÉÈÀÙÂÊÎÔÛÄËÏÖÜÇ0-9 \-'/]+", line):
        return True
    return False

def classify_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"master\s*-?\s*class|masterclass", t): return "master class"
    if re.search(r"atelier\s+d[’']immersion|immersion", t): return "atelier d'immersion"
    if re.search(r"\batelier[s]?\b", t): return "atelier"
    if re.search(r"\bstage[s]?\b", t): return "stage"
    return "evenement"

def detect_date_block(s: str):
    s = s or ""
    m = RE_RANGE.search(s)
    if m:
        d1, d2, mon, y = m.group(1), m.group(2), m.group(3), m.group(4)
        y = safe_int(y)
        return fmt_date(y, mon, d1), fmt_date(y, mon, d2)
    m = RE_DUO.search(s)
    if m:
        d1, d2, mon, y = m.group(1), m.group(2), m.group(3), m.group(4)
        y = safe_int(y)
        return fmt_date(y, mon, d1), fmt_date(y, mon, d2)
    m = RE_SINGLE.search(s)
    if m:
        d, mon, y = m.group(1), m.group(2), m.group(3)
        y = safe_int(y)
        return fmt_date(y, mon, d), ""
    m = RE_NUM.search(s)
    if m:
        d, mo, yy = m.group(1), m.group(2), m.group(3)
        y = safe_int(yy)
        if y is not None and y < 100:
            y += 2000
        return fmt_date(y, safe_int(mo), d), ""
    return "", ""

def extract_lines(html: str):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","noscript","iframe","svg"]):
        tag.decompose()
    lines = []
    for el in soup.find_all(["h1","h2","h3","h4","p","li","span","div"]):
        t = normalize_text(el.get_text(" ", strip=True))
        if t:
            lines.append(t)
    # dédoublonner immédiat
    dedup, prev = [], None
    for t in lines:
        if t != prev:
            dedup.append(t)
        prev = t
    return dedup

# ---------------- Endpoint ----------------
@bp.get("/infos-stage")
def infos_stage():
    try:
        r = requests.get(SRC, timeout=12)
        r.raise_for_status()
        lines = extract_lines(r.text)

        items = []
        current = None

        for raw in lines:
            line = raw.strip()

            if is_noise(line):
                continue

            # Démarrage d'un nouveau bloc UNIQUEMENT sur mot-clé
            if KEYWORDS.search(line):
                # Finaliser le précédent si non vide
                if current and any([current.get("date"), current.get("date_fin"),
                                    current["heures"], current["tarifs"], current.get("description")]):
                    current["titre_vocal"] = tts_jota(current.get("titre",""))
                    if current.get("description"):
                        current["description_vocal"] = tts_jota(heure_vocale(current["description"]))
                    # vocaliser les heures (élément par élément)
                    current["heures_vocal"] = [heure_vocale(h) for h in current["heures"]]
                    # date_spoken propre
                    if current.get("date") and current.get("date_fin"):
                        current["date_spoken"] = f"du {spoken_date(current['date'])} au {spoken_date(current['date_fin'])}"
                    elif current.get("date"):
                        current["date_spoken"] = spoken_date(current["date"])
                    items.append(current)

                # Nouveau bloc
                d1, d2 = detect_date_block(line)
                titre = line
                # si la date est sur la même ligne, l’enlever du titre
                if d1 or d2:
                    # retirer le premier motif de date de la ligne
                    titre = re.sub(rf"(?i)\bdu\s+\d{{1,2}}\s+au\s+\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?\b", "", titre)
                    titre = re.sub(rf"(?i)\b\d{{1,2}}\s+et\s+\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?\b", "", titre)
                    titre = re.sub(rf"(?i)\b\d{{1,2}}\s+{MONTH_WORD}(?:\s+\d{{4}})?\b", "", titre)
                    titre = re.sub(r"(?i)\b\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?\b", "", titre)
                    titre = titre.strip(" ,;:.-")

                typ = classify_type(titre or line)
                current = {
                    "type": typ,
                    "titre": titre[:240] if titre else typ.title(),
                    "date": d1,
                    "date_fin": d2,
                    "date_spoken": "",
                    "heures": [],
                    "heures_vocal": [],
                    "tarifs": [],
                    "description": "",
                    "sessions": []  # dates additionnelles (listes type "21 septembre", etc.)
                }
                continue

            # Si pas de bloc en cours, ignorer la ligne
            if not current:
                continue

            # Dans un bloc : chercher dates → remplir ou pousser en sessions
            d1, d2 = detect_date_block(line)
            if d1 or d2:
                if not current["date"]:
                    current["date"] = d1
                elif not current["date_fin"] and d2:
                    current["date_fin"] = d2
                else:
                    # dates additionnelles (sessions)
                    if d2:
                        current["sessions"].append({"date": d1, "date_fin": d2})
                    else:
                        current["sessions"].append({"date": d1})
                continue

            # Heures → en petites unités propres
            hrs = heures_from_line(line)
            if hrs:
                for h in hrs:
                    if h not in current["heures"]:
                        current["heures"].append(h)
                continue

            # Tarifs
            if RE_TARIF_LINE.search(line) or (RE_PRICE_ANY.search(line) and len(line) < 220):
                if line not in current["tarifs"]:
                    current["tarifs"].append(line)
                continue

            # Description
            if not is_noise(line):
                if len(current["description"]) < 1000:
                    current["description"] = (current["description"] + " " + line).strip()

        # Finaliser le dernier bloc
        if current and any([current.get("date"), current.get("date_fin"),
                            current["heures"], current["tarifs"], current.get("description")]):
            current["titre_vocal"] = tts_jota(current.get("titre",""))
            if current.get("description"):
                current["description_vocal"] = tts_jota(heure_vocale(current["description"]))
            current["heures_vocal"] = [heure_vocale(h) for h in current["heures"]]
            if current.get("date") and current.get("date_fin"):
                current["date_spoken"] = f"du {spoken_date(current['date'])} au {spoken_date(current['date_fin'])}"
            elif current.get("date"):
                current["date_spoken"] = spoken_date(current["date"])
            items.append(current)

        # Nettoyage / filtrage final
        cleaned = []
        seen = set()
        for it in items:
            # garder seulement les blocs avec type reconnu ET (date|heures|tarifs)
            if it["type"] not in {"stage","master class","atelier","atelier d'immersion","evenement"}:
                continue
            if not (it.get("date") or it.get("date_fin") or it["heures"] or it["tarifs"]):
                continue
            key = (it["type"], it.get("titre","")[:160], it.get("date",""), it.get("date_fin",""))
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(it)

        return jsonify({"source": SRC, "count": len(cleaned), "items": cleaned})

    except Exception as e:
        return jsonify({"source": SRC, "error": str(e)}), 500
