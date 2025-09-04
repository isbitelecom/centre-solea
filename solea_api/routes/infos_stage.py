# solea_api/routes/infos_stage.py
from flask import Blueprint, jsonify
import re
import requests
from bs4 import BeautifulSoup
from ..utils import normalize_text

bp = Blueprint("infos_stage", __name__)
SRC = "https://www.centresolea.org/stages"

# --- Regex ---
MONTH_WORD = r"(janv\.?|janvier|févr\.?|fevr\.?|février|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"
RE_DATE = re.compile(rf"(?i)(\d{{1,2}}(?:\s+et\s+\d{{1,2}})?\s+{MONTH_WORD}(?:\s+\d{{4}})?)")
RE_PRICE = re.compile(r"\d+\s*€")
RE_HOUR = re.compile(r"\b(\d{1,2})h0{0,2}\b")  # capture 20h, 20h0, 20h00

KEYWORDS = re.compile(r"(?i)\b(stage|master\s*-?\s*class|atelier[s]?|immersion)\b")

# --- Heures vocales ---
def heure_vocal(texte: str) -> str:
    """Convertit 20h00 -> 20 heures ; 20h30 -> 20 heures 30"""
    def repl(m):
        h = int(m.group(1))
        mn = m.group(0).split("h")[1]
        if not mn or mn == "0" or mn == "00":
            return f"{h} heures"
        else:
            return f"{h} heures {int(mn)}"
    return RE_HOUR.sub(repl, texte)

# --- Extraction utils ---
def extract_lines(html: str):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","noscript","iframe","svg"]):
        tag.decompose()
    lines = []
    for el in soup.find_all(["h1","h2","h3","h4","p","li"]):
        t = normalize_text(el.get_text(" ", strip=True))
        if t:
            lines.append(t)
    return lines

# --- Endpoint ---
@bp.get("/infos-stage")
def infos_stage():
    r = requests.get(SRC, timeout=12)
    r.raise_for_status()
    lines = extract_lines(r.text)

    items = []
    current = None

    for line in lines:
        # Détection d’un nouveau bloc (titre avec mot-clé ou date)
        if KEYWORDS.search(line) or RE_DATE.search(line):
            if current:
                items.append(current)
            current = {
                "titre": line,
                "date": RE_DATE.search(line).group(0) if RE_DATE.search(line) else "",
                "description": "",
                "tarifs": [],
                "heures": []
            }
            continue

        # Accumulation dans un bloc en cours
        if current:
            if RE_PRICE.search(line):
                current["tarifs"].append(line)
            elif "h" in line and re.search(r"\d{1,2}h", line):
                current["heures"].append(heure_vocal(line))
            else:
                # éviter les menus du site
                if not re.match(r"(?i)(l'?ecole|les cours|horaires et tarifs|le lieu|infos|contact|newsletter|suivez-nous)", line):
                    current["description"] += " " + line

    if current:
        items.append(current)

    return jsonify({
        "source": SRC,
        "count": len(items),
        "items": items
    })
