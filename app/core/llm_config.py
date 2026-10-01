import json
from dataclasses import dataclass, asdict, field
from typing import Optional
from app.config import LLM_CONFIG_PATH


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout: int = 60
    supports_json_mode: bool = True
    extra_headers: dict = field(default_factory=dict)
    extra_params: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def masked(self):
        d = self.to_dict()
        if d.get("api_key"):
            d["api_key"] = d["api_key"][:4] + "*" * max(0, len(d["api_key"]) - 8) + d["api_key"][-4:]
        return d


def save_config(cfg: LLMConfig) -> None:
    LLM_CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_config() -> LLMConfig:
    if LLM_CONFIG_PATH.exists():
        data = json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        return LLMConfig(**data)
    return LLMConfig()


def update_config(**kwargs) -> LLMConfig:
    cfg = load_config()
    for k, v in kwargs.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    save_config(cfg)
    return cfg
