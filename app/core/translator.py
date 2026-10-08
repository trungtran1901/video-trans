from openai import (APIConnectionError, AuthenticationError, NotFoundError,
                    PermissionDeniedError)
from app.core.llm_client import LLMClient
from app.core.asr import Segment

BATCH_SIZE = 12
# Lỗi kết nối / xác thực: chia nhỏ batch cũng vô ích, dừng sớm cho nhanh
FATAL_ERRORS = (APIConnectionError, AuthenticationError, PermissionDeniedError, NotFoundError)


def _fallback(payload: list[dict]) -> list[dict]:
    return [{"id": p["id"], "text": p["text"]} for p in payload]


def _translate_with_retry(client: LLMClient, payload: list[dict], target_lang: str, context: str,
                          stats: dict) -> list[dict]:
    try:
        return client.translate_segments(payload, target_lang, context=context)
    except Exception as e:
        stats["error"] = str(e)[:300]
        if isinstance(e, FATAL_ERRORS):
            stats["fatal"] = True
            stats["failed"] = stats.get("failed", 0) + len(payload)
            return _fallback(payload)
        if len(payload) <= 1:
            stats["failed"] = stats.get("failed", 0) + len(payload)
            return _fallback(payload)
        mid = len(payload) // 2
        left = _translate_with_retry(client, payload[:mid], target_lang, context, stats)
        if stats.get("fatal"):
            stats["failed"] = stats.get("failed", 0) + len(payload[mid:])
            return left + _fallback(payload[mid:])
        right = _translate_with_retry(client, payload[mid:], target_lang, context, stats)
        return left + right


def translate_all(client: LLMClient, segments: list[Segment], target_lang: str, context: str = "",
                  review: bool = True, stats: dict | None = None) -> list[dict]:
    """stats (tuỳ chọn) được điền: failed = số đoạn chưa dịch được, error = lỗi cuối, fatal = mất kết nối."""
    stats = stats if stats is not None else {}
    stats.setdefault("failed", 0)
    translated: list[dict] = []
    for i in range(0, len(segments), BATCH_SIZE):
        batch = segments[i:i + BATCH_SIZE]
        payload = [{"id": s.id, "text": s.text} for s in batch]
        if stats.get("fatal"):
            stats["failed"] += len(payload)
            translated.extend(_fallback(payload))
            continue
        translated.extend(_translate_with_retry(client, payload, target_lang, context, stats))

    if review and translated and not stats.get("fatal"):
        reviewed = []
        for i in range(0, len(translated), BATCH_SIZE):
            batch = translated[i:i + BATCH_SIZE]
            try:
                reviewed.extend(client.review_pass(batch, target_lang))
            except Exception:
                reviewed.extend(batch)
        translated = reviewed

    by_id = {t["id"]: t["text"] for t in translated}
    return [
        {"id": s.id, "start": s.start, "end": s.end, "text": by_id.get(s.id, s.text)}
        for s in segments
    ]