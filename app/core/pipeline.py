import os
import threading
from pathlib import Path
from app.config import OUTPUT_DIR
from app.core import jobstore, media, asr, translator, subtitles, dubbing, cuts as cutlib
from app.core.llm_client import LLMClient
from app.core.llm_config import load_config


def _make_preview(video_path: Path, preview_path: Path) -> None:
    """Small H.264 480p copy for the in-browser editor. Failure is non-fatal (falls back to the original)."""
    tmp = preview_path.with_name("preview.tmp.mp4")
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
        segments, detected_lang = asr.transcribe(audio_path, language=source_lang)
        total_duration = media.get_duration(video_path)
        jobstore.update_job(job_id, total_duration=total_duration)

        # Keep the original-language segments so the editor can show what was actually said.
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


def _preview_height(job_out_dir: Path, frame_h: int) -> int:
    """Height of the video the editor shows (preview.mp4 is at most 480p; falls back to the original)."""
    preview = job_out_dir / "preview.mp4"
    if preview.exists():
        try:
            return media.get_video_size(preview)[1]
        except Exception:
            pass
    return frame_h


def render_job(job_id: str):
    job = jobstore.get_job(job_id)
    if not job:
        return

    job_out_dir = OUTPUT_DIR / job_id
    video_path = Path(job.video_path)
    mode = job.mode
    # The dubbed voice is an option on top of any subtitle mode.
    # "dubbing" is the legacy standalone mode: it now means burn-in subtitles + dubbed voice.
    use_dub = bool((job.dub or {}).get("enabled")) or mode == "dubbing"
    if mode not in ("subtitle_burn", "subtitle_soft"):
        mode = "subtitle_burn"

    try:
        jobstore.update_job(job_id, status="rendering", progress=90, message="Rendering output")
        outputs = {}

        # Font sizes in the editor are pixels of its (<=480p) preview video. Scale them to the real
        # frame, otherwise the exported subtitles/watermark look much smaller than in the preview.
        _, src_h = media.get_video_size(video_path)
        ref_h = _preview_height(job_out_dir, src_h)
        size_unit = src_h / ref_h if ref_h else 1.0

        # Cut ranges (removed parts of the source timeline).
        source_video = video_path
        total_duration = job.total_duration or media.get_duration(video_path)
        cut_list = cutlib.normalize_cuts(job.cuts, total_duration)
        keep = cutlib.keep_ranges(cut_list, total_duration) if cut_list else None
        if cut_list and not keep:
            raise RuntimeError("Đã cắt toàn bộ video, không còn gì để xuất.")
        out_duration = cutlib.kept_duration(keep) if keep else total_duration

        # Cut + blur/cover/delogo + watermark in ONE encode shared by all languages
        # (fewer generations of re-encoding = better picture).
        wm = job.watermark if (job.watermark and job.watermark.get("enabled")) else None
        if keep or job.regions or wm:
            jobstore.update_job(job_id, message="Cutting / cleaning video")
            base = job_out_dir / "base.mp4"
            if media.prepare_base_video(video_path, base, keep_ranges=keep, regions=job.regions,
                                        watermark=wm, size_unit=size_unit):
                video_path = base

        step_progress = 90
        step_size = max(1, int(10 / max(1, len(job.target_langs))))

        for lang in job.target_langs:
            translated_segments = job.segments.get(lang)
            if not translated_segments:
                continue
            if cut_list:
                # subtitles/dubbing follow the shortened timeline
                translated_segments = cutlib.remap_segments(translated_segments, cut_list)

            srt_path = job_out_dir / f"subtitles_{lang}.srt"
            subtitles.write_srt(translated_segments, srt_path)
            lang_outputs = {"srt": srt_path.name}

            # Optional: swap the original audio for the dubbed voice (shares the editor preview's cache).
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
                    envelope_path=env_path,
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
            video_path.unlink(missing_ok=True)   # heavy intermediate file

        jobstore.update_job(job_id, status="completed", progress=100, message="Done", outputs=outputs)

    except Exception as e:
        jobstore.update_job(job_id, status="failed", error=str(e), message="Render failed")