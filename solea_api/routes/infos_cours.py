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

# =========================
# Regex de base (jours/heures)
# =========================
DAY_TOKEN = (
    r"(?<![A-Za-zÀ-ÖØ-öø-ÿ])"
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"
    r"(?![A-Za-zÀ-ÖØ-öø-ÿ])"
)
HOUR    = r"(?:\d{1,2}\s*(?:h|:)\s*[0-5]?\d|\d{1,2}\s*h)"
H_RANGE = rf"(?:{HOUR}\s*(?:-|–|—|à|a)\s*{HOUR})"
H_ANY   = rf"(?:{H_RANGE}|{HOUR})"

RE_DAY_TOKEN = re.compile(DAY_TOKEN, re.IGNORECASE)
RE_ANY_HOUR  = re.compile(H_ANY, re.IGNORECASE)

# =========================
# Regex titres / sous-titres structurants
# =========================
RE_TOP_DANSE = re.compile(
    r"^\s*danse\s+(flamenco|s[ée]villane)\s*(adultes|enfants(?:\s+et\s*t['’]?cap)?|)\s*$",
    re.IGNORECASE
)

# Sous-titres pour Flamenco Adultes = niveaux
RE_ADULTS_LEVEL = re.compile(
    r"^\s*(débutants?|debutants?|inter\s*1|inter\s*2|interm[ée]diaire|avanc[ée]s?|technique)\s*$",
    re.IGNORECASE
)

# Sous-titres pour Flamenco Enfants et T'CAP = groupes (publics)
RE_CHILD_GROUP = re.compile(
    r"^\s*(petits(?:\s*\(.*?\))?|grands(?:\s*\(.*?\))?|t['’]?cap|ados?)\s*$",
    re.IGNORECASE
)

# Sous-titres pour Sévillane = niveaux (seulement Débutants / Avancés)
RE_SEVI_LEVEL = re.compile(
    r"^\s*(débutants?|debutants?|avanc[ée]s?)\s*$",
    re.IGNORECASE
)

# Lignes horaires "Jour : 18h30 – 20h00"
RE_LINE_SCHEDULE = re.compile(
    rf"^\s*(?:{DAY_TOKEN})\s*:\s*{H_ANY}(?:\s*(?:,|/|\|\s*)\s*{H_ANY})*\s*$",
    re.IGNORECASE
)

# =========================
# Regex Tarifs / Adhésions
# =========================
RE_PRICE_LINE    = re.compile(r"€")
RE_ADHESION      = re.compile(r"Adh[ée]sion\s+annuelle[^0-9]{0,20}(\d{1,3})\s*€", re.IGNORECASE)
RE_REDUIT_BLOCK  = re.compile(r"(r[ée]duit|r[ée]duction|minima|[ée]tudiant|ch[oô]meur|famille|RSA|CAF|bourse)", re.IGNORECASE)
RE_PAY           = re.compile(r"(paiement|r[èe]glement|trimestre|mensuel|esp[eè]ces|ch[eè]ques|CB|carte|liquide|virement)", re.IGNORECASE)
RE_PAIR_NR       = re.compile(r"^\s*([0-9][0-9 ]{1,3})\s*€\s*\|\s*([0-9][0-9 ]{1,3})\s*€\s*$")
RE_TARIFS_HEADER = re.compile(r"^TARIFS\s+AU\s+TRIMESTRE$", re.IGNORECASE)
RE_TARIFS_CATEGORIES = re.compile(r"(?i)\b(adh[ée]rents?|[ée]l[eè]ves?|non\s*adh[ée]rents?)\b[^0-9]{0,15}([0-9 ][0-9 ]*)\s*€")

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

def clean_hours_text(s: str) -> str:
    return (s or "").replace("–", "-").replace("—", "-").replace(" à ", "-")

