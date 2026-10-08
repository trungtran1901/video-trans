import re
import threading
import uuid
from pathlib import Path
from app.core import jobstore, media as media_lib, cuts as cutlib

MIN_CLIP = 0.1
SPEED_MIN = 0.25
SPEED_MAX = 4.0
MAX_IMAGE_SECONDS = 600.0
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ensure_lock = threading.Lock()


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def clip_length(clip: dict) -> float:
    speed = float(clip.get("speed", 1.0)) or 1.0
    return max(0.0, (float(clip["out"]) - float(clip["in"])) / speed)


def layout(clips: list[dict]) -> list[tuple[dict, float, float]]:
    out = []
    t = 0.0
    for c in clips:
        length = clip_length(c)
        out.append((c, t, t + length))
        t += length
    return out


def comp_duration(clips: list[dict]) -> float:
    return sum(clip_length(c) for c in clips)


def is_identity(clips: list[dict], total_duration: float) -> bool:
    if len(clips) != 1:
        return False
    c = clips[0]
    return (
        c["media"] == "main"
        and abs(float(c.get("speed", 1.0)) - 1.0) < 1e-6
        and abs(float(c.get("volume", 1.0)) - 1.0) < 1e-6
        and float(c["in"]) <= 0.01
        and float(c["out"]) >= float(total_duration) - 0.05
    )


def sanitize_clips(items: list, media_map: dict) -> list[dict]:
    out = []
    seen = set()
    for it in items[:500]:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("media", ""))
        m = media_map.get(mid)
        if not m:
            continue
        try:
            start = float(it.get("in", 0.0))
            end = float(it.get("out", 0.0))
            speed = float(it.get("speed", 1.0))
            volume = float(it.get("volume", 1.0))
        except (TypeError, ValueError):
            continue
        duration = float(m.get("duration") or 0.0)
        if m.get("type") == "image":
            speed = 1.0
            volume = 1.0
            duration = duration or MAX_IMAGE_SECONDS
        speed = min(max(speed, SPEED_MIN), SPEED_MAX)
        volume = min(max(volume, 0.0), 2.0)
        start = min(max(start, 0.0), duration)
        end = min(max(end, start), duration)
        if end - start < MIN_CLIP * speed:
            continue
        cid = str(it.get("id", ""))
        if not _ID_RE.match(cid) or cid in seen:
            cid = new_id()
        seen.add(cid)
        out.append({
            "id": cid,
            "media": mid,
            "in": round(start, 3),
            "out": round(end, 3),
            "speed": round(speed, 3),
            "volume": round(volume, 3),
        })
    return out


def ensure_timeline(job_id: str):
    with _ensure_lock:
        job = jobstore.get_job(job_id)
        if not job:
            return None
        if job.clips and job.media and "main" in job.media:
            return job
        path = Path(job.video_path) if job.video_path else None
        if not path or not path.exists():
            return job
        total = float(job.total_duration or 0.0)
        if total <= 0:
            try:
                total = media_lib.get_duration(path)
            except Exception:
                return job
            jobstore.update_job(job_id, total_duration=total)
        if total <= 0:
            return job
        try:
            width, height = media_lib.get_video_size(path)
        except Exception:
            width, height = 1280, 720
        try:
            audio = media_lib.has_audio(path)
        except Exception:
            audio = True
        main = {
            "id": "main",
            "type": "video",
            "name": job.input_filename or "video",
            "path": job.video_path,
            "preview": "",
            "duration": total,
            "width": width,
            "height": height,
            "has_audio": audio,
            "translate": True,
            "state": "ready",
            "tstate": "done",
            "tmessage": "",
            "error": "",
            "segments": {},
            "merged": True,
        }
        media_map = dict(job.media or {})
        media_map["main"] = main
        cut_list = cutlib.normalize_cuts(job.cuts, total)
        keep = cutlib.keep_ranges(cut_list, total) if cut_list else [(0.0, total)]
        if not keep:
            keep = [(0.0, total)]
            cut_list = []
        clips = [
            {"id": new_id(), "media": "main", "in": round(s, 3), "out": round(e, 3), "speed": 1.0, "volume": 1.0}
            for s, e in keep
        ]
        if cut_list:
            for lang, segs in (job.segments or {}).items():
                if lang.startswith("_"):
                    continue
                jobstore.set_segments_for_lang(job_id, lang, cutlib.remap_segments(segs, cut_list))
        jobstore.update_job(job_id, clips=clips, media=media_map, cuts=[])
        return jobstore.get_job(job_id)