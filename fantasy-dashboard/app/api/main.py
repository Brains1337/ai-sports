from api.routes import router
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Fantasy Dashboard API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://draft.ai.goffs.xyz",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
def health():
    return {"ok": True}