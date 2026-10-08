import os
import threading
from pathlib import Path
from app.config import OUTPUT_DIR
from app.core import jobstore, media, asr, translator, subtitles, dubbing, composition
from app.core.llm_client import LLMClient
from app.core.llm_config import load_config

_asr_lock = threading.Lock()


def _make_preview(video_path: Path, preview_path: Path) -> None:
    tmp = preview_path.with_name(preview_path.stem + ".tmp.mp4")
    try:
        media.run_ffmpeg([
            "-i", str(video_path),
            "-vf", "scale=-2:'min(480,ih)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(tmp),
        ])
        os.replace(tmp, preview_path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def transcribe_and_translate(job_id: str, video_path: Path, target_langs: list[str], context: str, source_lang: str | None):
    job_out_dir = OUTPUT_DIR / job_id
    job_out_dir.mkdir(parents=True, exist_ok=True)

    try:
        jobstore.update_job(job_id, status="running", progress=5, message="Extracting audio")
        preview_thread = threading.Thread(
            target=_make_preview, args=(video_path, job_out_dir / "preview.mp4"), daemon=True
        )
        preview_thread.start()

        audio_path = job_out_dir / "source_audio.wav"
        media.extract_audio(video_path, audio_path)

        jobstore.update_job(job_id, progress=15, message="Transcribing speech")
        with _asr_lock:
            segments, detected_lang = asr.transcribe(audio_path, language=source_lang)
        total_duration = media.get_duration(video_path)
        jobstore.update_job(job_id, total_duration=total_duration)
        composition.ensure_timeline(job_id)

        jobstore.set_segments_for_lang(
            job_id, "_source",
            [{"id": s.id, "start": s.start, "end": s.end, "text": s.text} for s in segments],
        )

        cfg = load_config()
        client = LLMClient(cfg)

        step_progress = 20
        step_size = max(1, int(70 / max(1, len(target_langs))))

        for lang in target_langs:
            jobstore.update_job(job_id, progress=step_progress, message=f"Translating to {lang}")
            translated_segments = translator.translate_all(client, segments, lang, context=context)
            jobstore.set_segments_for_lang(job_id, lang, translated_segments)
            step_progress += step_size

        jobstore.update_job(job_id, progress=88, message="Preparing preview")
        preview_thread.join()

        jobstore.update_job(
            job_id,
            status="review",
            progress=90,
            message="Translation ready for review. Edit segments then export.",
        )

    except Exception as e:
        jobstore.update_job(job_id, status="failed", error=str(e), message="Failed")


def prepare_media(job_id: str, mid: str):
    job = jobstore.get_job(job_id)
    m = (job.media or {}).get(mid) if job else None
    if not m:
        return
    media_dir = OUTPUT_DIR / job_id / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    src = Path(m["path"])
    try:
        if m["type"] == "video":
            preview = media_dir / f"{mid}_preview.mp4"
            _make_preview(src, preview)
            jobstore.update_media(job_id, mid, preview=str(preview) if preview.exists() else "")
        try:
            prev = jobstore.get_job(job_id).media[mid].get("preview")
            thumb_src = Path(prev) if prev and Path(prev).exists() else src
            media.make_thumbnail(thumb_src, media_dir / f"{mid}_thumb.jpg", is_image=m["type"] == "image")
        except Exception:
            pass
        jobstore.update_media(job_id, mid, state="ready")
    except Exception as e:
        jobstore.update_media(job_id, mid, state="failed", error=str(e)[:500])
        return
    fresh = (jobstore.get_job(job_id).media or {}).get(mid)
    if fresh and fresh.get("translate") and fresh["type"] == "video" and fresh.get("tstate") in ("none", "failed"):
        translate_media(job_id, mid)


def translate_media(job_id: str, mid: str):
    job = jobstore.get_job(job_id)
    m = (job.media or {}).get(mid) if job else None
    if not m:
        return
    try:
        jobstore.update_media(job_id, mid, tstate="running", tmessage="Đang tách âm thanh", error="")
        work = OUTPUT_DIR / job_id / "media"
        work.mkdir(parents=True, exist_ok=True)
        audio_path = work / f"{mid}_audio.wav"
        media.extract_audio(Path(m["path"]), audio_path)
        jobstore.update_media(job_id, mid, tmessage="Đang nhận diện giọng nói")
        with _asr_lock:
            segments, _ = asr.transcribe(audio_path, language=job.source_lang or None)
        result = {"_source": [{"id": s.id, "start": s.start, "end": s.end, "text": s.text} for s in segments]}
        client = LLMClient(load_config())
        langs = list(job.target_langs)
        for i, lang in enumerate(langs):
            jobstore.update_media(job_id, mid, tmessage=f"Đang dịch {lang} ({i + 1}/{len(langs)})")
            result[lang] = translator.translate_all(client, segments, lang, context=job.context) if segments else []
        jobstore.update_media(job_id, mid, segments=result, tstate="done", tmessage="", merged=False)
    except Exception as e:
        jobstore.update_media(job_id, mid, tstate="failed", tmessage="", error=str(e)[:500])


def _preview_height(job_out_dir: Path, frame_h: int) -> int:
    preview = job_out_dir / "preview.mp4"
    if preview.exists():
        try:
            return media.get_video_size(preview)[1]
        except Exception:
            pass
    return frame_h


def render_job(job_id: str):
    job = composition.ensure_timeline(job_id)
    if not job:
        return

    job_out_dir = OUTPUT_DIR / job_id
    video_path = Path(job.video_path)
    mode = job.mode
    use_dub = bool((job.dub or {}).get("enabled")) or mode == "dubbing"
    if mode not in ("subtitle_burn", "subtitle_soft"):
        mode = "subtitle_burn"

    try:
        jobstore.update_job(job_id, status="rendering", progress=90, message="Rendering output")
        outputs = {}

        _, src_h = media.get_video_size(video_path)
        ref_h = _preview_height(job_out_dir, src_h)
        size_unit = src_h / ref_h if ref_h else 1.0

        source_video = video_path
        clips = job.clips
        media_map = job.media or {}
        main_duration = float(job.total_duration or media.get_duration(video_path))
        out_duration = composition.comp_duration(clips)
        if out_duration < 0.2:
            raise RuntimeError("Timeline trống, không còn gì để xuất.")
        identity = composition.is_identity(clips, main_duration)

        full_ranges = [
            (s, e) for c, s, e in composition.layout(clips)
            if media_map[c["media"]]["type"] == "video" and not media_map[c["media"]].get("translate", True)
        ]

        wm = job.watermark if (job.watermark and job.watermark.get("enabled")) else None
        if not identity or job.regions or wm:
            jobstore.update_job(job_id, message="Composing / cleaning video")
            base = job_out_dir / "base.mp4"
            if media.prepare_base_video(
                video_path, base,
                clips=None if identity else clips, media_map=media_map,
                regions=job.regions, watermark=wm, size_unit=size_unit,
            ):
                video_path = base

        step_progress = 90
        step_size = max(1, int(10 / max(1, len(job.target_langs))))

        for lang in job.target_langs:
            translated_segments = [
                {**s, "end": min(float(s["end"]), out_duration)}
                for s in (job.segments.get(lang) or [])
                if float(s["start"]) < out_duration - 0.05
            ]
            translated_segments = [s for s in translated_segments if float(s["end"]) - float(s["start"]) >= 0.05]
            if not translated_segments:
                continue

            srt_path = job_out_dir / f"subtitles_{lang}.srt"
            subtitles.write_srt(translated_segments, srt_path)
            lang_outputs = {"srt": srt_path.name}

            lang_video = video_path
            dub_base = None
            if use_dub:
                jobstore.update_job(job_id, message=f"Synthesizing dubbed audio ({lang})")
                work_dir = job_out_dir / f"tts_work_{lang}"
                work_dir.mkdir(parents=True, exist_ok=True)
                speaker_wav = dubbing.prepare_speaker_ref(job_out_dir, job.segments.get("_source"))
                dubbed_audio = dubbing.build_dubbed_track(
                    translated_segments, lang, work_dir, out_duration,
                    speaker_wav=speaker_wav, cache_dir=job_out_dir / "tts_cache",
                    voice=dubbing.mix_settings(job.dub)["voices"].get(lang, ""),
                )
                mix = dubbing.mix_settings(job.dub)
                env_path = None
                if mix["duck"] and float(mix["orig_volume"]) > 0.001 and media.has_audio(video_path):
                    env_path = dubbing.write_duck_envelope(
                        translated_segments, out_duration, float(mix["duck_level"]),
                        work_dir / "duck_env.wav",
                    )
                dub_base = job_out_dir / f"video_{lang}_dub_base.mp4"
                media.mix_audio_track(
                    video_path, dubbed_audio, dub_base,
                    orig_volume=float(mix["orig_volume"]), dub_volume=float(mix["dub_volume"]),
                    envelope_path=env_path, full_ranges=full_ranges,
                )
                lang_video = dub_base

            jobstore.update_job(job_id, message=f"Rendering subtitled video ({lang})")
            out_video = job_out_dir / f"video_{lang}_subtitled.mp4"
            if mode == "subtitle_burn":
                style = job.subtitle_style or {}
                frame_w, frame_h = media.get_video_size(lang_video)
                ass_path = job_out_dir / f"subtitles_{lang}.ass"
                subtitles.write_ass(
                    translated_segments, ass_path, frame_w, frame_h,
                    font_size=int(style.get("font_size", 28)),
                    pos_x=float(style.get("x", 0.5)),
                    pos_y=float(style.get("y", 0.9)),
                    size_unit=size_unit,
                )
                media.burn_subtitles_ass(lang_video, ass_path, out_video)
            else:
                media.mux_soft_subtitles(lang_video, srt_path, out_video, lang)
            lang_outputs["video"] = out_video.name

            if dub_base is not None:
                dub_base.unlink(missing_ok=True)

            outputs[lang] = lang_outputs
            step_progress += step_size

        if video_path != source_video:
            video_path.unlink(missing_ok=True)

        jobstore.update_job(job_id, status="completed", progress=100, message="Done", outputs=outputs)

    except Exception as e:
        jobstore.update_job(job_id, status="failed", error=str(e), message="Render failed")