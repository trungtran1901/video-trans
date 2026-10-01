import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("APP_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
CONFIG_DIR = DATA_DIR / "config"
JOBS_DB = DATA_DIR / "jobs.json"

for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

LLM_CONFIG_PATH = CONFIG_DIR / "llm_config.json"

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "medium")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

TTS_MODEL_NAME = os.getenv("TTS_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2")

MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "2048"))

# Export quality (libx264). CRF 14 + preset slow is visually lossless for almost any content.
# Lower CRF = better quality and bigger file (0 = lossless, 18 = "good", 23 = ffmpeg default).
EXPORT_CRF = min(max(int(os.getenv("EXPORT_CRF", "14")), 0), 30)
EXPORT_PRESET = os.getenv("EXPORT_PRESET", "slow")   # ultrafast ... veryslow
EXPORT_AUDIO_BITRATE = os.getenv("EXPORT_AUDIO_BITRATE", "256k")