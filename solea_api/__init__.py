from flask import Flask

def create_app():
    app = Flask(__name__)

    # Blueprints
    from .routes.infos_cours import bp as infos_cours_bp
    from .routes.infos_agenda import bp as infos_agenda_bp
    from .routes.infos_stage import bp as infos_stage_bp   # <-- cohérent avec infos_stage.py
    from .routes.infos_tablao import bp as infos_tablao_bp

    app.register_blueprint(infos_cours_bp)
    app.register_blueprint(infos_agenda_bp)
    app.register_blueprint(infos_stage_bp)                # <-- enregistré une seule fois
    app.register_blueprint(infos_tablao_bp)

    @app.get("/")
    def home():
        return "API Centre Soléa — OK"

    return app

# pour gunicorn (wsgi:app)
app = create_app()
