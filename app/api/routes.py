import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from app.config import UPLOAD_DIR, OUTPUT_DIR
from app.core.llm_config import load_config, save_config, LLMConfig
from app.core.llm_client import LLMClient
from app.core import jobstore, dubbing, composition, media as media_lib, translator, asr
from app.core.pipeline import transcribe_and_translate, render_job, prepare_media, translate_media

router = APIRouter()

MIN_SEGMENT_SECONDS = 0.05
EDIT_STATES = ("review", "completed", "failed")


def _ensure(job_id: str):
    job = jobstore.get_job(job_id)
    if job and job.status not in ("pending", "running"):
        return composition.ensure_timeline(job_id) or job
    return job


def _editable(job_id: str):
    job = _ensure(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in EDIT_STATES:
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")
    return job


def _comp_total(job) -> float:
    if job.clips:
        return composition.comp_duration(job.clips)
    return float(job.total_duration or 0.0)


def _media_playable(job_id: str, m: dict) -> Path:
    if m.get("type") == "image":
        return Path(m["path"])
    if m["id"] == "main":
        candidate = OUTPUT_DIR / job_id / "preview.mp4"
    else:
        candidate = Path(m["preview"]) if m.get("preview") else None
    if candidate and candidate.exists():
        return candidate
    return Path(m["path"])


def _media_out(job_id: str, m: dict) -> dict:
    d = {k: v for k, v in m.items() if k not in ("segments", "path", "preview")}
    segs = m.get("segments") or {}
    d["source"] = segs.get("_source", [])
    d["preview_ready"] = _media_playable(job_id, m).exists()
    if m.get("tstate") == "done" and not m.get("merged"):
        d["segments"] = {k: v for k, v in segs.items() if not k.startswith("_")}
    return d


def _public_job(job) -> dict:
    d = dict(job.__dict__)
    d["media"] = {mid: _media_out(job.id, m) for mid, m in (job.media or {}).items()}
    return d


@router.get("/api/llm/config")
def get_llm_config():
    return load_config().masked()


@router.post("/api/llm/config")
def set_llm_config(payload: dict):
    current = load_config()
    for k, v in payload.items():
        if hasattr(current, k):
            setattr(current, k, v)
    save_config(current)
    return current.masked()


@router.post("/api/llm/test")
def test_llm_connection(payload: dict | None = None):
    cfg = load_config()
    if payload:
        for k, v in payload.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    client = LLMClient(cfg)
    ok, msg = client.test_connection()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}


@router.get("/api/llm/models")
def get_llm_models():
    cfg = load_config()
    if not cfg.base_url:
        raise HTTPException(status_code=400, detail="base_url is not configured")
    client = LLMClient(cfg)
    try:
        models = client.list_models()
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{cfg.base_url}/models -> {e}")


