import importlib.util
import json
import os
import re
import threading
from pathlib import Path

import numpy as np

REPO_ID = os.getenv("KOKORO_REPO_ID", "contextboxai/Kokoro-Vietnamese")
DEVICE = os.getenv("KOKORO_DEVICE", "cpu").strip().lower()
DEFAULT_VOICE = os.getenv("KOKORO_DEFAULT_VOICE", "diem_trinh").strip()
AUTO_FOR_VI = os.getenv("KOKORO_AUTO", "1").strip().lower() not in ("0", "false", "no")
SPEED = float(os.getenv("KOKORO_SPEED", "1.0"))
ONNX_FILE = "kokoro_vi.onnx"
CONFIG_FILE = "config.json"
SAMPLE_RATE = 24000
CROSSFADE_MS = 50
MAX_PHONEMES = 500

VOICES = {
    "diem_trinh": "Diễm Trinh",
    "hung_thinh": "Hưng Thịnh",
    "mai_linh": "Mai Linh",
    "mai_loan": "Mai Loan",
    "manh_dung": "Mạnh Dũng",
    "my_yen": "Mỹ Yến",
    "ngoc_huyen": "Ngọc Huyền",
    "phat_tai": "Phát Tài",
    "thanh_dat": "Thành Đạt",
    "thuc_trinh": "Thục Trinh",
    "tuan_ngoc": "Tuấn Ngọc",
    "storyvert": "Storyvert",
    "duc_an": "Đức An",
    "duc_duy": "Đức Duy",
}

INSTALL_HINT = "Chưa cài Kokoro Vietnamese. Chạy: pip install onnxruntime vig2p soundfile huggingface_hub"

_lock = threading.RLock()
_state = {"session": None, "config": None}
_packs: dict[str, np.ndarray] = {}


def is_installed() -> bool:
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("onnxruntime", "vig2p", "soundfile", "huggingface_hub", "torch")
    )


def _fetch(filename: str) -> Path:
    from huggingface_hub import hf_hub_download
    return Path(hf_hub_download(repo_id=REPO_ID, filename=filename))


def _providers() -> list[str]:
    import onnxruntime as ort
    if DEVICE == "cuda" and "CUDAExecutionProvider" in ort.get_available_providers():
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _load():
    if _state["session"] is None:
        import onnxruntime as ort
        with open(_fetch(CONFIG_FILE), "r", encoding="utf-8") as f:
            config = json.load(f)
        _state["config"] = config
        _state["session"] = ort.InferenceSession(str(_fetch(ONNX_FILE)), providers=_providers())
    return _state["session"], _state["config"]


def _pack(voice: str) -> np.ndarray:
    if voice not in VOICES:
        raise RuntimeError(f"Giọng Kokoro '{voice}' không tồn tại. Các giọng có sẵn: {', '.join(sorted(VOICES))}")
    if voice not in _packs:
        import torch
        raw = torch.load(_fetch(f"voicepacks/{voice}.pt"), map_location="cpu", weights_only=True)
        if hasattr(raw, "detach"):
            raw = raw.detach().cpu().numpy()
        _packs[voice] = np.asarray(raw, dtype=np.float32)
    return _packs[voice]


def split_text(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    for match in re.finditer(r"[.!?…]+(?:[\"”’)]*)", normalized):
        end = match.end()
        if end < len(normalized) and not normalized[end].isspace():
            continue
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    rest = normalized[start:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def _phoneme_chunks(text: str) -> list[str]:
    from vig2p import phonemize_text

    out: list[str] = []

    def add(piece: str) -> None:
        piece = piece.strip()
        if not piece:
            return
        ps = phonemize_text(piece)
        if not ps:
            return
        if len(ps) <= MAX_PHONEMES:
            out.append(ps)
            return
        parts = re.split(r"(?<=[,;:])\s+", piece)
        if len(parts) > 1:
            mid = len(parts) // 2
            halves = [" ".join(parts[:mid]), " ".join(parts[mid:])]
        else:
            words = piece.split(" ")
            if len(words) < 2:
                out.append(ps[:MAX_PHONEMES])
                return
            mid = len(words) // 2
            halves = [" ".join(words[:mid]), " ".join(words[mid:])]
        for h in halves:
            add(h)

    for sentence in split_text(text):
        add(sentence)
    return out


def _merge(chunks: list[np.ndarray], crossfade: int) -> np.ndarray:
    valid = [np.asarray(c, dtype=np.float32) for c in chunks if len(c) > 0]
    if not valid:
        return np.array([], dtype=np.float32)
    merged = valid[0]
    for chunk in valid[1:]:
        overlap = min(int(crossfade), len(merged), len(chunk))
        if overlap <= 0:
            merged = np.concatenate([merged, chunk])
            continue
        fade_out = np.linspace(1.0, 0.0, overlap + 2, dtype=np.float32)[1:-1]
        fade_in = 1.0 - fade_out
        mixed = merged[-overlap:] * fade_out + chunk[:overlap] * fade_in
        merged = np.concatenate([merged[:-overlap], mixed, chunk[overlap:]])
    return merged.astype(np.float32, copy=False)


def synthesize(text: str, voice: str | None = None, speed: float | None = None) -> np.ndarray:
    voice = (voice or DEFAULT_VOICE).strip()
    speed_value = np.asarray(float(speed if speed else SPEED), dtype=np.float32)
    waves: list[np.ndarray] = []
    with _lock:
        session, config = _load()
        pack = _pack(voice)
        vocab = config["vocab"]
        limit = int(config["plbert"]["max_position_embeddings"])
        for ps in _phoneme_chunks(text):
            ids = [vocab[p] for p in ps if p in vocab]
            if not ids or len(ids) + 2 > limit:
                continue
            input_ids = np.asarray([[0, *ids, 0]], dtype=np.int64)
            ref_s = pack[min(len(ps), pack.shape[0]) - 1]
            wave, _ = session.run(None, {"input_ids": input_ids, "ref_s": ref_s, "speed": speed_value})
            waves.append(np.asarray(wave, dtype=np.float32).reshape(-1))
    return _merge(waves, round(SAMPLE_RATE * CROSSFADE_MS / 1000))


def synthesize_to_file(text: str, voice: str | None, output_path: Path, speed: float | None = None) -> None:
    import soundfile as sf
    audio = synthesize(text, voice, speed)
    if len(audio) == 0:
        audio = np.zeros(int(SAMPLE_RATE * 0.4), dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    sf.write(str(output_path), audio, SAMPLE_RATE, subtype="PCM_16")


def prefetch(voices: list[str] | None = None) -> None:
    _fetch(CONFIG_FILE)
    _fetch(ONNX_FILE)
    for v in voices or sorted(VOICES):
        _fetch(f"voicepacks/{v}.pt")