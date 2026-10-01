"""Video cutting helpers.

A "cut" is a part of the SOURCE timeline (seconds) that is removed on export.
Subtitles stay on the source timeline in the editor; on export they are remapped
onto the shortened timeline with remap_segments().
"""

MIN_CUT = 0.05


def normalize_cuts(items, duration: float = 0.0) -> list[dict]:
    """Validate, clamp to the video, sort and merge overlapping/touching ranges."""
    raw = []
    for it in items or []:
        try:
            start = max(0.0, float(it["start"]))
            end = float(it["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if duration and duration > 0:
            end = min(end, duration)
        if end - start >= MIN_CUT:
            raw.append([start, end])
    raw.sort()
    merged: list[list[float]] = []
    for s, e in raw:
        if merged and s <= merged[-1][1] + 1e-3:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [{"start": round(s, 3), "end": round(e, 3)} for s, e in merged]


def keep_ranges(cuts: list[dict], duration: float) -> list[tuple[float, float]]:
    """Complement of the cuts: the parts of the video that stay."""
    keep: list[tuple[float, float]] = []
    pos = 0.0
    for c in cuts:
        if c["start"] - pos >= MIN_CUT:
            keep.append((pos, c["start"]))
        pos = max(pos, c["end"])
    if duration - pos >= MIN_CUT:
        keep.append((pos, float(duration)))
    return keep


def kept_duration(keep: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in keep)


def remap_time(t: float, cuts: list[dict]) -> float:
    """Source time -> time on the shortened video (a time inside a cut maps to the cut point)."""
    removed = 0.0
    for c in cuts:
        if t <= c["start"]:
            break
        removed += min(t, c["end"]) - c["start"]
    return t - removed


def remap_segments(segments: list[dict], cuts: list[dict]) -> list[dict]:
    out = []
    for seg in segments:
        s = remap_time(float(seg["start"]), cuts)
        e = remap_time(float(seg["end"]), cuts)
        if e - s < MIN_CUT:
            continue  # the whole line was inside a cut
        out.append({**seg, "start": round(s, 3), "end": round(e, 3)})
    for i, seg in enumerate(out):
        seg["id"] = i
    return out