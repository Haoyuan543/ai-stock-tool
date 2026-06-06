from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
from typing import Any

import httpx

from backend.config import get_settings


DEFAULT_OPENAI_MODEL = "gpt-5"


class AIAnalyzer:
    def __init__(self) -> None:
        self.settings = get_settings()

    def analyze(self, prompt: str, fallback_markdown: str, model: str | None = None) -> dict[str, Any]:
        selected_model = (model or self.settings.openai_model or DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
        if not self.settings.openai_api_key:
            return {
                "markdown": fallback_markdown + "\n\n> Data Missing: OPENAI_API_KEY is not configured. Fallback mode was used.",
                "analysis_mode": "fallback",
                "openai_error": "OPENAI_API_KEY is not configured.",
                "model_used": selected_model,
            }

        result_queue: Queue[dict[str, Any]] = Queue(maxsize=1)
        worker = Thread(target=self._call_openai, args=(prompt, fallback_markdown, result_queue, selected_model), daemon=True)
        worker.start()
        try:
            return result_queue.get(timeout=self.settings.openai_timeout_seconds)
        except Empty:
            seconds = int(self.settings.openai_timeout_seconds)
            return {
                "markdown": fallback_markdown
                + f"\n\n> Data Missing: OpenAI API analysis timed out after {seconds} seconds. Fallback mode was used.",
                "analysis_mode": "fallback",
                "openai_error": f"OpenAI API analysis timed out after {seconds} seconds.",
                "model_used": selected_model,
            }

    def _call_openai(self, prompt: str, fallback_markdown: str, result_queue: Queue[dict[str, Any]], model: str) -> None:
        try:
            response = httpx.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": prompt,
                    "max_output_tokens": self.settings.openai_max_output_tokens,
                },
                timeout=httpx.Timeout(
                    self.settings.openai_timeout_seconds,
                    connect=20.0,
                    read=self.settings.openai_timeout_seconds,
                    write=20.0,
                    pool=20.0,
                ),
            )
            response.raise_for_status()
            data = response.json()
            output_text = self._extract_output_text(data)
            if not output_text:
                raise RuntimeError("OpenAI response did not include output text.")
            result = {
                "markdown": output_text,
                "analysis_mode": "openai",
                "openai_error": "",
                "model_used": model,
            }
        except Exception as exc:
            result = {
                "markdown": fallback_markdown + f"\n\n> Data Missing: OpenAI API analysis failed. Fallback mode was used. Error: {exc}",
                "analysis_mode": "fallback",
                "openai_error": str(exc),
                "model_used": model,
            }
        try:
            result_queue.put_nowait(result)
        except Exception:
            pass

    def _extract_output_text(self, data: dict[str, Any]) -> str:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]

        chunks: list[str] = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks).strip()