@router.post("/api/jobs")
async def create_job_endpoint(
    file: UploadFile = File(...),
    target_langs: str = Form(...),
    mode: str = Form("subtitle_burn"),
    context: str = Form(""),
    source_lang: str = Form(""),
):
    langs = [x.strip() for x in target_langs.split(",") if x.strip()]
    if not langs:
        raise HTTPException(status_code=400, detail="target_langs is required")

    if mode not in ("subtitle_burn", "subtitle_soft"):
        mode = "subtitle_burn"  # dubbing is now an option inside the editor, not a mode

    job_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix or ".mp4"
    saved_path = UPLOAD_DIR / f"{job_id}{ext}"
    with open(saved_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    jobstore.create_job(
        input_filename=file.filename,
        target_langs=langs,
        mode=mode,
        job_id=job_id,
        video_path=str(saved_path),
        context=context,
        source_lang=source_lang,
    )

    thread = threading.Thread(
        target=transcribe_and_translate,
        args=(job_id, saved_path, langs, context, source_lang or None),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@router.get("/api/jobs")
def get_jobs():
    result = []
    for j in jobstore.list_jobs():
        d = _public_job(j)
        d.pop("segments", None)
        result.append(d)
    return result


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _ensure(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _public_job(job)


@router.delete("/api/jobs/{job_id}")
def delete_job_endpoint(job_id: str):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("running", "rendering"):
        raise HTTPException(status_code=400, detail="job is currently processing, cannot delete now")

    if job.video_path:
        video_file = Path(job.video_path)
        if video_file.exists():
            video_file.unlink(missing_ok=True)

    job_out_dir = OUTPUT_DIR / job_id
    if job_out_dir.exists():
        shutil.rmtree(job_out_dir, ignore_errors=True)

    jobstore.delete_job(job_id)
    return {"ok": True}


@router.get("/api/jobs/{job_id}/segments")
def get_segments(job_id: str):
    job = _ensure(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.segments


def _normalize_segments(items: list, max_duration: float) -> list[dict]:
    """Validate, sort by start time, drop empty/invalid items and re-number ids."""
    cleaned = []
    for it in items:
        try:
            start = max(0.0, float(it["start"]))
            end = float(it["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if max_duration and max_duration > 0:
            end = min(end, max_duration)
        text = str(it.get("text", "")).strip()
        if not text or end - start < MIN_SEGMENT_SECONDS:
            continue
        cleaned.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    cleaned.sort(key=lambda s: s["start"])
    for i, seg in enumerate(cleaned):
        seg["id"] = i
    return cleaned


@router.put("/api/jobs/{job_id}/segments/{lang}")
def update_segments(job_id: str, lang: str, payload: dict):
    """Replace the whole segment list of one language (supports split / merge / add / delete / retime)."""
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")
    if lang.startswith("_") or lang not in job.segments:
        raise HTTPException(status_code=400, detail="unknown language")

    items = payload.get("segments")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="segments must be a list")

    cleaned = _normalize_segments(items, _comp_total(job))
    jobstore.set_segments_for_lang(job_id, lang, cleaned)
    return {"ok": True, "segments": cleaned}


@router.put("/api/jobs/{job_id}/subtitle-style")
def update_subtitle_style(job_id: str, payload: dict):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")

    try:
        font_size = int(payload.get("font_size", 28))
        x = float(payload.get("x", 0.5))
        y = float(payload.get("y", 0.9))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid subtitle style values")

    font_size = min(max(font_size, 10), 120)
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)

    style = {"font_size": font_size, "x": round(x, 4), "y": round(y, 4)}
    jobstore.update_job(job_id, subtitle_style=style)
    return {"ok": True, "subtitle_style": style}


WATERMARK_TYPES = {"text", "image"}


@router.put("/api/jobs/{job_id}/watermark")
def update_watermark(job_id: str, payload: dict):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")

    current = dict(job.watermark or {})
    try:
        enabled = bool(payload.get("enabled", current.get("enabled", False)))
        wtype = payload.get("type", current.get("type", "text"))
        if wtype not in WATERMARK_TYPES:
            wtype = "text"
        text = str(payload.get("text", current.get("text", "")))[:200]
        opacity = min(max(float(payload.get("opacity", current.get("opacity", 0.5))), 0.02), 1.0)
        x = min(max(float(payload.get("x", current.get("x", 0.82))), 0.0), 1.0)
        y = min(max(float(payload.get("y", current.get("y", 0.86))), 0.0), 1.0)
        font_size = min(max(int(payload.get("font_size", current.get("font_size", 24))), 8), 200)
        scale = min(max(float(payload.get("scale", current.get("scale", 0.16))), 0.02), 1.0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid watermark values")

    watermark = {
        "enabled": enabled,
        "type": wtype,
        "text": text,
        "image": current.get("image", ""),
        "opacity": round(opacity, 3),
        "x": round(x, 4),
        "y": round(y, 4),
        "font_size": font_size,
        "scale": round(scale, 4),
    }
    jobstore.update_job(job_id, watermark=watermark)
    return {"ok": True, "watermark": watermark}


@router.post("/api/jobs/{job_id}/watermark-image")
async def upload_watermark_image(job_id: str, file: UploadFile = File(...)):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")

    ext = Path(file.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(status_code=400, detail="only PNG/JPG/WEBP images are supported")

    job_out_dir = OUTPUT_DIR / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)
    dest = job_out_dir / f"watermark_src{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    watermark = dict(job.watermark or {})
    watermark["image"] = str(dest)
    watermark["type"] = "image"
    jobstore.update_job(job_id, watermark=watermark)
    return {"ok": True, "watermark": watermark}


@router.get("/api/jobs/{job_id}/watermark-image")
def get_watermark_image(job_id: str):
    job = jobstore.get_job(job_id)
    if not job or not job.watermark or not job.watermark.get("image"):
        raise HTTPException(status_code=404, detail="no watermark image")
    path = Path(job.watermark["image"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="watermark image missing on disk")
    return FileResponse(str(path))


REGION_MODES = {"blur", "box", "delogo"}


@router.put("/api/jobs/{job_id}/regions")
def update_regions(job_id: str, payload: dict):
    """Save areas (fractions of the frame) to blur / cover / delogo when exporting."""
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")
    items = payload.get("regions", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="regions must be a list")

    cleaned = []
    for it in items[:20]:
        try:
            x, y, w, h = (float(it[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        w = min(max(w, 0.0), 1.0 - x)
        h = min(max(h, 0.0), 1.0 - y)
        if w < 0.005 or h < 0.005:
            continue
        mode = it.get("mode") if it.get("mode") in REGION_MODES else "blur"
        cleaned.append({"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5), "mode": mode})

    jobstore.update_job(job_id, regions=cleaned)
    return {"ok": True, "regions": cleaned}


@router.get("/api/jobs/{job_id}/media")
def list_media(job_id: str):
    job = _ensure(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {mid: _media_out(job_id, m) for mid, m in (job.media or {}).items()}


@router.post("/api/jobs/{job_id}/media")
async def add_media(job_id: str, file: UploadFile = File(...), translate: str = Form("false")):
    _editable(job_id)
    ext = Path(file.filename or "").suffix.lower()
    is_image = ext in composition.IMAGE_EXTS
    if not ext:
        ext = ".mp4"
    mid = uuid.uuid4().hex[:8]
    media_dir = OUTPUT_DIR / job_id / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{mid}{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        width, height = media_lib.get_video_size(dest)
        if is_image:
            duration, has_audio = composition.MAX_IMAGE_SECONDS, False
        else:
            duration = media_lib.get_duration(dest)
            has_audio = media_lib.has_audio(dest)
            if duration <= 0:
                raise ValueError("duration")
    except Exception:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Không đọc được tệp video/ảnh này")
    wants = translate.strip().lower() in ("1", "true", "yes", "on")
    entry = {
        "id": mid,
        "type": "image" if is_image else "video",
        "name": Path(file.filename or mid).name[:80],
        "path": str(dest),
        "preview": "",
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": has_audio,
        "translate": bool(wants and not is_image and has_audio),
        "state": "preparing",
        "tstate": "none",
        "tmessage": "" if (is_image or has_audio or not wants) else "Clip không có âm thanh",
        "error": "",
        "segments": {},
        "merged": False,
    }
    jobstore.add_media(job_id, entry)
    threading.Thread(target=prepare_media, args=(job_id, mid), daemon=True).start()
    return _media_out(job_id, entry)


@router.put("/api/jobs/{job_id}/media/{mid}")
def update_media_endpoint(job_id: str, mid: str, payload: dict):
    job = _editable(job_id)
    m = (job.media or {}).get(mid)
    if not m:
        raise HTTPException(status_code=404, detail="media not found")
    if "translate" in payload and m["type"] == "video" and mid != "main":
        want = bool(payload["translate"]) and bool(m.get("has_audio"))
        jobstore.update_media(job_id, mid, translate=want)
        if want and m.get("state") == "ready" and m.get("tstate") in ("none", "failed"):
            jobstore.update_media(job_id, mid, tstate="running", tmessage="Đang chuẩn bị", error="")
            threading.Thread(target=translate_media, args=(job_id, mid), daemon=True).start()
    fresh = jobstore.get_job(job_id)
    return _media_out(job_id, fresh.media[mid])


@router.get("/api/jobs/{job_id}/media/{mid}/file")
def media_file(job_id: str, mid: str, request: Request):
    job = jobstore.get_job(job_id)
    m = (job.media or {}).get(mid) if job else None
    if not m:
        raise HTTPException(status_code=404, detail="media not found")
    path = _media_playable(job_id, m)
    if not path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    if m["type"] == "image":
        return FileResponse(str(path))
    return _serve_with_range(path, request)


@router.get("/api/jobs/{job_id}/media/{mid}/thumb")
def media_thumb(job_id: str, mid: str):
    job = jobstore.get_job(job_id)
    m = (job.media or {}).get(mid) if job else None
    if not m:
        raise HTTPException(status_code=404, detail="media not found")
    out_dir = OUTPUT_DIR / job_id
    thumb = out_dir / "main_thumb.jpg" if mid == "main" else out_dir / "media" / f"{mid}_thumb.jpg"
    if not thumb.exists():
        try:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            media_lib.make_thumbnail(_media_playable(job_id, m), thumb, is_image=m["type"] == "image")
        except Exception:
            raise HTTPException(status_code=404, detail="no thumbnail")
    return FileResponse(str(thumb), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@router.put("/api/jobs/{job_id}/timeline")
def update_timeline(job_id: str, payload: dict):
    job = _editable(job_id)
    items = payload.get("clips")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="clips must be a list")
    clips = composition.sanitize_clips(items, job.media or {})
    if not clips or composition.comp_duration(clips) < 0.2:
        raise HTTPException(status_code=400, detail="timeline is empty")
    jobstore.update_job(job_id, clips=clips)
    merged = payload.get("merged")
    if isinstance(merged, list):
        for mid in merged:
            if str(mid) in (job.media or {}):
                jobstore.update_media(job_id, str(mid), merged=True)
    return {"ok": True, "clips": clips}


@router.post("/api/jobs/{job_id}/export")
def export_job(job_id: str):
    job = _ensure(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail=f"job is not ready to export (status={job.status})")
    if any(m.get("state") == "preparing" or m.get("tstate") == "running" for m in (job.media or {}).values()):
        raise HTTPException(status_code=400, detail="Clip mới đang được xử lý, hãy đợi xong rồi xuất.")

    # Set the status BEFORE starting the thread, otherwise the browser can poll,
    # still see "review" and reopen the editor instead of showing render progress.
    jobstore.update_job(job_id, status="rendering", progress=90, message="Rendering output", error="")

    thread = threading.Thread(target=render_job, args=(job_id,), daemon=True)
    thread.start()
    return {"ok": True}


def _serve_with_range(path: Path, request: Request):
    """Serve a file with HTTP Range support so the browser can seek (video and audio)."""
    size = path.stat().st_size
    media_type = "audio/mp4" if path.suffix == ".m4a" else (mimetypes.guess_type(path.name)[0] or "video/mp4")
    range_header = request.headers.get("range")
    match = re.match(r"bytes=(\d*)-(\d*)$", (range_header or "").strip())

    if not match or (match.group(1) == "" and match.group(2) == ""):
        return FileResponse(str(path), media_type=media_type, headers={"Accept-Ranges": "bytes"})

    start_s, end_s = match.groups()
    if start_s == "":
        start = max(0, size - int(end_s))
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        raise HTTPException(status_code=416, detail="range not satisfiable",
                            headers={"Content-Range": f"bytes */{size}"})

    def iter_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(iter_range(), status_code=206, media_type=media_type, headers=headers)


def _playable_video_path(job) -> Path:
    preview = OUTPUT_DIR / job.id / "preview.mp4"
    if preview.exists():
        return preview
    return Path(job.video_path)


@router.get("/api/jobs/{job_id}/video")
def stream_video(job_id: str, request: Request):
    """Serve the preview video with HTTP Range support so the browser can seek."""
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    path = _playable_video_path(job)
    if not path.exists():
        raise HTTPException(status_code=404, detail="video not found")

    return _serve_with_range(path, request)


@router.get("/api/jobs/{job_id}/download/{lang}/{kind}")
def download_output(job_id: str, lang: str, kind: str):
    job = jobstore.get_job(job_id)
    if not job or job.status != "completed":
        raise HTTPException(status_code=404, detail="output not ready")
    lang_outputs = job.outputs.get(lang)
    if not lang_outputs or kind not in lang_outputs:
        raise HTTPException(status_code=404, detail="file not found")
    file_path = OUTPUT_DIR / job_id / lang_outputs[kind]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    return FileResponse(str(file_path), filename=file_path.name)


# ---------------------------------------------------------------------------
# Dubbing preview: synthesize the dubbed track for the editor so the person can
# hear it in place of the original audio before exporting.
# ---------------------------------------------------------------------------

_dub_state: dict = {}          # (job_id, lang) -> {state, done, total, error}
_dub_lock = threading.Lock()


def _dub_job(job_id: str, lang: str):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if lang.startswith("_") or lang not in (job.segments or {}):
        raise HTTPException(status_code=400, detail="unknown language")
    return job


def _dub_files(job_id: str, lang: str) -> tuple[Path, Path]:
    d = OUTPUT_DIR / job_id
    return d / f"dub_preview_{lang}.m4a", d / f"dub_preview_{lang}.json"


def _run_dub_preview(job_id: str, lang: str, segs: list, sig: str, total_duration: float, source_segments,
                     voice: str = ""):
    key = (job_id, lang)

    def progress(done, total):
        with _dub_lock:
            st = _dub_state.get(key)
            if st:
                st["done"], st["total"] = done, total

    try:
        out_dir = OUTPUT_DIR / job_id
        speaker = dubbing.prepare_speaker_ref(out_dir, source_segments)
        wav = dubbing.build_dubbed_track(
            segs, lang, out_dir / f"dub_work_{lang}", total_duration,
            speaker_wav=speaker, cache_dir=out_dir / "tts_cache", progress_cb=progress, voice=voice,
        )
        audio, meta = _dub_files(job_id, lang)
        tmp = audio.with_name(f"dub_preview_{lang}.tmp.m4a")
        dubbing.encode_preview(wav, tmp)
        os.replace(tmp, audio)
        meta.write_text(json.dumps({"signature": sig}), encoding="utf-8")
        with _dub_lock:
            _dub_state[key] = {"state": "idle", "done": len(segs), "total": len(segs), "error": ""}
    except Exception as e:
        with _dub_lock:
            _dub_state[key] = {"state": "failed", "done": 0, "total": len(segs), "error": str(e)[:500]}


@router.post("/api/jobs/{job_id}/dub-preview/{lang}")
def start_dub_preview(job_id: str, lang: str):
    """Build (or incrementally update) the dubbed preview track from the SAVED segments."""
    job = _dub_job(job_id, lang)
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")
    segs = [dict(s) for s in job.segments[lang]]
    if not segs:
        raise HTTPException(status_code=400, detail="no segments to synthesize")

    key = (job_id, lang)
    with _dub_lock:
        st = _dub_state.get(key)
        if st and st.get("state") == "running":
            return {"ok": True, "state": "running"}
        _dub_state[key] = {"state": "running", "done": 0, "total": len(segs), "error": ""}

    voice = dubbing.mix_settings(job.dub)["voices"].get(lang, "")
    sig = dubbing.segments_signature(segs, lang, voice)
    threading.Thread(
        target=_run_dub_preview,
        args=(job_id, lang, segs, sig, _comp_total(job), job.segments.get("_source"), voice),
        daemon=True,
    ).start()
    return {"ok": True, "state": "running"}


@router.get("/api/jobs/{job_id}/dub-preview/{lang}/status")
def dub_preview_status(job_id: str, lang: str):
    job = _dub_job(job_id, lang)
    audio, meta = _dub_files(job_id, lang)
    audio_sig = ""
    if audio.exists() and meta.exists():
        try:
            audio_sig = json.loads(meta.read_text(encoding="utf-8")).get("signature", "")
        except Exception:
            audio_sig = ""
    voice = dubbing.mix_settings(job.dub)["voices"].get(lang, "")
    cur_sig = dubbing.segments_signature(job.segments[lang], lang, voice) if job.segments[lang] else ""
    with _dub_lock:
        st = dict(_dub_state.get((job_id, lang)) or {})
    return {
        "state": st.get("state", "idle"),
        "has_audio": bool(audio_sig),
        "up_to_date": bool(audio_sig) and audio_sig == cur_sig,
        "audio_version": audio_sig,
        "done": st.get("done", 0),
        "total": st.get("total", len(job.segments[lang])),
        "error": st.get("error", ""),
        "engine": dubbing.engine_label(lang, voice),
    }


@router.get("/api/jobs/{job_id}/dub-preview/{lang}/audio")
def dub_preview_audio(job_id: str, lang: str, request: Request):
    _dub_job(job_id, lang)
    audio, _ = _dub_files(job_id, lang)
    if not audio.exists():
        raise HTTPException(status_code=404, detail="dub preview not built yet")
    return _serve_with_range(audio, request)


VOICE_RE = re.compile(r"^(xtts|edge:[A-Za-z0-9\-]{3,80}|kokoro:[a-z_]{2,40})$")


@router.get("/api/voices/{lang}")
def get_voices(lang: str):
    """Voices available for a language (Edge voices need internet; XTTS only for the languages it speaks)."""
    return dubbing.list_voices(lang)


def _sample_text(segments: list) -> str:
    for s in segments or []:
        t = " ".join(str(s.get("text", "")).split())
        if t:
            if len(t) > 140:
                t = t[:140].rsplit(" ", 1)[0]
            return t
    return "Hello, this is a voice sample."


@router.get("/api/jobs/{job_id}/voice-preview/{lang}")
def voice_preview(job_id: str, lang: str, voice: str = ""):
    """Short audio sample: the first translated line of this job read with the chosen voice."""
    job = _dub_job(job_id, lang)
    voice = (voice or "").strip()
    if voice and not VOICE_RE.match(voice):
        raise HTTPException(status_code=400, detail="invalid voice")
    text = _sample_text(job.segments.get(lang))

    out_dir = OUTPUT_DIR / job_id
    sample_dir = out_dir / "voice_samples"
    sample_dir.mkdir(parents=True, exist_ok=True)
    path = sample_dir / f"{dubbing._h(lang, voice, text, dubbing.engine_id(lang, voice))}.wav"
    if not path.exists():
        speaker = None
        if dubbing.pick_engine(lang, voice) == "xtts":
            speaker = dubbing.prepare_speaker_ref(out_dir, job.segments.get("_source"))
        tmp = path.with_name(path.stem + ".tmp.wav")
        try:
            dubbing.synthesize_segment(text, lang, tmp, speaker_wav=speaker, voice=voice)
            os.replace(tmp, path)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(e)[:500])
    return FileResponse(str(path), media_type="audio/wav")


@router.put("/api/jobs/{job_id}/dub-settings")
def update_dub_settings(job_id: str, payload: dict):
    """Audio used on export: original only (enabled=false) or dubbed voice mixed with the original (volumes, ducking)."""
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("review", "completed", "failed"):
        raise HTTPException(status_code=400, detail="job is not in a state that allows editing")
    dub = dubbing.mix_settings(job.dub)
    try:
        dub["enabled"] = bool(payload.get("enabled", dub["enabled"]))
        dub["orig_volume"] = round(min(max(float(payload.get("orig_volume", dub["orig_volume"])), 0.0), 1.0), 3)
        dub["dub_volume"] = round(min(max(float(payload.get("dub_volume", dub["dub_volume"])), 0.0), 1.0), 3)
        dub["duck"] = bool(payload.get("duck", dub["duck"]))
        dub["duck_level"] = round(min(max(float(payload.get("duck_level", dub["duck_level"])), 0.0), 1.0), 3)
        if isinstance(payload.get("voices"), dict):
            voices = {}
            for k, v in payload["voices"].items():
                k, v = str(k).strip().lower()[:20], str(v).strip()
                if k and not k.startswith("_") and v and VOICE_RE.match(v):
                    voices[k] = v
            dub["voices"] = voices
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid dub settings")
    jobstore.update_job(job_id, dub=dub)
    return {"ok": True, "dub": dub}


# ---------------------------------------------------------------------------
# Dịch lại: chạy lại bước dịch cho job đã tải lên (không chạy lại nhận diện giọng nói)
# ---------------------------------------------------------------------------

def _norm_text(t) -> str:
    return " ".join(str(t or "").split()).lower()


def _source_texts(job) -> set:
    """Mọi câu gốc (video chính + clip thêm) – đoạn nào còn giống hệt câu gốc là chưa được dịch."""
    texts = {_norm_text(s["text"]) for s in (job.segments or {}).get("_source", [])}
    for m in (job.media or {}).values():
        for s in (m.get("segments") or {}).get("_source", []):
            texts.add(_norm_text(s.get("text")))
    texts.discard("")
    return texts


@router.post("/api/jobs/{job_id}/retranslate")
def retranslate(job_id: str, payload: dict | None = None):
    """payload: {"langs": [..] | null (mọi ngôn ngữ), "all": false (true = dịch lại cả đoạn đã dịch)}"""
    job = _editable(job_id)
    payload = payload or {}

    # Chưa có kết quả nhận diện (job lỗi từ sớm): chạy lại toàn bộ pipeline
    if not (job.segments or {}).get("_source"):
        if not job.video_path or not Path(job.video_path).exists():
            raise HTTPException(status_code=400, detail="không còn file video gốc để xử lý lại")
        jobstore.update_job(job_id, status="running", progress=5, message="Re-running", error="")
        threading.Thread(
            target=transcribe_and_translate,
            args=(job_id, Path(job.video_path), job.target_langs, job.context, job.source_lang or None),
            daemon=True,
        ).start()
        return {"ok": True, "restarted": True}

    all_langs = [l for l in job.segments if not l.startswith("_")]
    langs = payload.get("langs") or all_langs
    if any(l not in all_langs for l in langs):
        raise HTTPException(status_code=400, detail="unknown language")
    only_missing = not bool(payload.get("all", False))

    src = _source_texts(job)
    client = LLMClient(load_config())
    results, total_failed, last_error = {}, 0, ""

    for lang in langs:
        segs = [dict(s) for s in job.segments[lang]]
        idxs = [i for i, s in enumerate(segs) if not only_missing or _norm_text(s["text"]) in src]
        if not idxs:
            results[lang] = {"translated": 0, "failed": 0}
            continue
        items = [asr.Segment(id=n, start=float(segs[i]["start"]), end=float(segs[i]["end"]),
                             text=str(segs[i]["text"])) for n, i in enumerate(idxs)]
        stats: dict = {}
        out = translator.translate_all(client, items, lang, context=job.context, stats=stats)
        failed = int(stats.get("failed", 0))
        if failed >= len(idxs):
            raise HTTPException(
                status_code=400,
                detail=f"Không kết nối được mô hình AI ({lang}): {stats.get('error') or 'không rõ lỗi'}",
            )
        for n, i in enumerate(idxs):
            segs[i]["text"] = out[n]["text"]
        jobstore.set_segments_for_lang(job_id, lang, segs)
        results[lang] = {"translated": len(idxs) - failed, "failed": failed}
        total_failed += failed
        last_error = stats.get("error") or last_error

    return {"ok": True, "restarted": False, "results": results, "failed": total_failed, "error": last_error}