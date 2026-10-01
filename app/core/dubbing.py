import hashlib
import os
import re
import shutil
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from app.config import TTS_MODEL_NAME
from app.core.media import time_stretch, concat_audio_with_silence, get_duration, run_ffmpeg

os.environ.setdefault("COQUI_TOS_AGREED", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

_tts_model = None
# The TTS model is not thread-safe (preview thread and export thread may both call it).
_tts_lock = threading.RLock()


@contextmanager
def _torch_load_full():
    """PyTorch >= 2.6 defaults torch.load(weights_only=True), which rejects the Coqui XTTS checkpoint
    ("Weights only load failed"). The checkpoint is the official model downloaded by Coqui itself, so
    load it the old way, only while the model is being created."""
    import torch
    orig = torch.load

    def patched(*args, **kwargs):
        if kwargs.get("weights_only") is None:
            kwargs["weights_only"] = False
        return orig(*args, **kwargs)

    torch.load = patched
    try:
        yield
    finally:
        torch.load = orig


def get_tts():
    global _tts_model
    with _tts_lock:
        if _tts_model is None:
            os.environ["COQUI_TOS_AGREED"] = "1"
            os.environ["HF_HUB_DISABLE_XET"] = "1"
            from TTS.api import TTS
            with _torch_load_full():
                _tts_model = TTS(TTS_MODEL_NAME)
        return _tts_model


LANG_MAP = {
    "en": "en", "es": "es", "fr": "fr", "de": "de",
    "it": "it", "pt": "pt", "pl": "pl", "tr": "tr", "ru": "ru",
    "nl": "nl", "cs": "cs", "ar": "ar", "zh": "zh-cn", "ja": "ja",
    "ko": "ko", "hu": "hu", "hi": "hi",
}

# Languages the XTTS v2 model can speak. Vietnamese (and others) are NOT in this list.
XTTS_LANGS = {"en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar",
              "zh-cn", "hu", "ko", "ja", "hi"}

# TTS_ENGINE: "auto" (XTTS when the language is supported, otherwise Microsoft Edge voices),
# "xtts" (always XTTS) or "edge" (always Edge voices; online, no voice cloning).
TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").strip().lower()

EDGE_VOICES = {
    "vi": "vi-VN-HoaiMyNeural", "en": "en-US-AriaNeural", "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural", "zh": "zh-CN-XiaoxiaoNeural", "zh-cn": "zh-CN-XiaoxiaoNeural",
    "fr": "fr-FR-DeniseNeural", "es": "es-ES-ElviraNeural", "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural", "pt": "pt-BR-FranciscaNeural", "ru": "ru-RU-SvetlanaNeural",
    "th": "th-TH-PremwadeeNeural", "id": "id-ID-GadisNeural", "hi": "hi-IN-SwaraNeural",
    "ar": "ar-SA-ZariyahNeural", "tr": "tr-TR-EmelNeural", "nl": "nl-NL-ColetteNeural",
    "pl": "pl-PL-ZofiaNeural", "cs": "cs-CZ-VlastaNeural", "hu": "hu-HU-NoemiNeural",
    "ms": "ms-MY-YasminNeural", "fil": "fil-PH-BlessicaNeural",
}
# Override voices with e.g. EDGE_TTS_VOICES="vi=vi-VN-NamMinhNeural,en=en-US-GuyNeural"
for _pair in os.getenv("EDGE_TTS_VOICES", "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        if _k.strip() and _v.strip():
            EDGE_VOICES[_k.strip().lower()] = _v.strip()


def _xtts_lang(lang: str) -> str:
    l = lang.strip().lower()
    return LANG_MAP.get(l, l)


def _voice_kind(voice: str | None) -> tuple[str, str]:
    """Split a stored voice id ("" | "xtts" | "edge:<ShortName>") into (engine, name)."""
    v = (voice or "").strip()
    if v == "xtts":
        return "xtts", ""
    if v.startswith("edge:") and len(v) > 5:
        return "edge", v[5:]
    return "", ""


def pick_engine(lang: str, voice: str | None = None) -> str:
    kind, _ = _voice_kind(voice)
    if kind:                                   # a voice picked in the editor wins over the .env default
        return kind
    if TTS_ENGINE in ("xtts", "edge"):
        return TTS_ENGINE
    return "xtts" if _xtts_lang(lang) in XTTS_LANGS else "edge"


def edge_voice(lang: str, voice: str | None = None) -> str | None:
    kind, name = _voice_kind(voice)
    if kind == "edge":
        return name
    l = lang.strip().lower()
    return EDGE_VOICES.get(l) or EDGE_VOICES.get(l.split("-")[0])


def engine_id(lang: str, voice: str | None = None) -> str:
    """Identity of the voice used for `lang`; part of cache keys and of the preview signature."""
    if pick_engine(lang, voice) == "edge":
        return f"edge:{edge_voice(lang, voice)}"
    return "xtts"


def engine_label(lang: str, voice: str | None = None) -> str:
    if pick_engine(lang, voice) == "edge":
        return f"Microsoft Edge – {edge_voice(lang, voice) or 'chưa có giọng cho ngôn ngữ này'} (cần internet, không clone giọng gốc)"
    return "XTTS v2 (clone giọng từ video gốc)"


def _h(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def segments_signature(segments: list[dict], lang: str, voice: str | None = None) -> str:
    """Fingerprint of everything that affects the dubbed track (timing + text)."""
    h = hashlib.sha1(f"{lang}|{engine_id(lang, voice)}".encode("utf-8"))
    for s in segments:
        h.update(f"|{float(s['start']):.3f}|{float(s['end']):.3f}|{str(s['text']).strip()}".encode("utf-8"))
    return h.hexdigest()[:16]


def prepare_speaker_ref(out_dir: Path, source_segments: list[dict] | None, max_len: float = 15.0) -> str | None:
    """Short voice-cloning reference (a few seconds of the original speech) instead of the whole audio."""
    src = out_dir / "source_audio.wav"
    if not src.exists():
        return None
    ref = out_dir / "speaker_ref.wav"
    if ref.exists():
        return str(ref)
    start = 0.0
    if source_segments:
        start = max(0.0, float(source_segments[0].get("start", 0.0)))
    try:
        run_ffmpeg(["-ss", f"{start:.2f}", "-t", f"{max_len:.1f}", "-i", str(src),
                    "-ac", "1", "-ar", "22050", str(ref)])
        if ref.exists() and ref.stat().st_size > 1000:
            return str(ref)
    except Exception:
        pass
    return str(src)


_DROP_CATEGORIES = {"So", "Sk", "Cc", "Cf", "Cs", "Co", "Cn"}   # emoji, ♪, control/format chars


def _clean_for_tts(text: str) -> str:
    t = "".join(ch for ch in text if unicodedata.category(ch) not in _DROP_CATEGORIES or ch in " \n")
    return re.sub(r"\s+", " ", t).strip()


def _write_silence(output_path: Path, seconds: float = 0.4) -> None:
    run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", f"{seconds}", str(output_path)])


# Edge's service throttles rapid back-to-back connections (first lines work, then it answers
# "No audio was received"). So every Edge request is serialized, spaced out, and retried with backoff.
_edge_lock = threading.Lock()
_edge_state = {"last": 0.0}
EDGE_MIN_INTERVAL = float(os.getenv("EDGE_TTS_MIN_INTERVAL", "1.0"))   # seconds between requests
EDGE_MAX_ATTEMPTS = int(os.getenv("EDGE_TTS_MAX_ATTEMPTS", "5"))


def _synthesize_edge(text: str, lang: str, output_path: Path, voice_id: str | None = None) -> None:
    voice = edge_voice(lang, voice_id)
    if not voice:
        raise RuntimeError(
            f"Chưa có giọng Edge cho ngôn ngữ '{lang}'. Đặt biến môi trường, ví dụ "
            f"EDGE_TTS_VOICES=\"{lang}=<tên-giọng>\" (xem danh sách bằng lệnh: edge-tts --list-voices)."
        )
    try:
        import asyncio
        import edge_tts
        from importlib.metadata import version as _pkg_version
        edge_ver = _pkg_version("edge-tts")
    except ImportError:
        raise RuntimeError("Thiếu thư viện edge-tts. Chạy: pip install -U edge-tts")

    # Whisper often emits lines like "♪", "..." or emoji: nothing to speak, and Edge answers
    # "No audio was received". Emit a short silence instead of failing the whole build.
    clean = _clean_for_tts(text)
    if not any(ch.isalnum() for ch in clean):
        _write_silence(output_path)
        return

    mp3 = output_path.with_suffix(".mp3")
    last_err = None
    with _edge_lock:
        for attempt in range(max(1, EDGE_MAX_ATTEMPTS)):
            wait = EDGE_MIN_INTERVAL - (time.monotonic() - _edge_state["last"])
            if attempt > 0:
                wait = max(wait, 3.0 * attempt)          # backoff: 3s, 6s, 9s, 12s
            if wait > 0:
                time.sleep(wait)
            try:
                async def _run():
                    await edge_tts.Communicate(clean, voice).save(str(mp3))
                asyncio.run(_run())
                if mp3.exists() and mp3.stat().st_size > 0:
                    last_err = None
                else:
                    last_err = RuntimeError("Edge trả về file rỗng")
            except Exception as e:  # throttling / network hiccup: retry
                last_err = e
                mp3.unlink(missing_ok=True)
            _edge_state["last"] = time.monotonic()
            if last_err is None:
                break
    if last_err is not None or not mp3.exists():
        mp3.unlink(missing_ok=True)
        shown = clean if len(clean) <= 80 else clean[:77] + "..."
        raise RuntimeError(
            f"Edge TTS thất bại (edge-tts {edge_ver}, giọng {voice}) ở đoạn: \"{shown}\" -> {last_err}. "
            f"Thử: pip install -U edge-tts; kiểm tra internet/VPN/proxy và đồng hồ hệ thống; "
            f"hoặc sửa nội dung đoạn đó rồi bấm Cập nhật."
        )
    try:
        run_ffmpeg(["-i", str(mp3), "-ar", "24000", "-ac", "1", str(output_path)])
    finally:
        mp3.unlink(missing_ok=True)


def synthesize_segment(text: str, lang: str, output_path: Path, speaker_wav: str | None = None,
                       voice: str | None = None) -> None:
    if pick_engine(lang, voice) == "edge":
        _synthesize_edge(text, lang, output_path, voice)
        return
    tts_lang = _xtts_lang(lang)
    if tts_lang not in XTTS_LANGS:
        raise RuntimeError(
            f"XTTS không hỗ trợ ngôn ngữ '{lang}'. Đặt TTS_ENGINE=auto (mặc định) để dùng giọng Edge cho ngôn ngữ này."
        )
    with _tts_lock:
        tts = get_tts()
        kwargs = dict(text=text, language=tts_lang, file_path=str(output_path))
        if speaker_wav:
            kwargs["speaker_wav"] = speaker_wav
        elif getattr(tts, "speakers", None):
            kwargs["speaker"] = tts.speakers[0]
        else:
            raise RuntimeError(
                "This TTS model requires a reference voice sample (speaker_wav) but none was provided "
                "and the model has no built-in named speakers."
            )
        tts.tts_to_file(**kwargs)


def build_dubbed_track(segments: list[dict], lang: str, work_dir: Path, total_duration: float,
                       speaker_wav: str | None = None, cache_dir: Path | None = None,
                       progress_cb=None, voice: str | None = None) -> Path:
    """Synthesize every segment and lay the clips on a silent track.

    Clips are cached by (lang, text) and (lang, text, target length), so re-building after an
    edit only re-synthesizes the segments that changed. Preview and final export share the cache.
    Speech is only sped up to fit its slot (never slowed down), so voices stay natural.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir or work_dir
    cache.mkdir(parents=True, exist_ok=True)

    items = [s for s in segments if str(s.get("text", "")).strip()]
    clips = []
    for i, seg in enumerate(items):
        text = str(seg["text"]).strip()
        key_raw = _h(lang, text, engine_id(lang, voice))
        raw_path = cache / f"raw_{key_raw}.wav"
        if not raw_path.exists():
            tmp = cache / f"raw_{key_raw}.tmp.wav"
            synthesize_segment(text, lang, tmp, speaker_wav=speaker_wav, voice=voice)
            os.replace(tmp, raw_path)

        target_len = max(0.3, float(seg["end"]) - float(seg["start"]))
        st_path = cache / f"seg_{_h(key_raw, round(target_len, 2))}.wav"
        if not st_path.exists():
            actual_len = get_duration(raw_path)
            factor = actual_len / target_len if actual_len > 0 else 1.0
            tmp = cache / (st_path.stem + ".tmp.wav")
            if factor > 1.02:
                time_stretch(raw_path, tmp, factor)
            else:
                shutil.copyfile(raw_path, tmp)
            os.replace(tmp, st_path)

        clips.append((float(seg["start"]), st_path))
        if progress_cb:
            progress_cb(i + 1, len(items))

    last_end = max((float(s["end"]) for s in items), default=0.0)
    total = max(float(total_duration or 0.0), last_end + 0.5)
    output_path = work_dir / "dubbed_track.wav"
    concat_audio_with_silence(clips, total, output_path)
    return output_path


MIX_DEFAULTS = {"enabled": False, "orig_volume": 0.25, "dub_volume": 1.0, "duck": True, "duck_level": 0.3,
                "voices": {}}   # voices: {lang: "" | "xtts" | "edge:<ShortName>"}

# Ducking windows: how long before / after a spoken line the original audio is lowered, and lines
# closer than DUCK_MERGE_GAP are treated as one window (avoids "pumping" between sentences).
# The editor preview (app.js voiceRegions) uses the same numbers.
DUCK_PAD_BEFORE, DUCK_PAD_AFTER, DUCK_MERGE_GAP, DUCK_RAMP = 0.15, 0.25, 0.6, 0.12


def mix_settings(raw: dict | None) -> dict:
    out = dict(MIX_DEFAULTS)
    for k in MIX_DEFAULTS:
        if raw and k in raw:
            out[k] = raw[k]
    out["voices"] = dict(out.get("voices") or {})
    return out


def duck_regions(segments: list[dict]) -> list[list[float]]:
    regs: list[list[float]] = []
    for s in sorted(segments, key=lambda x: float(x["start"])):
        if not str(s.get("text", "")).strip():
            continue
        a = max(0.0, float(s["start"]) - DUCK_PAD_BEFORE)
        b = float(s["end"]) + DUCK_PAD_AFTER
        if regs and a - regs[-1][1] <= DUCK_MERGE_GAP:
            regs[-1][1] = max(regs[-1][1], b)
        else:
            regs.append([a, b])
    return regs


def write_duck_envelope(segments: list[dict], total_duration: float, level: float, out_path: Path,
                        sr: int = 8000) -> Path:
    """Mono 16-bit wav used as a gain curve: 1.0 normally, `level` while the dubbed voice speaks."""
    import wave
    import numpy as np

    level = min(max(float(level), 0.0), 1.0)
    regs = duck_regions(segments)
    total = max(float(total_duration or 0.0), regs[-1][1] if regs else 0.0) + 1.0

    xs, ys = [0.0], [1.0]
    def add(t, v):
        t = max(t, xs[-1] + 1e-4)
        xs.append(t)
        ys.append(v)
    for a, b in regs:
        add(a - DUCK_RAMP, 1.0)
        add(a, level)
        add(b, level)
        add(b + DUCK_RAMP, 1.0)
    add(total, 1.0)

    t = np.arange(int(total * sr), dtype=np.float64) / sr
    env = np.interp(t, xs, ys)
    pcm = np.clip(env * 32767.0, 0, 32767).astype("<i2")
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out_path


def encode_preview(wav_path: Path, out_path: Path) -> None:
    """Small AAC copy of the dubbed track for the in-browser preview."""
    run_ffmpeg(["-i", str(wav_path), "-vn", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(out_path)])

# ---------------------------------------------------------------------------
# Voice catalogue for the editor's combobox
# ---------------------------------------------------------------------------

_catalog = {"t": 0.0, "items": None}


def _edge_catalog() -> list[dict]:
    """All Microsoft Edge voices (needs internet). Cached for an hour."""
    if _catalog["items"] is not None and time.time() - _catalog["t"] < 3600:
        return _catalog["items"]
    import asyncio
    import edge_tts
    items = asyncio.run(edge_tts.list_voices())
    _catalog["items"], _catalog["t"] = items, time.time()
    return items


def list_voices(lang: str) -> dict:
    """Voices that can speak `lang`: {"voices": [{id, label, engine}], "online": bool}.
    id is "" (automatic), "xtts" or "edge:<ShortName>" - the same value the editor stores per language."""
    prefix = lang.strip().lower().split("-")[0]
    default = edge_voice(lang)
    voices = [{"id": "", "label": "Tự động (mặc định)", "engine": "auto"}]

    online = True
    try:
        catalog = _edge_catalog()
    except Exception:
        catalog, online = [], False

    edge_items = []
    for v in catalog:
        short = str(v.get("ShortName", ""))
        if not short or str(v.get("Locale", "")).lower().split("-")[0] != prefix:
            continue
        name = short.split("-", 2)[-1]
        if name.endswith("Neural"):
            name = name[:-6]
        label = f"{name} – {v.get('Gender', '')} – {v.get('Locale', '')}"
        if short == default:
            label += " ★ mặc định"
        edge_items.append({"id": f"edge:{short}", "label": label, "engine": "edge"})
    edge_items.sort(key=lambda x: (0 if "★" in x["label"] else 1, x["label"]))
    if not edge_items and default:            # offline: at least the built-in default voice
        edge_items.append({"id": f"edge:{default}", "label": f"{default} (mặc định, chưa tải được danh sách đầy đủ)", "engine": "edge"})
    voices += edge_items

    if _xtts_lang(lang) in XTTS_LANGS:
        voices.append({"id": "xtts", "label": "XTTS v2 – clone giọng người nói trong video (chạy trên máy, chậm)", "engine": "xtts"})
    return {"voices": voices, "online": online}