# solea_api/routes/infos_stage.py
from flask import Blueprint, Response
import requests
from bs4 import BeautifulSoup
from ..utils import normalize_text  # on réutilise ta normalisation

bp = Blueprint("infos_stage", __name__)
SRC = "https://www.centresolea.org/stages"

@bp.get("/infos-stage-solea")
def infos_stage_solea():
    # 1) Récupère le HTML
    r = requests.get(SRC, timeout=10)
    r.raise_for_status()

    # 2) Parse et nettoie
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    # 3) Extrait le texte utile
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        t = el.get_text(" ", strip=True)
        if t:
            lines.append(t)

    # 4) Normalise et renvoie en texte brut
    text = normalize_text("\n".join(lines))
    return Response(text, mimetype="text/plain; charset=utf-8")
