import time
from openai import (APIConnectionError, APITimeoutError, AuthenticationError, NotFoundError,
                    PermissionDeniedError)
from app.core.llm_client import LLMClient, LLMFormatError
from app.core.asr import Segment

BATCH_SIZE = 8
TIMEOUT_RETRIES = 2
MAX_CONSECUTIVE_TIMEOUT_BATCHES = 3
FATAL_ERRORS = (APIConnectionError, AuthenticationError, PermissionDeniedError, NotFoundError)


def _fail(payload: list[dict], stats: dict) -> list[dict]:
    stats["failed"] = stats.get("failed", 0) + len(payload)
    stats.setdefault("failed_ids", set()).update(p["id"] for p in payload)
    return [{"id": p["id"], "text": p["text"]} for p in payload]


def _translate_with_retry(client: LLMClient, payload: list[dict], target_lang: str, context: str,
                          stats: dict) -> list[dict]:
    err = None
    for attempt in range(TIMEOUT_RETRIES + 1):
        try:
            return client.translate_segments(payload, target_lang, context=context)
        except Exception as e:
            err = e
            stats["error"] = (str(e) or e.__class__.__name__)[:300]
            if isinstance(e, APITimeoutError) and attempt < TIMEOUT_RETRIES:
                time.sleep(3 * (attempt + 1))
                continue
            break

    if isinstance(err, (APITimeoutError, LLMFormatError)):
        stats["timed_out"] = True
        return _fail(payload, stats)
    if isinstance(err, FATAL_ERRORS):
        stats["fatal"] = True
        return _fail(payload, stats)
    if len(payload) <= 1:
        return _fail(payload, stats)
    mid = len(payload) // 2
    left = _translate_with_retry(client, payload[:mid], target_lang, context, stats)
    if stats.get("fatal") or stats.get("timed_out"):
        return left + _fail(payload[mid:], stats)
    right = _translate_with_retry(client, payload[mid:], target_lang, context, stats)
    return left + right


def translate_all(client: LLMClient, segments: list[Segment], target_lang: str, context: str = "",
                  review: bool = True, stats: dict | None = None, progress_cb=None,
                  batch_cb=None) -> list[dict]:
    stats = stats if stats is not None else {}
    stats.setdefault("failed", 0)
    stats.setdefault("failed_ids", set())
    final: dict[int, str] = {}
    consecutive = 0

    for i in range(0, len(segments), BATCH_SIZE):
        batch = segments[i:i + BATCH_SIZE]
        payload = [{"id": s.id, "text": s.text} for s in batch]
        ids = {p["id"] for p in payload}

        if stats.get("fatal") or consecutive >= MAX_CONSECUTIVE_TIMEOUT_BATCHES:
            stats["fatal"] = True
            out = _fail(payload, stats)
        else:
            stats["timed_out"] = False
            out = _translate_with_retry(client, payload, target_lang, context, stats)
            batch_failed = ids & stats["failed_ids"]
            if stats.get("timed_out") and len(batch_failed) == len(ids):
                consecutive += 1
            else:
                consecutive = 0
            if review and not batch_failed and not stats.get("fatal"):
                try:
                    out = client.review_pass(out, target_lang)
                except Exception:
                    pass

        for t in out:
            final[t["id"]] = t["text"]
        if batch_cb:
            batch_cb([{"id": t["id"], "text": t["text"]} for t in out], ids & stats["failed_ids"])
        if progress_cb:
            progress_cb(min(i + BATCH_SIZE, len(segments)), len(segments))

    return [
        {"id": s.id, "start": s.start, "end": s.end, "text": final.get(s.id, s.text)}
        for s in segments
    ]