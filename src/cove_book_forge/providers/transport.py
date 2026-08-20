import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

import httpx

from cove_book_forge.errors import ForgeErrorCode, ForgeException

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _RequestRateGate:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Clock,
        sleep: Sleep,
    ) -> None:
        self._limit = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._clock()
                cutoff = now - 60.0
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._limit:
                    self._timestamps.append(now)
                    return
                delay = max(self._timestamps[0] + 60.0 - now, 0.0)
            await self._sleep(delay)


class _RequestLimits:
    def __init__(
        self,
        *,
        max_concurrency: int,
        requests_per_minute: int,
        clock: Clock,
        sleep: Sleep,
    ) -> None:
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.rate_gate = _RequestRateGate(
            requests_per_minute,
            clock=clock,
            sleep=sleep,
        )


class ProviderTransport:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_concurrency: int,
        requests_per_minute: int,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        request_limits: _RequestLimits | None = None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        resolved_clock = clock or time.monotonic
        resolved_sleep = sleep or asyncio.sleep
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._max_response_bytes = max_response_bytes
        self._request_limits = request_limits or _RequestLimits(
            max_concurrency=max_concurrency,
            requests_per_minute=requests_per_minute,
            clock=resolved_clock,
            sleep=resolved_sleep,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        async with self._request_limits.semaphore:
            await self._request_limits.rate_gate.acquire()
            try:
                async with (
                    httpx.AsyncClient(
                        timeout=self._timeout_seconds,
                        transport=self._transport,
                    ) as client,
                    client.stream(
                        method,
                        url,
                        headers=headers,
                        json=json,
                    ) as response,
                ):
                    self._raise_for_status(response.status_code)
                    content = await self._read_success_body(response)
                    return httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=content,
                        extensions=response.extensions,
                        request=response.request,
                    )
            except httpx.RequestError:
                pass
            raise ForgeException(
                ForgeErrorCode.MODEL_UNAVAILABLE,
                "Model request failed.",
                retryable=True,
            ) from None

    async def _read_success_body(self, response: httpx.Response) -> bytes:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(chunk) > self._max_response_bytes - len(content):
                raise ForgeException(
                    ForgeErrorCode.MODEL_OUTPUT_INVALID,
                    "Model output was malformed.",
                )
            content.extend(chunk)
        return bytes(content)

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            raise ForgeException(
                ForgeErrorCode.MODEL_AUTH_FAILED,
                "Model authentication failed.",
            )
        if status_code == 429:
            raise ForgeException(
                ForgeErrorCode.MODEL_RATE_LIMITED,
                "Model request was rate limited.",
                retryable=True,
            )
        raise ForgeException(
            ForgeErrorCode.MODEL_UNAVAILABLE,
            "Model request was unsuccessful.",
            retryable=500 <= status_code < 600,
        )