def canon_level(s: str) -> str:
    s = (s or "").strip().lower()
    if not s:
        return ""
    if s.startswith("début") or s.startswith("debut"):
        return "Débutants"
    if s.startswith("inter "):
        # Inter 1 / Inter 2 → "Intermédiaire"
        return "Intermédiaire"
    if "interm" in s:
        return "Intermédiaire"
    if s.startswith("avanc"):
        return "Avancés"
    if "technique" in s:
        return "Technique"
    return s.capitalize()

def canon_public_from_child_group(grp: str) -> str:
    g = (grp or "").lower()
    if g.startswith("ado"):
        return "Ados"
    if "cap" in g:
        return "T’CAP"
    # Petits / Grands → Enfants
    return "Enfants"

def sanitize_label(s: str) -> str:
    s = (s or "").replace("·", " ").strip()
    return sanitize_for_voice(s).capitalize() if s else ""

def parse_structured_horaires(all_lines):
    """
    Parse déterministe basé sur la structure:
    - Sections: DANSE FLAMENCO ADULTES / ENFANTS et T’CAP / DANSE SÉVILLANE
    - Sous-titres: niveaux (Adultes, Sévillane) ou groupes (Enfants)
    - Lignes horaires: 'Jour : 18h30 – 20h00'
    """
    horaires = []
    seen = set()

    current_danse = ""
    current_section = ""   # "flamenco_adultes" | "flamenco_enfants" | "sevillane"
    current_public = ""    # fixé par section/enfants
    current_level  = ""    # fixé par sous-titre Adultes / Sévillane ; vide pour Enfants

    for raw in all_lines:
        line = normalize_text(raw)
        if not line:
            continue

        # 1) Sections top-level "DANSE ..."
        mt = RE_TOP_DANSE.match(line)
        if mt:
            d, scope = mt.group(1), (mt.group(2) or "")
            d_norm = "Flamenco" if re.search(r"flamenco", d, re.IGNORECASE) else "Sévillane"
            current_danse = d_norm
            current_level = ""
            current_public = ""
            scope_l = scope.lower().strip()

            if d_norm == "Flamenco" and "adult" in scope_l:
                current_section = "flamenco_adultes"
                current_public = "Adultes"
            elif d_norm == "Flamenco" and ("enfant" in scope_l or "cap" in scope_l):
                current_section = "flamenco_enfants"
                current_public = ""  # sera défini par sous-titre (Petits/Grands/Ados/T’CAP)
            elif d_norm == "Sévillane":
                current_section = "sevillane"
                current_public = ""  # jamais de public pour Sévillane
            else:
                current_section = ""

            continue

        # 2) Sous-titres (selon la section)
        if current_section == "flamenco_adultes":
            ms = RE_ADULTS_LEVEL.match(line)
            if ms:
                current_level = canon_level(ms.group(0))
                # public reste "Adultes"
                continue

        elif current_section == "flamenco_enfants":
            ms = RE_CHILD_GROUP.match(line)
            if ms:
                grp = ms.group(0)
                current_public = canon_public_from_child_group(grp)
                current_level = ""  # pas de niveau demandé pour Enfants/Ados/T’CAP
                continue

        elif current_section == "sevillane":
            ms = RE_SEVI_LEVEL.match(line)
            if ms:
                current_level = canon_level(ms.group(0))  # donnera Débutants / Avancés
                current_public = ""  # jamais de public
                continue

        # 3) Lignes horaires -> extraire jour + heures
        if RE_LINE_SCHEDULE.match(line):
            # Jour
            mj = RE_DAY_TOKEN.search(line)
            if not mj:
                continue
            jour = norm_day(mj.group(0))

            # Heures (toutes)
            hours_found = [m.group(0) for m in RE_ANY_HOUR.finditer(line)]
            if not hours_found:
                continue
            hours_text = clean_hours_text(" - ".join(hours_found))
            item = {
                "jour": jour,
                "heures": hours_text,
                "heures_vocal": remplacer_h_par_heure(hours_text),
                "niveau": sanitize_label(current_level),
                "danse": sanitize_label(current_danse),
                "public": sanitize_label(current_public),
            }

            keyi = (item["jour"], item["heures"], item["danse"], item["public"], item["niveau"])
            if keyi not in seen:
                seen.add(keyi)
                horaires.append(item)

    return horaires

