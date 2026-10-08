import json
import os
import subprocess
from pathlib import Path
from app.config import EXPORT_CRF, EXPORT_PRESET, EXPORT_AUDIO_BITRATE


def run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", *args]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "Khong tim thay ffmpeg. Hay cai ffmpeg va them thu muc chua ffmpeg.exe vao bien moi truong PATH, "
            "sau do mo terminal moi va khoi dong lai server."
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="ignore")[-4000:])


def final_video_args() -> list[str]:
    """Encoder settings of the file the person downloads: near-lossless, full source resolution."""
    return ["-c:v", "libx264", "-preset", EXPORT_PRESET, "-crf", str(EXPORT_CRF),
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def intermediate_video_args() -> list[str]:
    """Shared clean-up pass (cut / logo removal / watermark): visually transparent, encoded once."""
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "12", "-pix_fmt", "yuv420p"]


def extract_audio(video_path: Path, audio_path: Path) -> None:
    run_ffmpeg(["-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_path)])


def get_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(
            "Khong tim thay ffprobe. Hay cai ffmpeg (kem ffprobe) va them vao bien moi truong PATH."
        )
    return float(out.stdout.decode().strip())


def get_video_size(path: Path) -> tuple[int, int]:
    """Displayed frame size (width, height), taking phone-style rotation metadata into account."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("Khong tim thay ffprobe. Hay cai ffmpeg (kem ffprobe) va them vao bien moi truong PATH.")
    info = json.loads(out.stdout.decode() or "{}")
    stream = (info.get("streams") or [{}])[0]
    width, height = int(stream["width"]), int(stream["height"])
    rotation = 0
    try:
        rotation = int(float((stream.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        pass
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def get_fps(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw = out.stdout.decode().strip().splitlines()[0]
        num, _, den = raw.partition("/")
        value = float(num) / float(den or 1)
    except Exception:
        return 30.0
    if value <= 0:
        return 30.0
    return min(max(value, 1.0), 60.0)


def make_thumbnail(src: Path, out: Path, is_image: bool = False, at: float = 0.0) -> None:
    tmp = out.with_name(out.stem + ".tmp.jpg")
    args = []
    if not is_image and at > 0:
        args += ["-ss", f"{at:.2f}"]
    args += ["-i", str(src), "-frames:v", "1", "-vf", "scale=-2:120", "-q:v", "4", str(tmp)]
    run_ffmpeg(args)
    os.replace(tmp, out)


def _atempo_chain(speed: float) -> list[str]:
    if abs(speed - 1.0) < 1e-3:
        return []
    chain = []
    s = speed
    while s > 2.0:
        chain.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        chain.append("atempo=0.5")
        s /= 0.5
    chain.append(f"atempo={s:.4f}")
    return chain


def _even(n: int) -> int:
    return max(2, n - n % 2)


def prepare_base_video(video_path: Path, output_path: Path, clips=None, media_map=None, regions=None,
                       watermark=None, size_unit: float = 1.0) -> bool:
    frame_w, frame_h = get_video_size(video_path)
    inputs: list[str] = []
    parts: list[str] = []
    n_inputs = 0
    audio = has_audio(video_path)
    label = "0:v"
    alabel = None

    if clips:
        frame_w, frame_h = _even(frame_w), _even(frame_h)
        fps = get_fps(video_path)
        media_map = media_map or {}
        fit = (
            f"scale={frame_w}:{frame_h}:force_original_aspect_ratio=decrease,"
            f"pad={frame_w}:{frame_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps:.3f},format=yuv420p"
        )
        for i, c in enumerate(clips):
            m = media_map[c["media"]]
            speed = float(c.get("speed", 1.0)) or 1.0
            start, end = float(c["in"]), float(c["out"])
            length = max(0.05, (end - start) / speed)
            silence = f"anullsrc=r=48000:cl=stereo,atrim=duration={length:.3f},asetpts=PTS-STARTPTS[ca{i}]"
            if m["type"] == "image":
                inputs += ["-loop", "1", "-framerate", f"{fps:.3f}", "-t", f"{length:.3f}", "-i", str(m["path"])]
                parts.append(f"[{n_inputs}:v]{fit}[cv{i}]")
                parts.append(silence)
            else:
                inputs += ["-i", str(m["path"])]
                parts.append(
                    f"[{n_inputs}:v]trim=start={start:.3f}:end={end:.3f},"
                    f"setpts=(PTS-STARTPTS)/{speed:.4f},{fit}[cv{i}]"
                )
                if m.get("has_audio"):
                    chain = [f"atrim=start={start:.3f}:end={end:.3f}", "asetpts=PTS-STARTPTS", *_atempo_chain(speed)]
                    volume = float(c.get("volume", 1.0))
                    if abs(volume - 1.0) > 0.01:
                        chain.append(f"volume={volume:.3f}")
                    chain += ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
                    parts.append(f"[{n_inputs}:a]{','.join(chain)}[ca{i}]")
                else:
                    parts.append(silence)
            n_inputs += 1
        pads = "".join(f"[cv{i}][ca{i}]" for i in range(len(clips)))
        parts.append(f"{pads}concat=n={len(clips)}:v=1:a=1[vcut][acut]")
        label, alabel, audio = "vcut", "acut", True
    else:
        inputs = ["-i", str(video_path)]
        n_inputs = 1

    for i, r in enumerate(regions or []):
        mode = r.get("mode", "blur")
        x = int(round(r["x"] * frame_w))
        y = int(round(r["y"] * frame_h))
        w = max(8, int(round(r["w"] * frame_w)))
        h = max(8, int(round(r["h"] * frame_h)))
        if mode == "delogo":
            x, y = max(1, x), max(1, y)
            w, h = min(w, frame_w - 1 - x), min(h, frame_h - 1 - y)
        else:
            x, y = min(max(0, x), frame_w - 8), min(max(0, y), frame_h - 8)
            w, h = min(w, frame_w - x), min(h, frame_h - y)
        if w < 8 or h < 8:
            continue
        nxt = f"rg{i}"
        if mode == "delogo":
            parts.append(f"[{label}]delogo=x={x}:y={y}:w={w}:h={h}[{nxt}]")
        elif mode == "box":
            parts.append(f"[{label}]drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill[{nxt}]")
        else:
            radius = max(1, min(20, min(w, h) // 4 - 1))
            parts.append(
                f"[{label}]split[sa{i}][sb{i}];"
                f"[sb{i}]crop={w}:{h}:{x}:{y},boxblur={radius}:3[sc{i}];"
                f"[sa{i}][sc{i}]overlay={x}:{y}[{nxt}]"
            )
        label = nxt

    if watermark and watermark.get("enabled"):
        opacity = min(max(float(watermark.get("opacity", 0.5)), 0.0), 1.0)
        x = int(round(float(watermark.get("x", 0.82)) * frame_w))
        y = int(round(float(watermark.get("y", 0.86)) * frame_h))
        if watermark.get("type") == "image":
            image_path = watermark.get("image", "")
            if image_path and Path(image_path).exists():
                inputs += ["-i", str(image_path)]
                scale = min(max(float(watermark.get("scale", 0.16)), 0.02), 1.0)
                wm_w = max(8, int(round(scale * frame_w)))
                parts.append(f"[{n_inputs}:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={opacity:.3f}[wm]")
                parts.append(f"[{label}][wm]overlay=x={x}:y={y}[vwm]")
                label = "vwm"
                n_inputs += 1
        else:
            text = str(watermark.get("text", "")).strip()
            if text:
                font_size = min(max(int(round(int(watermark.get("font_size", 24)) * size_unit)), 8), 800)
                escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")
                parts.append(
                    f"[{label}]drawtext=text='{escaped}':fontcolor=white@{opacity:.3f}:fontsize={font_size}:"
                    f"x={x}:y={y}:shadowcolor=black@{min(opacity, 0.6):.3f}:shadowx=1:shadowy=1[vwm]"
                )
                label = "vwm"

    if not parts or label == "0:v":
        return False

    maps = ["-map", f"[{label}]"]
    if audio and alabel:
        maps += ["-map", f"[{alabel}]"]
        audio_args = ["-c:a", "aac", "-b:a", "320k"]
    elif audio:
        maps += ["-map", "0:a?"]
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = []

    run_ffmpeg([
        *inputs,
        "-filter_complex", ";".join(parts),
        *maps,
        *intermediate_video_args(),
        *audio_args,
        "-movflags", "+faststart",
        str(output_path),
    ])
    return True


def burn_subtitles(video_path: Path, srt_path: Path, output_path: Path) -> None:
    escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    run_ffmpeg([
        "-i", str(video_path),
        "-vf", f"subtitles='{escaped}'",
        *final_video_args(),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ])


def burn_subtitles_ass(video_path: Path, ass_path: Path, output_path: Path) -> None:
    escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
    run_ffmpeg([
        "-i", str(video_path),
        "-vf", f"ass='{escaped}'",
        *final_video_args(),
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ])


def mux_soft_subtitles(video_path: Path, srt_path: Path, output_path: Path, lang_code: str) -> None:
    run_ffmpeg([
        "-i", str(video_path),
        "-i", str(srt_path),
        "-map", "0", "-map", "1",
        "-c", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={lang_code}",
        str(output_path),
    ])


def replace_audio_track(video_path: Path, audio_path: Path, output_path: Path, keep_original: bool = False) -> None:
    if keep_original:
        run_ffmpeg([
            "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v", "-map", "0:a", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac",
            "-metadata:s:a:0", "language=orig",
            "-metadata:s:a:1", "language=dub",
            str(output_path),
        ])
    else:
        run_ffmpeg([
            "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE, "-shortest",
            str(output_path),
        ])


def has_audio(path: Path) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index",
           "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError("Khong tim thay ffprobe. Hay cai ffmpeg (kem ffprobe) va them vao bien moi truong PATH.")
    return bool(out.stdout.decode().strip())


def audio_channels(path: Path) -> int:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
           "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return int(out.stdout.decode().strip().split(",")[0])
    except Exception:
        return 2


_MONO_TO_STEREO = "aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0"   # duplicate, keeps the level (plain upmix is -3 dB)


def mix_audio_track(video_path: Path, dub_path: Path, output_path: Path,
                    orig_volume: float = 0.25, dub_volume: float = 1.0,
                    envelope_path: Path | None = None, full_ranges=None) -> None:
    ranges = [(float(a), float(b)) for a, b in (full_ranges or []) if float(b) - float(a) > 0.01][:60]
    use_orig = (orig_volume > 0.001 or bool(ranges)) and has_audio(video_path)
    if not use_orig:
        if abs(dub_volume - 1.0) < 0.01:
            replace_audio_track(video_path, dub_path, output_path, keep_original=False)
            return
        graph = f"[1:a]volume={dub_volume:.3f}[aout]"
        inputs = ["-i", str(video_path), "-i", str(dub_path)]
    else:
        if audio_channels(video_path) == 1:
            orig_chain = f"aresample=48000,{_MONO_TO_STEREO}"
        else:
            orig_chain = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        dub_chain = f"aresample=48000,{_MONO_TO_STEREO}"
        inputs = ["-i", str(video_path), "-i", str(dub_path)]
        if ranges:
            cond = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
            vol = f"volume='if(gt({cond},0),1,{orig_volume:.3f})':eval=frame"
        else:
            vol = f"volume={orig_volume:.3f}"
        parts = [f"[0:a]{orig_chain},{vol}[o0]"]
        last = "o0"
        if envelope_path:
            inputs += ["-i", str(envelope_path)]
            parts.append(f"[2:a]{dub_chain}[env]")
            parts.append("[o0][env]amultiply[o1]")
            last = "o1"
        parts.append(f"[1:a]{dub_chain},volume={dub_volume:.3f}[d0]")
        parts.append(f"[{last}][d0]amerge=inputs=2,pan=stereo|c0=c0+c2|c1=c1+c3,alimiter=limit=0.97[aout]")
        graph = ";".join(parts)

    run_ffmpeg([
        *inputs,
        "-filter_complex", graph,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", EXPORT_AUDIO_BITRATE, "-shortest",
        str(output_path),
    ])


def time_stretch(input_path: Path, output_path: Path, factor: float) -> None:
    factor = max(0.5, min(2.0, factor))
    run_ffmpeg(["-i", str(input_path), "-filter:a", f"atempo={factor:.4f}", str(output_path)])


def concat_audio_with_silence(clips: list[tuple[float, Path]], total_duration: float, output_path: Path) -> None:
    from pydub import AudioSegment
    track = AudioSegment.silent(duration=int(total_duration * 1000))
    for start_sec, clip_path in clips:
        clip = AudioSegment.from_file(clip_path)
        pos_ms = int(start_sec * 1000)
        track = track.overlay(clip, position=pos_ms)
    track.export(output_path, format="wav")