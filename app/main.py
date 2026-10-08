from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.api.routes import router
from app.core import jobstore

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Video Translator & Dubbing")


@app.on_event("startup")
def recover_interrupted_jobs():
    for job in jobstore.list_jobs():
        if job.status in ("running", "rendering"):
            if any(not k.startswith("_") for k in (job.segments or {})):
                jobstore.update_job(job.id, status="review", progress=90,
                                    message="Job bị gián đoạn do máy chủ khởi động lại. Kết quả đã lưu được giữ lại, bấm “Dịch lại” để dịch tiếp.")
            else:
                jobstore.update_job(job.id, status="failed", error="Máy chủ khởi động lại khi đang xử lý.",
                                    message="Interrupted")


app.include_router(router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "web" / "templates" / "index.html"))