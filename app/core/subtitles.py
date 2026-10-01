import pysrt
from pathlib import Path


def _to_srt_time(seconds: float) -> pysrt.SubRipTime:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return pysrt.SubRipTime(hours=hours, minutes=minutes, seconds=secs, milliseconds=ms)


def write_srt(segments: list[dict], output_path: Path) -> None:
    subs = pysrt.SubRipFile()
    for i, seg in enumerate(segments, start=1):
        item = pysrt.SubRipItem(
            index=i,
            start=_to_srt_time(seg["start"]),
            end=_to_srt_time(seg["end"]),
            text=seg["text"],
        )
        subs.append(item)
    subs.save(str(output_path), encoding="utf-8")


def _to_ass_time(seconds: float) -> str:
    cs = int(round(seconds * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


# libass sizes a font by its line height (ascent + descent), CSS by its em: ~1.12x for Arial.
ASS_FONT_FACTOR = 1.12


def write_ass(segments: list[dict], output_path: Path, video_width: int, video_height: int,
              font_size: int = 28, pos_x: float = 0.5, pos_y: float = 0.9, size_unit: float = 1.0) -> None:
    """font_size is in the editor's preview pixels; size_unit = real frame height / preview height,
    so the exported text has the same size relative to the frame as in the preview."""
    px = int(round(pos_x * video_width))
    py = int(round(pos_y * video_height))
    fs = max(8, int(round(font_size * size_unit * ASS_FONT_FACTOR)))
    outline = max(2, int(round(fs / 16)))
    shadow = max(1, int(round(fs / 32)))
    margin_lr = int(round(video_width * 0.08))      # preview wraps text at 84% of the frame width

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Arial,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},{shadow},5,{margin_lr},{margin_lr},10,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = []
    for seg in segments:
        text = _ass_escape(str(seg["text"]).strip())
        if not text:
            continue
        start = _to_ass_time(seg["start"])
        end = _to_ass_time(seg["end"])
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\an5\\pos({px},{py})}}{text}")

    output_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")