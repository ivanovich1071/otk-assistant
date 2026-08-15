"""Клиент OpenRouter для чтения чертежей vision-моделью.

Три вещи, без которых отладка промптов разорительна и мучительна:
кэш ответов по хешу запроса, учёт стоимости и повтор при сбое сети.

Ключ ищется по очереди: переменная окружения OPENROUTER_API_KEY,
файл .env в корне проекта, файл, указанный в OPENROUTER_ENV или параметром --env.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "work" / "llm_cache"

BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Одна модель на весь проект. Дорогая max не используется: на перепроверке
# сомнительных позиций она вчетверо поднимала стоимость чертежа, ничего
# принципиально не добавляя — сомнительное всё равно смотрит человек.
MODEL_MAIN = os.environ.get("OPENROUTER_MODEL", "").strip() or "qwen/qwen3.7-plus"

# Цены OpenRouter, $ за миллион токенов. Нужны только для отчёта пользователю,
# поэтому расхождение с реальным счётом на копейки некритично.
PRICES = {
    "qwen/qwen3.7-plus": (0.32, 1.28),
    "qwen/qwen3.7-flash": (0.03, 0.13),
}


class LLMError(RuntimeError):
    pass


def find_api_key(env_file: Path | None = None) -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"].strip()

    candidates = [env_file] if env_file else []
    if os.environ.get("OPENROUTER_ENV"):
        candidates.append(Path(os.environ["OPENROUTER_ENV"]))
    candidates.append(ROOT / ".env")

    for path in candidates:
        if path and path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY"):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
    raise LLMError(
        "Не найден ключ OpenRouter. Задайте OPENROUTER_API_KEY, положите его в .env "
        "проекта или укажите файл через --env / OPENROUTER_ENV."
    )


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, prompt: int, completion: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.by_model[model] = self.by_model.get(model, 0) + 1
        pin, pout = PRICES.get(model, (0.0, 0.0))
        self.cost_usd += prompt / 1e6 * pin + completion / 1e6 * pout

    def summary(self) -> str:
        return (f"запросов {self.calls} (из кэша {self.cached}), "
                f"токенов {self.prompt_tokens}+{self.completion_tokens}, "
                f"стоимость ${self.cost_usd:.4f}")


class LLM:
    def __init__(self, model: str = MODEL_MAIN, env_file: Path | None = None,
                 use_cache: bool = True, timeout: int = 180) -> None:
        self.model = model
        self.api_key = find_api_key(env_file)
        self.use_cache = use_cache
        self.timeout = timeout
        self.usage = Usage()

    def _cache_path(self, key: str) -> Path:
        return CACHE_DIR / f"{key}.json"

    def ask(self, system: str, user: str, images: list[Path] | None = None,
            model: str | None = None, temperature: float = 0.0,
            max_tokens: int = 8000) -> str:
        model = model or self.model
        images = images or []

        parts: list[dict] = [{"type": "text", "text": user}]
        for img in images:
            data = base64.b64encode(img.read_bytes()).decode()
            suffix = img.suffix.lstrip(".").lower().replace("jpg", "jpeg")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{suffix};base64,{data}"},
            })

        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }

        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:32]
        cache_file = self._cache_path(key)
        if self.use_cache and cache_file.exists():
            self.usage.cached += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))["text"]

        text, prompt_tok, completion_tok = self._post(payload)
        self.usage.add(model, prompt_tok, completion_tok)

        if self.use_cache:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"model": model, "text": text}, ensure_ascii=False),
                encoding="utf-8")
        return text

    def _post(self, payload: dict) -> tuple[str, int, int]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            BASE_URL, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Title": "Karty obmera",
            },
        )

        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    last = LLMError(f"HTTP {e.code}: {detail}")
                    time.sleep(2 ** attempt * 3)
                    continue
                raise LLMError(f"OpenRouter HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise LLMError(f"Сеть недоступна: {e}") from e
        else:
            raise LLMError(f"Не удалось получить ответ: {last}")

        if "choices" not in data:
            raise LLMError(f"Неожиданный ответ OpenRouter: {str(data)[:300]}")
        usage = data.get("usage") or {}
        return (data["choices"][0]["message"]["content"] or "",
                int(usage.get("prompt_tokens", 0)),
                int(usage.get("completion_tokens", 0)))


def parse_json(text: str) -> object:
    """Модель любит обрамлять JSON пояснениями и ```-заборами — вынимаем ядро."""
    cleaned = text.strip()
    if "```" in cleaned:
        chunks = cleaned.split("```")
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith(("{", "[")):
                cleaned = chunk
                break
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError(f"В ответе нет JSON: {text[:200]}")
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    return json.loads(cleaned[start : end + 1])
