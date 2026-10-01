import json
import re
import httpx
from openai import OpenAI
from app.core.llm_config import LLMConfig

JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.client = OpenAI(
            api_key=cfg.api_key or "not-required",
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            default_headers=cfg.extra_headers or {},
        )

    def list_models(self) -> list[str]:
        try:
            models = self.client.models.list()
            ids = sorted(m.id for m in models.data)
            if ids:
                return ids
        except Exception:
            pass
        return self._list_models_raw()

    def _list_models_raw(self) -> list[str]:
        url = self.cfg.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        headers.update(self.cfg.extra_headers or {})
        resp = httpx.get(url, headers=headers, timeout=self.cfg.timeout)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data if isinstance(data, list) else [])
        ids = []
        for item in items:
            if isinstance(item, dict) and "id" in item:
                ids.append(item["id"])
            elif isinstance(item, str):
                ids.append(item)
        if not ids:
            raise ValueError("No models returned from " + url + ": " + resp.text[:500])
        return sorted(ids)

    def test_connection(self) -> tuple[bool, str]:
        try:
            self.client.chat.completions.create(
                model=self.cfg.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            return True, "ok"
        except Exception as e:
            return False, str(e)

    def _extract_json(self, content: str) -> dict:
        try:
            return json.loads(content)
        except Exception:
            pass
        match = JSON_BLOCK_RE.search(content)
        candidate = match.group(0) if match else content
        try:
            return json.loads(candidate)
        except Exception:
            pass
        repaired = re.sub(r",\s*([\]}])", r"\1", candidate)
        repaired = repaired.replace("\n", " ")
        try:
            return json.loads(repaired)
        except Exception:
            pass
        last_complete = repaired.rfind('},')
        if last_complete != -1:
            truncated = repaired[:last_complete + 1]
            opens = truncated.count("[")
            closes = truncated.count("]")
            truncated = truncated + ("]" * (opens - closes)) + "}"
            try:
                return json.loads(truncated)
            except Exception:
                pass
        raise ValueError("LLM response is not valid JSON: " + content[:800])

    def _chat(self, system_prompt: str, user_prompt: str, force_json: bool) -> str:
        kwargs = dict(
            model=self.cfg.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
        )
        kwargs.update(self.cfg.extra_params or {})
        if force_json and self.cfg.supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    def translate_segments(self, segments: list[dict], target_lang: str, context: str = "") -> list[dict]:
        system_prompt = (
            f"You are a professional native-level subtitle translator. Translate the following dialogue "
            f"segments into {target_lang}. Each segment is a separate subtitle line tied to a specific "
            f"moment in the video, so translate EACH segment independently and keep its meaning strictly "
            f"within that segment only. Do NOT merge content from one segment into another, do NOT move "
            f"words across segment boundaries, and do NOT reorder segments. Use natural, everyday spoken "
            f"language as native speakers would say it, not literal word-for-word translation, but stay "
            f"faithful to what is said in that exact segment. Keep the exact same number of segments and "
            f"the same ids. Context/topic of the video: {context or 'general'}. "
            'Respond ONLY with strict JSON in this exact shape: '
            '{"segments": [{"id": <int>, "text": "<translated text>"}]}'
        )
        user_prompt = json.dumps({"segments": segments}, ensure_ascii=False)
        try:
            content = self._chat(system_prompt, user_prompt, force_json=True)
        except Exception:
            content = self._chat(system_prompt, user_prompt, force_json=False)
        data = self._extract_json(content)
        result = data.get("segments", [])
        by_id = {int(s["id"]): s["text"] for s in result if "id" in s and "text" in s}
        output = []
        for seg in segments:
            sid = int(seg["id"])
            output.append({"id": sid, "text": by_id.get(sid, seg["text"])})
        return output

    def review_pass(self, segments: list[dict], target_lang: str) -> list[dict]:
        system_prompt = (
            f"You are a senior editor reviewing a {target_lang} subtitle translation for naturalness "
            f"and consistency across the whole conversation. Rewrite any segment that still sounds "
            f"stiff, literal, or inconsistent in tone with the rest. Each segment is tied to a specific "
            f"moment in the video, so do NOT move content across segment ids and do NOT change the "
            f"number or order of segments. Keep the same ids and count. "
            'Respond ONLY with strict JSON: {"segments": [{"id": <int>, "text": "<text>"}]}'
        )
        user_prompt = json.dumps({"segments": segments}, ensure_ascii=False)
        try:
            content = self._chat(system_prompt, user_prompt, force_json=True)
        except Exception:
            content = self._chat(system_prompt, user_prompt, force_json=False)
        data = self._extract_json(content)
        result = data.get("segments", [])
        by_id = {int(s["id"]): s["text"] for s in result if "id" in s and "text" in s}
        output = []
        for seg in segments:
            sid = int(seg["id"])
            output.append({"id": sid, "text": by_id.get(sid, seg["text"])})
        return output
