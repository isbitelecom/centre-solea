# solea_api/routes/infos_cours.py
from flask import Blueprint, jsonify, request
import re
from ..utils import (
    fetch_html, soup_from_html, normalize_text,
    cache_key, cache_get, cache_set, cache_meta,
    remplacer_h_par_heure, sanitize_for_voice,
)

bp = Blueprint("infos_cours", __name__)

SRC = "https://www.centresolea.org/horaires-et-tarifs"

# Regex robustes
DAY_TOKEN = (
    r"(?<![A-Za-zÀ-ÖØ-öø-ÿ])"
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"
    r"(?![A-Za-zÀ-ÖØ-öø-ÿ])"
)
HOUR   = r"(?:\d{1,2}\s*(?:h|:)\s*[0-5]?\d|\d{1,2}\s*h)"
H_RANGE= rf"(?:{HOUR}\s*(?:-|–|—|à|a)\s*{HOUR})"
H_ANY  = rf"(?:{H_RANGE}|{HOUR})"
LVL    = r"(débutant(?:e|s)?|debutant(?:e|s)?|interm[ée]diaire|inter(?:\s*1|\s*2)?|avanc[ée]s?|perfectionnement|technique)"
DANCE  = r"(flamenco|s[ée]villan(?:e|es)?)"
PUBLIC = r"(enfants?|ados?|adultes?)"

RE_DAY_TOKEN = re.compile(DAY_TOKEN, re.IGNORECASE)
RE_ANY_HOUR  = re.compile(H_ANY, re.IGNORECASE)
RE_LVL       = re.compile(LVL, re.IGNORECASE)
RE_DANCE     = re.compile(DANCE, re.IGNORECASE)
RE_PUBLIC    = re.compile(PUBLIC, re.IGNORECASE)

RE_PRICE_LINE    = re.compile(r"€")
RE_ADHESION      = re.compile(r"Adh[ée]sion\s+annuelle[^0-9]{0,20}(\d{1,3})\s*€", re.IGNORECASE)
RE_REDUIT_BLOCK  = re.compile(r"(r[ée]duit|r[ée]duction|minima|[ée]tudiant|ch[oô]meur|famille|RSA|CAF|bourse)", re.IGNORECASE)
RE_PAY           = re.compile(r"(paiement|r[èe]glement|trimestre|mensuel|esp[eè]ces|ch[eè]ques|CB|carte|liquide|virement)", re.IGNORECASE)
RE_PAIR_NR       = re.compile(r"^\s*([0-9][0-9 ]{1,3})\s*€\s*\|\s*([0-9][0-9 ]{1,3})\s*€\s*$")
RE_TARIFS_HEADER = re.compile(r"^TARIFS\s+AU\s+TRIMESTRE$", re.IGNORECASE)
RE_TARIFS_CATEGORIES= re.compile(r"(?i)\b(adh[ée]rents?|[ée]l[eè]ves?|non\s*adh[ée]rents?)\b[^0-9]{0,15}([0-9 ][0-9 ]*)\s*€")

DAY_MAP = {
    "lun": "Lundi", "lun.": "Lundi", "lundi": "Lundi",
    "mar": "Mardi", "mar.": "Mardi", "mardi": "Mardi",
    "mer": "Mercredi", "mer.": "Mercredi", "mercredi": "Mercredi",
    "jeu": "Jeudi", "jeu.": "Jeudi", "jeudi": "Jeudi",
    "ven": "Vendredi", "ven.": "Vendredi", "vendredi": "Vendredi",
    "sam": "Samedi", "sam.": "Samedi", "samedi": "Samedi",
    "dim": "Dimanche", "dim.": "Dimanche", "dimanche": "Dimanche",
}
def norm_day(tok: str) -> str:
    t = (tok or "").lower().strip(".")
    return DAY_MAP.get(t, tok.capitalize())

def segment_by_days(text: str, pre_window: int = 160):
    segs = []
    mlist = list(RE_DAY_TOKEN.finditer(text))
    if not mlist:
        return segs
    for i, m in enumerate(mlist):
        start = m.start()
        end = mlist[i + 1].start() if i + 1 < len(mlist) else len(text)
        pre_start = max(0, start - pre_window)
        segs.append({
            "jour": norm_day(m.group(0)),
            "text": text[start:end],
            "pre": text[pre_start:start]
        })
    return segs

