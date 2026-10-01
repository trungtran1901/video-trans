from dataclasses import dataclass
from pathlib import Path
from faster_whisper import WhisperModel
from app.config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE

_model = None

SENTENCE_END_CHARS = set(".!?…")
SOFT_BREAK_CHARS = set(",;:")
MAX_SEGMENT_DURATION = 5.0
SOFT_MAX_DURATION = 3.5
MAX_SEGMENT_CHARS = 80
SOFT_MAX_CHARS = 50
MAX_GAP_SECONDS = 0.5


@dataclass
class Segment:
    id: int
    start: float
    end: float
    text: str


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
    return _model


def _collect_words(raw_segments) -> list[dict]:
    words = []
    for seg in raw_segments:
        if not seg.words:
            text = seg.text.strip()
            if text:
                words.append({"start": seg.start, "end": seg.end, "word": text})
            continue
        for w in seg.words:
            token = w.word.strip()
            if token:
                words.append({"start": w.start, "end": w.end, "word": token})
    return words


def _resegment(words: list[dict]) -> list[Segment]:
    segments: list[Segment] = []
    current_words: list[dict] = []
    seg_id = 0

    def flush():
        nonlocal current_words, seg_id
        if not current_words:
            return
        text = " ".join(w["word"] for w in current_words).strip()
        text = text.replace(" ,", ",").replace(" .", ".").replace(" ?", "?").replace(" !", "!")
        if text:
            segments.append(Segment(
                id=seg_id,
                start=current_words[0]["start"],
                end=current_words[-1]["end"],
                text=text,
            ))
            seg_id += 1
        current_words = []

    prev_end = None
    for w in words:
        if prev_end is not None and (w["start"] - prev_end) > MAX_GAP_SECONDS and current_words:
            flush()

        current_words.append(w)
        prev_end = w["end"]

        duration = current_words[-1]["end"] - current_words[0]["start"]
        char_count = sum(len(x["word"]) for x in current_words)
        last_char = w["word"][-1:]
        ends_sentence = last_char in SENTENCE_END_CHARS
        ends_soft = last_char in SOFT_BREAK_CHARS

        hard_limit_hit = duration >= MAX_SEGMENT_DURATION or char_count >= MAX_SEGMENT_CHARS
        soft_limit_hit = (duration >= SOFT_MAX_DURATION or char_count >= SOFT_MAX_CHARS) and ends_soft

        if ends_sentence or hard_limit_hit or soft_limit_hit:
            flush()

    flush()
    return segments


def transcribe(audio_path: Path, language: str | None = None) -> tuple[list[Segment], str]:
    model = get_model()
    raw_segments, info = model.transcribe(
        str(audio_path),
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        word_timestamps=True,
    )
    words = _collect_words(list(raw_segments))
    if not words:
        return [], info.language
    segments = _resegment(words)
    return segments, info.language
