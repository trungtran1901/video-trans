import json
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from app.config import JOBS_DB

_lock = threading.Lock()


@dataclass
class Job:
    id: str
    status: str = "pending"
    progress: int = 0
    message: str = ""
    input_filename: str = ""
    video_path: str = ""
    target_langs: list = field(default_factory=list)
    mode: str = "subtitle_burn"
    context: str = ""
    total_duration: float = 0.0
    segments: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    regions: list = field(default_factory=list)  # areas to blur / cover / delogo before export
    cuts: list = field(default_factory=list)     # removed parts of the source timeline [{start, end}]
    subtitle_style: dict = field(default_factory=lambda: {"font_size": 28, "x": 0.5, "y": 0.9})
    watermark: dict = field(default_factory=lambda: {
        "enabled": False,
        "type": "text",
        "text": "",
        "image": "",
        "opacity": 0.5,
        "x": 0.82,
        "y": 0.86,
        "font_size": 24,
        "scale": 0.16,
    })
    dub: dict = field(default_factory=lambda: {"enabled": False, "orig_volume": 0.25, "dub_volume": 1.0, "duck": True, "duck_level": 0.3})  # export with the dubbed voice instead of the original audio
    source_lang: str = ""
    clips: list = field(default_factory=list)
    media: dict = field(default_factory=dict)
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def _load_all() -> dict:
    if JOBS_DB.exists():
        return json.loads(JOBS_DB.read_text(encoding="utf-8"))
    return {}


def _save_all(data: dict) -> None:
    JOBS_DB.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def create_job(input_filename: str, target_langs: list[str], mode: str, job_id: str | None = None,
               video_path: str = "", context: str = "", source_lang: str = "") -> Job:
    job = Job(
        id=job_id or str(uuid.uuid4()),
        input_filename=input_filename,
        target_langs=target_langs,
        mode=mode,
        video_path=video_path,
        context=context,
        source_lang=source_lang,
    )
    with _lock:
        data = _load_all()
        data[job.id] = asdict(job)
        _save_all(data)
    return job


def get_job(job_id: str) -> Job | None:
    with _lock:
        data = _load_all()
    raw = data.get(job_id)
    return Job(**raw) if raw else None


def update_job(job_id: str, **kwargs) -> None:
    with _lock:
        data = _load_all()
        if job_id not in data:
            return
        data[job_id].update(kwargs)
        data[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _save_all(data)


def set_segments_for_lang(job_id: str, lang: str, segments: list[dict]) -> None:
    with _lock:
        data = _load_all()
        if job_id not in data:
            return
        all_segments = data[job_id].get("segments") or {}
        all_segments[lang] = segments
        data[job_id]["segments"] = all_segments
        data[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _save_all(data)


def list_jobs() -> list[Job]:
    with _lock:
        data = _load_all()
    return [Job(**v) for v in sorted(data.values(), key=lambda x: x["created_at"], reverse=True)]


def delete_job(job_id: str) -> bool:
    with _lock:
        data = _load_all()
        if job_id not in data:
            return False
        del data[job_id]
        _save_all(data)
        return True

def add_media(job_id: str, entry: dict) -> None:
    with _lock:
        data = _load_all()
        if job_id not in data:
            return
        media = data[job_id].get("media") or {}
        media[entry["id"]] = entry
        data[job_id]["media"] = media
        data[job_id]["updated_at"] = datetime.utcnow().isoformat()
        _save_all(data)


def update_media(job_id: str, mid: str, **kwargs) -> None:
    with _lock:
        data = _load_all()
        job = data.get(job_id)
        if not job:
            return
        entry = (job.get("media") or {}).get(mid)
        if entry is None:
            return
        entry.update(kwargs)
        job["updated_at"] = datetime.utcnow().isoformat()
        _save_all(data)