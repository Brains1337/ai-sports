from api.routes import router
from fastapi import FastAPI

app = FastAPI(title="Fantasy Dashboard API", version="0.1.0")
app.include_router(router)


@app.get("/health")
def health():
    return {"ok": True}
