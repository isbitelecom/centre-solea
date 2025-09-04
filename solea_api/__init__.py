# solea_api/__init__.py
from flask import Flask, Response
import requests
from bs4 import BeautifulSoup

# si tu as utils.normalize_text, on l'importe, sinon on fait un fallback local
try:
    from .utils import normalize_text
except Exception:
    def normalize_text(s: str) -> str:
        return " ".join((s or "").replace("\r", "\n").replace("\t", " ").split())

SRC = "https://www.centresolea.org/stages"

def create_app():
    app = Flask(__name__)

    # 1) Blueprints “classiques” (garde ceux que tu utilises vraiment)
    try:
        from .routes.infos_cours import bp as infos_cours_bp
        app.register_blueprint(infos_cours_bp)
    except Exception:
        pass

    try:
        from .routes.infos_agenda import bp as infos_agenda_bp
        app.register_blueprint(infos_agenda_bp)
    except Exception:
        pass

    try:
        # tu peux garder ton blueprint infos_stage si tu veux,
        # mais la route /infos-stage ci-dessous suffira de toute façon
        from .routes.infos_stage import bp as infos_stage_bp
        app.register_blueprint(infos_stage_bp)
    except Exception:
        pass

    try:
        from .routes.infos_tablao import bp as infos_tablao_bp
        app.register_blueprint(infos_tablao_bp)
    except Exception:
        pass

    # 2) éviter les 404 liés au slash final
    app.url_map.strict_slashes = False

    @app.get("/")
    def home():
        return "API Centre Soléa — OK"

    # 3) ✅ Route TEXTE pour le voicebot (direct, sans blueprint)
    @app.get("/infos-stage")
    def infos_stage_plain():
        r = requests.get(SRC, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        lines = []
        for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            t = el.get_text(" ", strip=True)
            if t:
                lines.append(t)
        text = normalize_text("\n".join(lines))
        return Response(text, mimetype="text/plain; charset=utf-8")

    # 4) ❌ On écrase l’ancienne URL pour la rendre indisponible
    @app.get("/infos-stage-solea")
    def infos_stage_solea_removed():
        return "Cette route n'existe plus. Utilise /infos-stage.", 410

    # 5) 🔎 Debug : liste toutes les routes actives (ouvre dans le navigateur)
    @app.get("/debug-routes")
    def debug_routes():
        body = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
            body.append(f"{','.join(rule.methods)}  {rule.rule}  -> {rule.endpoint}")
        return Response("\n".join(body), mimetype="text/plain; charset=utf-8")

    return app

# pour gunicorn (wsgi:app)
app = create_app()
