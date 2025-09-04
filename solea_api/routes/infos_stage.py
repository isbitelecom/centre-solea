# solea_api/routes/infos_stage.py
from flask import Blueprint, Response
import requests

bp = Blueprint("infos_stage", __name__)  # <-- nom unique du blueprint
SRC = "https://www.centresolea.org/stages"

@bp.get("/infos-stage")  # <-- nouvelle route
def info_stage():
    r = requests.get(SRC)
    r.raise_for_status()
    return Response(r.text, mimetype="text/html")
