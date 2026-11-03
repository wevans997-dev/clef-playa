import base64
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import Credentials

PRICE_PER_M_INPUT = {
    "clef": 0.24,
    "clef-flash": 0.038,
}

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}


class ClefError(RuntimeError):
    pass


@dataclass
class ClefResult:
    answers: dict[str, Any]
    usage: dict[str, int]
    model: str
    latency_s: float
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class ClefClient:
    def __init__(
        self,
        creds: Credentials,
        model: str = "clef-flash",
        max_attempts: int = 6,
        refresh: Callable[[], Credentials] | None = None,
    ):
        if model not in PRICE_PER_M_INPUT:
            raise ValueError(f"model must be one of {sorted(PRICE_PER_M_INPUT)}")
        self.model = model
        self.max_attempts = max_attempts
        # Each Clef variant is its own Workers AI model id; the shared endpoint rejects
        # model="clef-flash".
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{creds.account_id}/ai/run/@cf/cloudflare/{model}"
        self._token = creds.token
        self._refresh = refresh
        self._refresh_lock = threading.Lock()
        self._client = httpx.Client(timeout=httpx.Timeout(120, connect=20))

    def close(self):
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _rotate_token(self, seen: str):
        # Several worker threads will hit the same 401 at once; only refresh once.
        with self._refresh_lock:
            if self._token == seen and self._refresh:
                self._token = self._refresh().token

    def ask(
        self,
        state: Any,
        questions: dict[str, Any],
        images: list[bytes] | None = None,
        image_type: str = "image/jpeg",
    ) -> ClefResult:
        body: dict[str, Any] = {"model": self.model, "state": state, "questions": questions}
        if images:
            if len(images) > 4:
                raise ValueError("Clef accepts at most 4 images per request")
            body["images"] = [{"content_type": image_type, "base64": base64.b64encode(b).decode()} for b in images]

        last_err = None
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            token = self._token
            try:
                r = self._client.post(self.url, json=body, headers=self._headers())
            except httpx.TransportError as e:
                last_err = e
                self._sleep(attempt)
                continue
            latency = time.monotonic() - t0

            if r.status_code in (401, 403) and self._refresh:
                last_err = ClefError(f"HTTP {r.status_code}: {r.text[:300]}")
                self._rotate_token(token)
                continue
            if r.status_code in RETRYABLE:
                last_err = ClefError(f"HTTP {r.status_code}: {r.text[:300]}")
                self._sleep(attempt, r.headers.get("retry-after"))
                continue
            if r.status_code != 200:
                raise ClefError(f"HTTP {r.status_code}: {r.text[:1000]}")

            data = r.json()
            if not data.get("success", True):
                # The REST wrapper returns success=false with an errors[] list for model-level
                # failures (bad schema, oversized image); those are not worth retrying.
                raise ClefError(f"Clef error: {data.get('errors')}")
            result = data.get("result", data)
            return ClefResult(
                answers=result["answers"],
                usage=result.get("usage", {}),
                model=result.get("model", self.model),
                latency_s=latency,
                attempts=attempt,
                raw=result,
            )
        raise ClefError(f"gave up after {self.max_attempts} attempts: {last_err}")

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None = None):
        if retry_after and retry_after.isdigit():
            time.sleep(min(int(retry_after), 60))
            return
        time.sleep(min(30, (2**attempt) * 0.5) + random.uniform(0, 0.5))


def cost_usd(model: str, input_tokens: int) -> float:
    return input_tokens / 1_000_000 * PRICE_PER_M_INPUT[model]
