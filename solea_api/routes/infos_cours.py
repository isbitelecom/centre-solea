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

# =========================
# Regex sémantiques (élargies)
# =========================
LVL       = r"(débutant(?:·|\.|e|es|s)?|debutant(?:·|\.|e|es|s)?|initiation|tous\s*niveaux|multi\s*niveaux|interm[ée]diaire|inter(?:\s*1|\s*2)?|avanc[ée]s?|perfectionnement|technique)"
LVL_SEVI  = r"(débutant(?:·|\.|e|es|s)?|debutant(?:·|\.|e|es|s)?|initiation|tous\s*niveaux|multi\s*niveaux|interm[ée]diaire|inter(?:\s*1|\s*2)?|avanc[ée]s?|perfectionnement)"  # pas "technique" pour Sévillanes
DANCE     = r"(flamenco|s[ée]villan(?:e|es)?)"
PUBLIC    = r"(tout\s*public|famille|parents|enfants?|ados?|adultes?)"

RE_DAY_TOKEN = re.compile(DAY_TOKEN, re.IGNORECASE)
RE_ANY_HOUR  = re.compile(H_ANY, re.IGNORECASE)
RE_LVL       = re.compile(LVL, re.IGNORECASE)
RE_LVL_SEVI  = re.compile(LVL_SEVI, re.IGNORECASE)
RE_DANCE     = re.compile(DANCE, re.IGNORECASE)
RE_PUBLIC    = re.compile(PUBLIC, re.IGNORECASE)

