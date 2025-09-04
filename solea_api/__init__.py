# solea_api/__init__.py
from flask import Flask

def create_app():
    app = Flask(__name__)

    # Blueprints
    from .routes.infos_cours import bp as infos_cours_bp
    from .routes.infos_agenda import bp as infos_agenda_bp
    from .routes.infos_stage_solea import bp as infos_stage_bp
    from .routes.infos_tablao import bp as infos_tablao_bp

    app.register_blueprint(infos_cours_bp)
    app.register_blueprint(infos_agenda_bp)
    app.register_blueprint(infos_stage_bp)
    app.register_blueprint(infos_tablao_bp)

    @app.get("/")
    def home():
        return "API Centre Soléa — OK"

    return app

# pour gunicorn (wsgi:app)
app = create_app()
