from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.api.routes import router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Video Translator & Dubbing")

app.include_router(router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "web" / "templates" / "index.html"))