# =========================
# Regex Tarifs / Adhésions (inchangé + légères améliorations)
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

        # =========================
        # RÉCOLTE TEXTE Wix (blocs + tableaux) -> text_blocks
        # =========================
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

        # Lignes à plat (dédupliquées)
        seen_line, lines = set(), []
        for block in text_blocks:
            for l in re.split(r"\n+", block):
                l2 = normalize_text(l)
                if l2 and l2 not in seen_line:
                    seen_line.add(l2)
                    lines.append(l2)

        # =========================
        # 1) HORAIRES — Passe 1 (proximité + voisins + co-occurrence Sévillane + contexte par jour)
        # =========================
        horaires, seen_items = [], set()
        PROX_WINDOW = 200  # fenêtrage large pour capter titres/paragraphes voisins

        def _first_match(regex, text):
            return regex.search(text) if text else None

        def _search_in_spaces(rx, spaces):
            for s in spaces:
                if not s:
                    continue
                m = rx.search(s)
                if m:
                    return m
            return None

        def _norm_cap(s):
            s = (s or "").replace("·", "").strip()
            return s.capitalize() if s else ""

        # Contexte cumulatif par jour : on y met des infos "sûres"
        contexte_par_jour = {}

        for bidx, block in enumerate(text_blocks):
            for seg in segment_by_days(block, pre_window=160):
                seg_txt = seg["text"]
                hour_matches = list(RE_ANY_HOUR.finditer(seg_txt))
                if not hour_matches:
                    continue

                # Proximité autour de la première et de la dernière heure
                h_first, h_last = hour_matches[0], hour_matches[-1]
                def _win(hm):
                    s = max(0, hm.start() - PROX_WINDOW)
                    e = min(len(seg_txt), hm.end() + PROX_WINDOW)
                    return seg_txt[s:e]
                vicinity_first = _win(h_first)
                vicinity_last  = _win(h_last)

                # Voisinage inter-blocs (queue du précédent / tête du suivant)
                prev_tail = text_blocks[bidx - 1][-PROX_WINDOW:] if bidx - 1 >= 0 else ""
                next_head = text_blocks[bidx + 1][:PROX_WINDOW]  if bidx + 1 < len(text_blocks) else ""

                # Préfixe interne renvoyé par segment_by_days
                pre_tail = seg.get("pre", "")[-PROX_WINDOW:] if seg.get("pre") else ""

                search_spaces = [vicinity_first, vicinity_last, prev_tail, next_head, seg_txt, pre_tail]
                spaces_with_sevi = [s for s in search_spaces if re.search(r"s[ée]villan", s or "", re.IGNORECASE)]

                # Détection de la danse
                dance_m = _search_in_spaces(RE_DANCE, search_spaces)
                is_sevi = bool(dance_m and re.search(r"s[ée]villan", dance_m.group(0), re.IGNORECASE))

                # NIVEAU / PUBLIC
                if is_sevi and spaces_with_sevi:
                    # Pour Sévillane : co-occurrence stricte (et pas de "Technique" via LVL_SEVI)
                    lvl_m    = _search_in_spaces(RE_LVL_SEVI, spaces_with_sevi)
                    public_m = _search_in_spaces(RE_PUBLIC,    spaces_with_sevi)
                else:
                    # Flamenco (ou inconnu) : recherche standard
                    lvl_m    = _search_in_spaces(RE_LVL,    search_spaces)
                    public_m = _search_in_spaces(RE_PUBLIC, search_spaces)

                # Fallback : préfixe si rien trouvé
                if not (lvl_m or public_m or dance_m) and pre_tail:
                    lvl_m    = lvl_m    or _first_match(RE_LVL, pre_tail)
                    public_m = public_m or _first_match(RE_PUBLIC, pre_tail)
                    dance_m  = dance_m  or _first_match(RE_DANCE, pre_tail)

                hours_text = " ".join(m.group(0) for m in hour_matches)
                jour_norm = seg["jour"]

                # Normalisation / garde-fous
                niv_txt = (lvl_m.group(0) if lvl_m else "")
                dan_txt = (dance_m.group(0) if dance_m else "")
                pub_txt = (public_m.group(0) if public_m else "")

                # Interdit "Technique" pour Sévillane sauf co-occurrence stricte (déjà gérée via LVL_SEVI)
                if re.search(r"s[ée]villan", dan_txt or "", re.IGNORECASE) and re.search(r"\btechnique\b", niv_txt or "", re.IGNORECASE):
                    niv_txt = ""  # annule sur-signal

                item = {
                    "jour": jour_norm,
                    "heures": hours_text.replace("–", "-").replace("—", "-"),
                    "heures_vocal": remplacer_h_par_heure(hours_text),
                    "niveau": _norm_cap(niv_txt),
                    "danse":  _norm_cap(sanitize_for_voice(dan_txt)),
                    "public": _norm_cap(pub_txt),
                }

                # Mise à jour de contexte par jour (reset si la danse change)
                ctx = contexte_par_jour.get(jour_norm, {"niveau": "", "danse": "", "public": ""})
                if item["danse"]:
                    if ctx.get("danse") and ctx["danse"].lower() != item["danse"].lower():
                        ctx = {"niveau": "", "danse": item["danse"], "public": ""}  # reset niveau/public
                    else:
                        ctx["danse"] = item["danse"]

                # On nourrit le contexte seulement avec des infos sûres
                if item["niveau"]:
                    if not (re.search(r"s[ée]villan", item["danse"], re.IGNORECASE) and item["niveau"].lower() == "technique"):
                        ctx["niveau"] = item["niveau"]
                if item["public"]:
                    ctx["public"] = item["public"]

                contexte_par_jour[jour_norm] = ctx

                keyi = (item["jour"], item["heures"], item["niveau"], item["danse"], item["public"])
                if keyi not in seen_items:
                    seen_items.add(keyi)
                    horaires.append(item)

        # =========================
        # 1.b) HORAIRES — Passe 2 (scan par lignes + voisinage ±3 lignes + reset de contexte à changement de danse)
        # =========================
        def _key_pair(jour, heures):
            return (jour, heures.replace("–", "-").replace("—", "-").strip())

        by_key = { _key_pair(h["jour"], h["heures"]): h for h in horaires }

        ctx_danse = ""
        ctx_public = ""
        ctx_niveau = ""
        NEIGHB = 3
        N = len(lines)

        for i, line in enumerate(lines):
            if not line:
                continue

            # Mise à jour du contexte si la ligne contient des libellés
            m_d = RE_DANCE.search(line)
            m_p = RE_PUBLIC.search(line)
            m_l = RE_LVL.search(line)

            if m_d:
                new_danse = m_d.group(0)
                # Reset contexte si la danse change
                if (ctx_danse or "").lower() != new_danse.lower():
                    ctx_niveau = ""
                    ctx_public = ""
                ctx_danse = new_danse

            if m_p:
                ctx_public = m_p.group(0)
            if m_l:
                ctx_niveau = m_l.group(0)

            # Détection d'une ligne horaire avec jour
            if RE_DAY_TOKEN.search(line) and RE_ANY_HOUR.search(line):
                jour = norm_day(RE_DAY_TOKEN.search(line).group(0))
                hours_text = " ".join(m.group(0) for m in RE_ANY_HOUR.finditer(line))

                # voisinage ±3 lignes
                start = max(0, i - NEIGHB)
                end   = min(N, i + NEIGHB + 1)
                neigh_txt = "\n".join(lines[start:end])

                # Recherche locale
                danse  = (RE_DANCE.search(neigh_txt) or (RE_DANCE.search(ctx_danse) if isinstance(ctx_danse, str) else None))
                is_sevi_line = bool(danse and re.search(r"s[ée]villan", danse.group(0), re.IGNORECASE))

                if is_sevi_line:
                    lvl    = (RE_LVL_SEVI.search(neigh_txt) or (RE_LVL_SEVI.search(ctx_niveau) if isinstance(ctx_niveau, str) else None))
                else:
                    lvl    = (RE_LVL.search(neigh_txt) or (RE_LVL.search(ctx_niveau) if isinstance(ctx_niveau, str) else None))

                public = (RE_PUBLIC.search(neigh_txt) or (RE_PUBLIC.search(ctx_public) if isinstance(ctx_public, str) else None))

                k = _key_pair(jour, hours_text)
                item = by_key.get(k, {
                    "jour": jour,
                    "heures": hours_text.replace("–", "-").replace("—", "-"),
                    "heures_vocal": remplacer_h_par_heure(hours_text),
                    "niveau": "",
                    "danse": "",
                    "public": "",
                })

                # Compléter à partir de la ligne + contexte
                niv_txt = (lvl.group(0) if lvl else ctx_niveau)
                dan_txt = (sanitize_for_voice(danse.group(0) if danse else ctx_danse))
                pub_txt = (public.group(0) if public else ctx_public)

                # Sévillanes : ne jamais poser "Technique" depuis le contexte
                if re.search(r"s[ée]villan", dan_txt or "", re.IGNORECASE) and (niv_txt or "").lower() == "technique":
                    niv_txt = ""

                item["niveau"] = item["niveau"] or _norm_cap(niv_txt)
                item["danse"]  = item["danse"]  or _norm_cap(dan_txt)
                item["public"] = item["public"] or _norm_cap(pub_txt)

                by_key[k] = item

        # Réécrit la liste horaires consolidée
        horaires = list(by_key.values())

        # =========================
        # Filtres anti faux-positifs
        # =========================
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

        def _is_sevi_technique_vendredi(item: dict) -> bool:
            return (
                item.get("jour") == "Vendredi"
                and re.search(r"s[ée]villan", item.get("danse", ""), re.IGNORECASE)
                and item.get("niveau", "").lower() == "technique"
            )

        horaires = [h for h in horaires if not _is_flamenco_debutants_adultes_vendredi(h)]
        horaires = [h for h in horaires if not _is_sevi_technique_vendredi(h)]

        # =========================
        # 2) TARIFS (comme avant)
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

        # Niveaux Sévillanes (info de synthèse éventuelle)
        niveaux_sevillane = []
        for l in lines:
            if re.search(r"s[ée]villan", l, re.IGNORECASE) and RE_LVL.search(l):
                raw = RE_LVL.search(l).group(0).lower()
                if "debut" in raw or "début" in raw:
                    norm = "Débutants"
                elif "avanc" in raw:
                    norm = "Avancés"
                elif "inter" in raw:
                    norm = "Intermédiaire"
                elif "tous" in raw or "multi" in raw:
                    norm = "Tous niveaux"
                elif "initiation" in raw:
                    norm = "Initiation"
                else:
                    norm = raw.capitalize()
                if norm not in niveaux_sevillane:
                    niveaux_sevillane.append(norm)

        # =========================
        # Version "vocale" des horaires
        # =========================
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
