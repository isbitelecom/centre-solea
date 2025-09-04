# solea_api/routes/infos_stage_solea.py
from flask import Blueprint, Response
import requests

bp = Blueprint("infos_stage_solea", __name__)
SRC = "https://www.centresolea.org/stages"

@bp.get("/infos-stage-solea")
def infos_stage_solea():
    r = requests.get(SRC)
    r.raise_for_status()
    return Response(r.text, mimetype="text/html")
