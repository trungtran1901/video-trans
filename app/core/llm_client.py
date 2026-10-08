import json
import re
import httpx
from openai import OpenAI, APITimeoutError
from app.core.llm_config import LLMConfig

JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
MIN_REQUEST_TIMEOUT = 180
LINE_RE = re.compile(r"^\s*\[?(\d+)\]?\s*[:.\-)\t|]\s*(.+?)\s*$")


class LLMFormatError(ValueError):
    pass


def _strip_fences(text) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.client = OpenAI(
            api_key=cfg.api_key or "not-required",
            base_url=cfg.base_url,
            timeout=max(float(cfg.timeout or 0), MIN_REQUEST_TIMEOUT),
            max_retries=0,
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
        content = _strip_fences(content)
        if not content:
            raise ValueError("LLM response is empty")
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

    def _chat_json_first(self, system_prompt: str, user_prompt: str) -> str:
        try:
            return self._chat(system_prompt, user_prompt, force_json=True)
        except APITimeoutError:
            raise
        except Exception:
            return self._chat(system_prompt, user_prompt, force_json=False)

    @staticmethod
    def _by_id_from_json(content: str, extract) -> dict:
        data = extract(content)
        result = data.get("segments", []) if isinstance(data, dict) else []
        return {int(s["id"]): s["text"] for s in result if "id" in s and "text" in s}

    @staticmethod
    def _by_id_from_lines(content: str) -> dict:
        found = {}
        for line in _strip_fences(content).splitlines():
            m = LINE_RE.match(line)
            if m:
                found[int(m.group(1))] = m.group(2)
        return found

    def translate_segments(self, segments: list[dict], target_lang: str, context: str = "") -> list[dict]:
        base = (
            f"You are a professional native-level subtitle translator. Translate the following dialogue "
            f"segments into {target_lang}. Each segment is a separate subtitle line tied to a specific "
            f"moment in the video, so translate EACH segment independently and keep its meaning strictly "
            f"within that segment only. Do NOT merge content from one segment into another, do NOT move "
            f"words across segment boundaries, and do NOT reorder segments. Use natural, everyday spoken "
            f"language as native speakers would say it, not literal word-for-word translation, but stay "
            f"faithful to what is said in that exact segment. Keep the exact same number of segments and "
            f"the same ids. Context/topic of the video: {context or 'general'}. "
        )
        json_prompt = base + (
            'Respond ONLY with strict JSON in this exact shape: '
            '{"segments": [{"id": <int>, "text": "<translated text>"}]}'
        )
        line_prompt = base + (
            'Respond ONLY with one line per segment, in the form: <id>: <translated text>. '
            'No JSON, no markdown, no explanations.'
        )
        json_user = json.dumps({"segments": segments}, ensure_ascii=False)
        line_user = "\n".join(f"{s['id']}: {s['text']}" for s in segments)

        by_id: dict = {}
        last = ""
        for force_json in (True, False):
            try:
                last = self._chat(json_prompt, json_user, force_json=force_json)
                by_id = self._by_id_from_json(last, self._extract_json)
            except APITimeoutError:
                raise
            except Exception:
                by_id = {}
            if by_id:
                break
        if not by_id:
            try:
                last = self._chat(line_prompt, line_user, force_json=False)
            except APITimeoutError:
                raise
            except Exception as e:
                raise LLMFormatError(f"LLM request failed: {e}")
            by_id = self._by_id_from_lines(last)
        if not by_id:
            raise LLMFormatError(f"LLM response is not usable: {_strip_fences(last)[:200]!r}")

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
        content = self._chat_json_first(system_prompt, user_prompt)
        data = self._extract_json(content)
        result = data.get("segments", [])
        by_id = {int(s["id"]): s["text"] for s in result if "id" in s and "text" in s}
        output = []
        for seg in segments:
            sid = int(seg["id"])
            output.append({"id": sid, "text": by_id.get(sid, seg["text"])})
        return output