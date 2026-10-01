from app.core.llm_client import LLMClient
from app.core.asr import Segment

BATCH_SIZE = 12


def _translate_with_retry(client: LLMClient, payload: list[dict], target_lang: str, context: str) -> list[dict]:
    try:
        return client.translate_segments(payload, target_lang, context=context)
    except Exception:
        if len(payload) <= 1:
            return [{"id": p["id"], "text": p["text"]} for p in payload]
        mid = len(payload) // 2
        left = _translate_with_retry(client, payload[:mid], target_lang, context)
        right = _translate_with_retry(client, payload[mid:], target_lang, context)
        return left + right


def translate_all(client: LLMClient, segments: list[Segment], target_lang: str, context: str = "", review: bool = True) -> list[dict]:
    translated: list[dict] = []
    for i in range(0, len(segments), BATCH_SIZE):
        batch = segments[i:i + BATCH_SIZE]
        payload = [{"id": s.id, "text": s.text} for s in batch]
        result = _translate_with_retry(client, payload, target_lang, context)
        translated.extend(result)

    if review and len(translated) > 0:
        reviewed = []
        for i in range(0, len(translated), BATCH_SIZE):
            batch = translated[i:i + BATCH_SIZE]
            try:
                reviewed.extend(client.review_pass(batch, target_lang))
            except Exception:
                reviewed.extend(batch)
        translated = reviewed

    by_id = {t["id"]: t["text"] for t in translated}
    output = []
    for s in segments:
        output.append({
            "id": s.id,
            "start": s.start,
            "end": s.end,
            "text": by_id.get(s.id, s.text),
        })
    return output
