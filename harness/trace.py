"""Local JSON traces; telemetry clocks never supply business timestamps."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel


def json_value(value):
    if isinstance(value, BaseModel):
        return json_value(value.model_dump(mode="json"))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value

    return "[unsupported object]"


def safe_value(value, *, redact_free_text=True):
    value = json_value(value)
    def clean(v, key=""):
        key = key.lower()
        if any(word in key for word in ("password", "secret", "api_key", "authorization", "database_url", "cookie", "credential")) or key in ("token", "access_token", "user_id", "decided_by", "email", "phone"):
            return "[redacted]"
    
        if redact_free_text and (key in ("text", "body", "content", "message", "reason", "prompt", "response")
                                 or key == "arguments" and isinstance(v, str)) and v is not None:
            raw = json.dumps(v, sort_keys=True, ensure_ascii=False).encode()
            return "[redacted sha256:" + sha256(raw).hexdigest() + "]"
    
        if isinstance(v, dict):
            return {k: clean(item, k) for k, item in v.items()}
    
        if isinstance(v, list):
            return [clean(item, key) for item in v]
    
        if isinstance(v, str):
            v = re.sub(r"(?i)(bearer\s+|sk-)[\w.\-]+", "[redacted]", v)
            v = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted email]", v)
            v = re.sub(r"\w+://[^\s]+", "[redacted URL]", v)
        
        return v
    
    return clean(value)


class TraceCollector:
    def __init__(self, run_id=None, *, scenario_id=None, event_id=None, process_id=None, conversation_id=None, free_text=False, trace_id=None):
        self.run_id = run_id or uuid4().hex
        self.trace_id = trace_id or self.run_id
        self.spans = []
        self.links = dict(scenario_id=scenario_id, event_id=event_id, process_id=process_id, conversation_id=conversation_id)
        self.free_text = free_text
        self._lock = RLock()

    @contextmanager
    def span(self, operation, *, arguments=None, parent_span_id=None, kind="tool"):
        started = perf_counter()
        entry = dict(run_id=self.run_id, trace_id=self.trace_id, span_id=uuid4().hex,
            parent_span_id=parent_span_id, operation=operation, kind=kind,
            started_at=datetime.now(timezone.utc).isoformat(), arguments=safe_value(arguments, redact_free_text=not self.free_text),
            result=None, error_code=None, status="running")
        try:
            yield entry
            if entry["status"] == "running":
                entry["status"] = "ok"
        except BaseException:
            entry.update(status="error", error_code="unhandled_exception")
            raise
        finally:
            entry["ended_at"] = datetime.now(timezone.utc).isoformat()
            entry["duration_seconds"] = perf_counter() - started
            with self._lock:
                self.spans.append(safe_value(dict(entry, **self.links), redact_free_text=not self.free_text))

    def record_llm(self, *, model, model_version, prompt_version, input_tokens, output_tokens,
                   cost: Decimal | None, duration_seconds, error_code=None, parent_span_id=None):
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise ValueError("Token counts must be nonnegative integers")
    
        if not math.isfinite(duration_seconds) or duration_seconds < 0 or cost is not None and (not cost.is_finite() or cost < 0):
            raise ValueError("Invalid duration or cost")
    
        with self.span("llm.call", kind="llm", parent_span_id=parent_span_id) as entry:
            entry.update(model=model, model_version=model_version, prompt_version=prompt_version,
                         input_tokens=input_tokens, output_tokens=output_tokens, cost=cost,
                         reported_duration_seconds=duration_seconds,
                         error_code=error_code, status="error" if error_code else "ok")

    def event(self, name, **data):
        with self.span(name, kind="lifecycle", arguments=data) as entry:
            entry["result"] = data

    def save(self, path):
        with self._lock:
            data = dict(format_version=1, run_id=self.run_id, trace_id=self.trace_id, spans=self.spans, **self.links)
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
