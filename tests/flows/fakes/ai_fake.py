"""The AI model without the model: deterministic answers shaped like the real completions.

The fake decides what it was asked by comparing the system prompt with the constants the
services send, then answers in the JSON shape each service validates:

- assessment insights (``ai_insight_service``): fixed strengths / weaknesses / summary;
- advisor rerank (``ai_advisor_match_service.rerank_advisors``): every advisor id found in
  the prompt, scored 95, 90, 85 … in the order they were listed;
- seeker rerank (``rerank_seekers``): the same for seeker ids.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from app.core import openai_client
from app.services import ai_advisor_match_service, ai_insight_service, country_rule_ai_service

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class _Completions:
    def __init__(self, fake: FakeAI) -> None:
        self._fake = fake

    async def create(self, **kw: Any) -> Any:
        return self._fake.complete(kw)


class _Responses:
    def __init__(self, fake: FakeAI) -> None:
        self._fake = fake

    async def create(self, **kw: Any) -> Any:
        self._fake.calls.append(("responses.create", kw))
        return SimpleNamespace(output_text=json.dumps(self._fake.country_rule_answer))


class FakeAI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.chat = SimpleNamespace(completions=_Completions(self))
        self.responses = _Responses(self)
        self.insights = {
            "strengths": [
                {"category": "education", "text": "A completed degree in a demanded field."},
                {"category": "language", "text": "Strong English test results."},
            ],
            "weaknesses": ["Limited work experience in the destination."],
            "missing_requirements": ["Proof of funds for the first year."],
            "summary": "A solid profile with one financial gap to close.",
        }
        self.country_rule_answer: dict[str, Any] = {}

    def complete(self, kw: dict[str, Any]) -> Any:
        self.calls.append(("chat.completions.create", kw))
        messages = kw.get("messages") or []
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        if system == ai_insight_service._SYSTEM_PROMPT:
            content = json.dumps(self.insights)
        elif system == ai_advisor_match_service._SEEKER_SYSTEM_PROMPT:
            ids = list(dict.fromkeys(UUID_RE.findall(user)))
            content = json.dumps(
                {
                    "matches": [
                        {
                            "seeker_id": i,
                            "ai_score": max(50, 95 - 5 * n),
                            "reason": "Strong fit on destination and visa type.",
                        }
                        for n, i in enumerate(ids)
                    ]
                }
            )
        else:
            ids = list(dict.fromkeys(UUID_RE.findall(user)))
            content = json.dumps(
                {
                    "matches": [
                        {
                            "advisor_id": i,
                            "ai_score": max(50, 95 - 5 * n),
                            "reason": "Expertise matches the seeker's destination and visa.",
                        }
                        for n, i in enumerate(ids)
                    ]
                }
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=42),
        )

    def install(self, monkeypatch: Any) -> None:
        def get_client(*args: Any, **kwargs: Any) -> FakeAI:
            return self

        monkeypatch.setattr(openai_client, "get_openai_client", get_client)
        for module in (ai_insight_service, ai_advisor_match_service, country_rule_ai_service):
            monkeypatch.setattr(module, "get_openai_client", get_client)
