from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, List, Optional


ZHIPU_CHAT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_EMBEDDING_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
DEFAULT_GLM_MODEL = "glm-5"
DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_HASH_DIMENSION = 256
DEFAULT_QWEN_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
DEFAULT_QWEN_QUERY_INSTRUCTION = (
    "Given a memory-style query about a Gcores radio episode fragment, "
    "retrieve relevant podcast transcript passages, scene summaries, and timeline notes."
)

TOKEN_RE = re.compile(r"[A-Za-z0-9_:+./-]+|[\u4e00-\u9fff]")


class EmbeddingProvider:
    name = "base"

    def embed(self, texts: List[str]) -> List[List[float]]:
        return self.embed_documents(texts)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        raise NotImplementedError

    def embed_queries(self, texts: List[str]) -> List[List[float]]:
        return self.embed_documents(texts)


@dataclass
class HashEmbeddingProvider(EmbeddingProvider):
    dimension: int = DEFAULT_HASH_DIMENSION
    name: str = "hash"

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [hash_embed(text, dimension=self.dimension) for text in texts]


@dataclass
class ZhipuEmbeddingProvider(EmbeddingProvider):
    api_key: str
    model: str = DEFAULT_EMBEDDING_MODEL
    dimension: Optional[int] = None
    batch_size: int = 16
    timeout_seconds: float = 60.0
    request_interval_seconds: float = 0.2
    _last_request_monotonic: float = field(default=0.0, init=False, repr=False)
    name: str = "zhipu"

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors: List[List[float]] = []
        for batch in chunked(texts, self.batch_size):
            sleep_for_rate_limit(self)
            body: dict = {
                "model": self.model,
                "input": batch,
            }
            if self.dimension:
                body["dimensions"] = int(self.dimension)
            payload = http_post_json(
                ZHIPU_EMBEDDING_URL,
                body=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout_seconds=self.timeout_seconds,
            )
            data = payload.get("data", [])
            ordered = sorted(data, key=lambda row: int(row.get("index", 0)))
            vectors.extend([list(row.get("embedding") or []) for row in ordered])
        return vectors


