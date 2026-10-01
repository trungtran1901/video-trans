import os
import sys

os.environ.setdefault("COQUI_TOS_AGREED", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def resolve_paths():
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:
        HF_HUB_CACHE = os.path.expanduser("~/.cache/huggingface")

    try:
        from TTS.utils.generic_utils import get_user_data_dir
        tts_dir = get_user_data_dir("tts")
    except Exception:
        tts_dir = os.path.expanduser("~/.local/share/tts")

    return HF_HUB_CACHE, tts_dir


if "--show-paths" in sys.argv:
    hf_dir, tts_dir = resolve_paths()
    print("Hugging Face cache dir (dung cho HF_CACHE_DIR):")
    print(" ", hf_dir)
    print("TTS model dir (dung cho TTS_CACHE_DIR):")
    print(" ", tts_dir)
    sys.exit(0)

from faster_whisper import WhisperModel
from TTS.api import TTS

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
TTS_MODEL_NAME = os.getenv("TTS_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2")

hf_dir, tts_dir = resolve_paths()
print("Se tai model vao:")
print("  Hugging Face cache:", hf_dir)
print("  TTS model dir:", tts_dir)
print()

print(f"Downloading faster-whisper model: {WHISPER_MODEL_SIZE}")
WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
print("faster-whisper model ready.")

print(f"Downloading TTS model: {TTS_MODEL_NAME}")
_orig_load = None
try:
    import torch
    _orig_load = torch.load

    def _patched_load(*a, **kw):
        if kw.get("weights_only") is None:  # PyTorch >= 2.6 rejects the Coqui XTTS checkpoint otherwise
            kw["weights_only"] = False
        return _orig_load(*a, **kw)

    torch.load = _patched_load
except Exception:
    pass
TTS(TTS_MODEL_NAME)
if _orig_load is not None:
    torch.load = _orig_load
print("TTS model ready.")

print()
print("Xong. De docker-compose dung lai model nay, dat trong file .env o thu muc goc du an:")
print(f'  HF_CACHE_DIR={hf_dir}')
print(f'  TTS_CACHE_DIR={tts_dir}')