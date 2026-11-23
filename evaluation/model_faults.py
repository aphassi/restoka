"""Inject a declared final-output fault at the test boundary of a live client."""
from typing import Any, Mapping


class FinalOutputFault:
    def __init__(self, client):
        self.client = client
        self.raw = None
        self.remaining = None

    def configure(self, _mocks, settings: Mapping[str, Any]) -> None:
        fault = settings.get("llm_structured_output")
        raw = fault.get("raw") if isinstance(fault, dict) else None
        self.raw = raw if isinstance(raw, str) else None
        self.remaining = fault.get("attempts") if isinstance(fault, dict) else None
        if self.remaining is not None and (type(self.remaining) is not int or self.remaining < 1):
            raise ValueError("Injected final-output fault must target a positive number of attempts")

    def complete(self, **kwargs):
        response = self.client.complete(**kwargs)
        if self.raw is None or self.remaining == 0:
            return response
        choices = response.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            return response
        choice = choices[0]
        message = choice["message"]
        if message.get("tool_calls") or choice.get("finish_reason") in {"length", "max_tokens"}:
            return response
        if self.remaining is not None:
            self.remaining -= 1
        return {**response, "choices": [{**choice, "message": {**message, "content": self.raw}}, *choices[1:]]}