@dataclass
class Qwen3EmbeddingProvider(EmbeddingProvider):
    model: str = DEFAULT_QWEN_EMBEDDING_MODEL
    dimension: Optional[int] = None
    batch_size: int = 8
    max_length: int = 8192
    device: str = "auto"
    query_instruction: str = DEFAULT_QWEN_QUERY_INSTRUCTION
    name: str = "qwen3"
    _tokenizer: object = field(default=None, init=False, repr=False)
    _model: object = field(default=None, init=False, repr=False)
    _torch: object = field(default=None, init=False, repr=False)
    _resolved_device: str = field(default="", init=False, repr=False)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._embed_texts(texts, is_query=False)

    def embed_queries(self, texts: List[str]) -> List[List[float]]:
        return self._embed_texts(texts, is_query=True)

    def _embed_texts(self, texts: List[str], *, is_query: bool) -> List[List[float]]:
        if not texts:
            return []
        tokenizer, model, torch_module, resolved_device = self._ensure_loaded()
        input_texts = [self._format_query_text(text) if is_query else (text or "") for text in texts]
        vectors: List[List[float]] = []
        with torch_module.no_grad():
            for batch in chunked(input_texts, self.batch_size):
                encoded = tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {key: value.to(resolved_device) for key, value in encoded.items()}
                outputs = model(**encoded)
                embeddings = last_token_pool(
                    last_hidden_states=outputs.last_hidden_state,
                    attention_mask=encoded["attention_mask"],
                    torch_module=torch_module,
                )
                if self.dimension and 0 < int(self.dimension) < int(embeddings.shape[1]):
                    embeddings = embeddings[:, : int(self.dimension)]
                embeddings = torch_module.nn.functional.normalize(embeddings, p=2, dim=1)
                vectors.extend(embeddings.detach().cpu().tolist())
        return vectors

    def _ensure_loaded(self) -> tuple[object, object, object, str]:
        if self._tokenizer is not None and self._model is not None and self._torch is not None:
            return self._tokenizer, self._model, self._torch, self._resolved_device
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Qwen3 embedding provider requires torch and transformers installed in the active Python environment"
            ) from exc
        resolved_device = self._resolve_device(torch)
        tokenizer = AutoTokenizer.from_pretrained(
            self.model,
            trust_remote_code=True,
            padding_side="left",
        )
        model_kwargs = {"trust_remote_code": True}
        if resolved_device.startswith("cuda"):
            model_kwargs["torch_dtype"] = torch.float16
            model_kwargs["low_cpu_mem_usage"] = True
            if self._should_use_device_map_auto():
                model_kwargs["device_map"] = "auto"
        model = AutoModel.from_pretrained(self.model, **model_kwargs)
        model.eval()
        if not model_kwargs.get("device_map"):
            model.to(resolved_device)
        input_device = self._infer_input_device(model=model, fallback=resolved_device)
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch
        self._resolved_device = input_device
        return tokenizer, model, torch, input_device

    def _resolve_device(self, torch_module: object) -> str:
        configured = (self.device or "auto").strip().lower()
        if configured != "auto":
            return configured
        if torch_module.cuda.is_available():
            return "cuda"
        return "cpu"

    def _format_query_text(self, text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return cleaned
        return f"Instruct: {self.query_instruction}\nQuery: {cleaned}"

    def _should_use_device_map_auto(self) -> bool:
        model_name = (self.model or "").lower()
        return "8b" in model_name

    def _infer_input_device(self, *, model: object, fallback: str) -> str:
        try:
            for parameter in model.parameters():
                device = str(getattr(parameter, "device", ""))
                if device and device != "meta":
                    return device
        except Exception:
            pass
        return fallback


@dataclass
class ZhipuGLMClient:
    api_key: str
    model: str = DEFAULT_GLM_MODEL
    timeout_seconds: float = 90.0
    request_interval_seconds: float = 1.2
    _last_request_monotonic: float = field(default=0.0, init=False, repr=False)

    def json_chat(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> dict:
        sleep_for_rate_limit(self)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # Official structured-output mode is far more reliable than plain chat
            # when we need machine-parseable search enrichment JSON.
            "response_format": {"type": "json_object"},
        }
        if self.model.startswith("glm-5") or self.model.startswith("glm-4.7"):
            # GLM-5 / GLM-4.7 enable thinking by default. Disable it for
            # extraction jobs so the final JSON is emitted in content instead of
            # reasoning traces.
            body["thinking"] = {"type": "disabled"}
        payload = http_post_json(
            ZHIPU_CHAT_URL,
            body=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout_seconds=self.timeout_seconds,
        )
        content = extract_message_content(payload)
        return parse_json_object(content)


def create_embedding_provider(
    *,
    provider_name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
    batch_size: Optional[int] = None,
) -> EmbeddingProvider:
    lowered = provider_name.strip().lower()
    if lowered == "hash":
        return HashEmbeddingProvider(dimension=dimension or DEFAULT_HASH_DIMENSION)
    if lowered in {"qwen3", "qwen", "qwen3-local"}:
        return Qwen3EmbeddingProvider(
            model=model or DEFAULT_QWEN_EMBEDDING_MODEL,
            dimension=dimension,
            batch_size=max(1, int(batch_size or 8)),
        )
    if lowered != "zhipu":
        raise ValueError(f"Unsupported embedding provider: {provider_name}")
    resolved_key = api_key or os.environ.get("ZHIPU_API_KEY")
    if not resolved_key:
        raise ValueError("Missing ZHIPU_API_KEY for zhipu embedding provider")
    return ZhipuEmbeddingProvider(
        api_key=resolved_key,
        model=model or DEFAULT_EMBEDDING_MODEL,
        dimension=dimension,
        batch_size=max(1, int(batch_size or 16)),
    )


def create_glm_client(
    *,
    provider_name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[ZhipuGLMClient]:
    lowered = provider_name.strip().lower()
    if lowered in {"none", "disabled", "fallback", "rule"}:
        return None
    if lowered != "zhipu":
        raise ValueError(f"Unsupported GLM provider: {provider_name}")
    resolved_key = api_key or os.environ.get("ZHIPU_API_KEY")
    if not resolved_key:
        return None
    return ZhipuGLMClient(api_key=resolved_key, model=model or DEFAULT_GLM_MODEL)


def hash_embed(text: str, *, dimension: int) -> List[float]:
    vector = [0.0] * max(8, int(dimension))
    tokens = TOKEN_RE.findall(text or "")
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % len(vector)
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + (digest[5] / 255.0)
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def last_token_pool(*, last_hidden_states, attention_mask, torch_module) -> object:
    left_padding = bool(torch_module.all(attention_mask[:, -1] == 1))
    if left_padding:
        return last_hidden_states[:, -1]
    sequence_lengths = attention_mask.sum(dim=1) - 1
    batch_size = last_hidden_states.shape[0]
    row_indexes = torch_module.arange(batch_size, device=last_hidden_states.device)
    return last_hidden_states[row_indexes, sequence_lengths]


def http_post_json(
    url: str,
    *,
    body: dict,
    headers: Optional[dict[str, str]] = None,
    timeout_seconds: float = 60.0,
    max_attempts: int = 5,
) -> dict:
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        **(headers or {}),
    }
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, max_attempts) + 1):
        request = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read().decode("utf-8")
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON response from {url}: {payload[:500]}") from exc
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code} for {url}: {body_text}")
            if exc.code == 429 and ('"1113"' in body_text or "余额不足" in body_text or "资源包" in body_text):
                raise last_error from exc
            if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt >= max_attempts:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"Request failed for {url}: {exc}")
            if attempt >= max_attempts:
                raise last_error from exc
        time.sleep(min(20.0, 1.2 * attempt))
    raise last_error or RuntimeError(f"Request failed for {url} without an exception")


def extract_message_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"GLM response did not include choices: {payload}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif "text" in part:
                    text_parts.append(str(part.get("text") or ""))
        if text_parts:
            return "\n".join(text_parts).strip()
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    raise RuntimeError(f"GLM response did not include text content: {payload}")


def parse_json_object(content: str) -> dict:
    text = content.strip()
    if not text:
        raise RuntimeError("GLM returned empty content")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GLM returned non-JSON content: {content[:500]}") from exc
    raise RuntimeError(f"GLM returned non-JSON content: {content[:500]}")


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    batch_size = max(1, int(size))
    for index in range(0, len(values), batch_size):
        yield values[index : index + batch_size]


def sleep_for_rate_limit(client: object) -> None:
    interval = float(getattr(client, "request_interval_seconds", 0.0) or 0.0)
    if interval <= 0:
        return
    now = time.monotonic()
    last = float(getattr(client, "_last_request_monotonic", 0.0) or 0.0)
    wait_seconds = interval - (now - last)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
        now = time.monotonic()
    setattr(client, "_last_request_monotonic", now)