@bp.get("/infos-cours")
def infos_cours():
    key = cache_key("infos-cours", request.args.to_dict(flat=True))
    entry = cache_get(key)

    try:
        # =========================
        # 0) Récupération & normalisation du texte
        # =========================
        html = fetch_html(SRC)
        soup = soup_from_html(html)

        text_blocks = []
        for sel in ['[data-hook="richTextElement"]', '[class*="richText"]']:
            for el in soup.select(sel):
                t = normalize_text(el.get_text("\n", strip=True))
                if t:
                    text_blocks.append(t)
        for el in soup.find_all(["h2", "h3", "h4", "p", "li"]):
            t = normalize_text(el.get_text("\n", strip=True))
            if t:
                text_blocks.append(t)
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [normalize_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"])]
                row = " | ".join([c for c in cells if c])
                if row:
                    rows.append(row)
            if rows:
                text_blocks.append("\n".join(rows))

        # Lignes à plat (dédupliquées, vides enlevées)
        seen_line, lines = set(), []
        for block in text_blocks:
            for l in re.split(r"\n+", block):
                l2 = normalize_text(l)
                if l2 and l2 not in seen_line:
                    seen_line.add(l2)
                    lines.append(l2)

        # =========================
        # 1) HORAIRES — Parse structuré (sections)
        # =========================
        horaires = parse_structured_horaires(lines)

        # =========================
        # 1.b) RÈGLES MÉTIER de sécurité
        # =========================
        def is_sevi(danse: str) -> bool:
            return "sevillan" in (danse or "").lower() or "sévillan" in (danse or "").lower()

        def is_flamenco(danse: str) -> bool:
            return "flamenco" in (danse or "").lower()

        def apply_business_rules(h):
            # Flamenco Enfants/Ados : pas de "Technique"
            if is_flamenco(h.get("danse")) and h.get("public") in {"Enfants", "Ados", "T’CAP"}:
                if h.get("niveau", "").lower() == "technique":
                    h["niveau"] = ""

            # Sévillane : pas de public ; niveau seulement Débutants / Avancés
            if is_sevi(h.get("danse")):
                h["public"] = ""
                if h.get("niveau") not in {"Débutants", "Avancés"}:
                    h["niveau"] = ""

            return h

        horaires = [apply_business_rules(dict(h)) for h in horaires]

        # (Option) filtre conservé contre un faux-poste connu
        def _is_flamenco_debutants_adultes_vendredi(item: dict) -> bool:
            jour   = (item.get("jour") or "").strip()
            danse  = (item.get("danse") or "").lower()
            public = (item.get("public") or "").lower()
            niveau = (item.get("niveau") or "").lower()
            return (
                jour == "Vendredi"
                and "flamenco" in danse
                and "adulte" in public
                and ("début" in niveau or "debut" in niveau)
            )
        horaires = [h for h in horaires if not _is_flamenco_debutants_adultes_vendredi(h)]

        # =========================
        # 2) TARIFS (identique à avant)
        # =========================
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

        # Niveaux Sévillane (synthèse informative)
        niveaux_sevillane = []
        for l in lines:
            if re.search(r"s[ée]villan", l, re.IGNORECASE) and RE_SEVI_LEVEL.match(l):
                lvl = canon_level(l)
                if lvl in {"Débutants", "Avancés"} and lvl not in niveaux_sevillane:
                    niveaux_sevillane.append(lvl)

        # =========================
        # Version "vocale" des horaires
        # =========================
        horaires_vocal = []
        for h in horaires:
            extra_parts = [x for x in [h["danse"], h["public"], h["niveau"]] if x]
            extra = " ".join(extra_parts)
            lead = f"Voici les horaires pour {extra} : " if extra else "Voici les horaires : "
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
