from fastapi import FastAPI
from solea_api.routes import infos_agenda, infos_cours, infos_stage, infos_tablao

app = FastAPI()

app.include_router(infos_agenda.router, prefix="/infos-agenda", tags=["Agenda"])
app.include_router(infos_cours.router, prefix="/infos-cours", tags=["Cours"])
app.include_router(infos_stage.router, prefix="/infos-stage", tags=["Stage"])
app.include_router(infos_tablao.router, prefix="/infos-tablao", tags=["Tablao"])

@app.get("/")
def root():
    return {"message": "API Centre Soléa — OK"}