@bp.get("/infos-cours")
def infos_cours():
    key = cache_key("infos-cours", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        # RÉCOLTE TEXTE Wix
        text_blocks = []
        for sel in ['[data-hook="richTextElement"]', '[class*="richText"]']:
            for el in soup.select(sel):
                t = normalize_text(el.get_text("\n", strip=True))
                if t:
                    text_blocks.append(t)
        for el in soup.find_all(["h2","h3","h4","p","li"]):
            t = normalize_text(el.get_text("\n", strip=True))
            if t:
                text_blocks.append(t)
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td","th"])]
                row = " | ".join([c for c in cells if c])
                if row:
                    rows.append(row)
            if rows:
                text_blocks.append("\n".join(rows))

        # Lignes à plat
        seen_line, lines = set(), []
        for block in text_blocks:
            for l in re.split(r"\n+", block):
                l2 = normalize_text(l)
                if l2 and l2 not in seen_line:
                    seen_line.add(l2)
                    lines.append(l2)

                # 1) HORAIRES (proximité + préfixe + fallback segment complet)
        horaires, seen_items = [], set()
        PROX_WINDOW = 160  # (garde 160 ou augmente à 240 si besoin)

        def _first_match(regex, text):
            return regex.search(text) if text else None

        for block in text_blocks:
            for seg in segment_by_days(block, pre_window=160):
                seg_txt = seg["text"]
                hour_matches = list(RE_ANY_HOUR.finditer(seg_txt))
                if not hour_matches:
                    continue

                # Fenêtres de proximité autour de la première et de la dernière heure
                h_first = hour_matches[0]
                h_last  = hour_matches[-1]

                s1 = max(0, h_first.start() - PROX_WINDOW)
                e1 = min(len(seg_txt), h_first.end() + PROX_WINDOW)
                vicinity_first = seg_txt[s1:e1]

                s2 = max(0, h_last.start() - PROX_WINDOW)
                e2 = min(len(seg_txt), h_last.end() + PROX_WINDOW)
                vicinity_last = seg_txt[s2:e2]

                pre_tail = seg.get("pre", "")[-PROX_WINDOW:] if seg.get("pre") else ""

                # Ordre de recherche : prox 1 -> prox 2 -> tout segment -> préfixe
                search_spaces = [vicinity_first, vicinity_last, seg_txt, pre_tail]

                lvl_m = dance_m = public_m = None
                for space in search_spaces:
                    lvl_m    = lvl_m    or _first_match(RE_LVL, space)
                    dance_m  = dance_m  or _first_match(RE_DANCE, space)
                    public_m = public_m or _first_match(RE_PUBLIC, space)
                    if lvl_m and dance_m and public_m:
                        break

                hours_text = " ".join(m.group(0) for m in hour_matches)
                item = {
                    "jour": seg["jour"],
                    "heures": hours_text.replace("–","-").replace("—","-"),
                    "heures_vocal": remplacer_h_par_heure(hours_text),
                    "niveau": (lvl_m.group(0).capitalize() if lvl_m else ""),
                    "danse": (sanitize_for_voice(dance_m.group(0)).capitalize() if dance_m else ""),
                    "public": (public_m.group(0).capitalize() if public_m else ""),
                }

                keyi = (item["jour"], item["heures"], item["niveau"], item["danse"], item["public"])
                if keyi not in seen_items:
                    seen_items.add(keyi)
                    horaires.append(item)

        # Filtre anti-faux-positif : Flamenco Débutants Adultes le vendredi
        def _is_flamenco_debutants_adultes_vendredi(item: dict) -> bool:
            jour   = (item.get("jour") or "").strip()
            danse  = (item.get("danse") or "").lower()
            public = (item.get("public") or "").lower()
            niveau = (item.get("niveau") or "").lower().replace("·", "")
            return (
                jour == "Vendredi"
                and "flamenco" in danse
                and "adulte" in public
                and re.search(r"d[ée]but", niveau) is not None
            )
        horaires = [h for h in horaires if not _is_flamenco_debutants_adultes_vendredi(h)]

        # 2) TARIFS
        tarifs_par_nb = {}
        tarifs_lignes = []
        tarifs_categories = {"adherents": [], "eleves": [], "non_adherents": []}
        conditions_reduites, modalites_paiement = [], []

        in_tarifs_block = False
        for l in lines:
            if RE_TARIFS_HEADER.match(l):
                in_tarifs_block = True
                continue
            if in_tarifs_block and not RE_PRICE_LINE.search(l):
                in_tarifs_block = False

            if RE_PRICE_LINE.search(l):
                tarifs_lignes.append(l)
                for cat, prix in RE_TARIFS_CATEGORIES.findall(l):
                    cat_low = cat.lower()
                    price_fmt = f"{prix.replace(' ', '')} €"
                    if "non" in cat_low and "adh" in cat_low:
                        if price_fmt not in tarifs_categories["non_adherents"]:
                            tarifs_categories["non_adherents"].append(price_fmt)
                    elif "adh" in cat_low:
                        if price_fmt not in tarifs_categories["adherents"]:
                            tarifs_categories["adherents"].append(price_fmt)
                    elif "lèv" in cat_low or "élè" in cat_low or "eleve" in cat_low:
                        if price_fmt not in tarifs_categories["eleves"]:
                            tarifs_categories["eleves"].append(price_fmt)

                if in_tarifs_block:
                    m_pair = RE_PAIR_NR.match(l)
                    if m_pair:
                        normal = m_pair.group(1).replace(" ", "")
                        reduit = m_pair.group(2).replace(" ", "")
                        idx = len(tarifs_par_nb) + 1
                        nb = str(idx)
                        tarifs_par_nb.setdefault(nb, {})
                        tarifs_par_nb[nb]["normal"] = f"{normal} €"
                        tarifs_par_nb[nb]["reduit"] = f"{reduit} €"

            if RE_REDUIT_BLOCK.search(l) and "€" not in l and l not in conditions_reduites:
                conditions_reduites.append(l)
            if RE_PAY.search(l) and "€" not in l and l not in modalites_paiement:
                modalites_paiement.append(l)

        # Adhésion annuelle
        full_txt = "\n".join(lines)
        m_ad = RE_ADHESION.search(full_txt)
        adhesion = f"{m_ad.group(1)} €" if m_ad else ""

        # Niveaux Sévillanas
        niveaux_sevillane = []
        for l in lines:
            if re.search(r"s[ée]villan", l, re.IGNORECASE) and RE_LVL.search(l):
                raw = RE_LVL.search(l).group(0).lower()
                norm = "Débutants" if "debut" in raw or "début" in raw else ("Avancés" if "avanc" in raw else raw.capitalize())
                if norm not in niveaux_sevillane:
                    niveaux_sevillane.append(norm)

        # Version vocal
        horaires_vocal = []
        for h in horaires:
            extra = " ".join([x for x in [h["danse"], h["public"], h["niveau"]] if x])
            lead = f"Voici les horaires pour {extra} : " if extra else ""
            phrase = f"{lead}{h['jour']} {h['heures_vocal']}"
            horaires_vocal.append(sanitize_for_voice(phrase))

        payload = {
            "source": SRC,
            "adhesion": adhesion,
            "horaires": horaires,
            "horaires_vocal": horaires_vocal,
            "tarifs_par_nb_cours": tarifs_par_nb,
            "tarifs_lignes": tarifs_lignes,
            "tarifs_categories": tarifs_categories,
            "conditions_reduites": conditions_reduites,
            "modalites_paiement": modalites_paiement,
            "niveaux_sevillane": niveaux_sevillane
        }
        cache_set(key, payload, ttl_seconds=60)
        return jsonify({**payload, "cache": cache_meta(True, entry)})

    except Exception as e:
        if entry:
            return jsonify({**entry["data"], "cache": cache_meta(False, entry)})
        return jsonify({"erreur": str(e)}), 500
