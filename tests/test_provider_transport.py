import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
import pytest

from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.transport import ProviderTransport

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    max_concurrency: int = 2,
    requests_per_minute: int = 20,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ProviderTransport:
    return ProviderTransport(
        timeout_seconds=5,
        max_concurrency=max_concurrency,
        requests_per_minute=requests_per_minute,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleep,
    )


def test_transport_limits_the_complete_request_concurrency() -> None:
    async def exercise() -> int:
        active = 0
        maximum = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            return httpx.Response(200, json={"ok": True})

        client = make_transport(handler, max_concurrency=2)
        calls = [
            asyncio.create_task(client.request("GET", f"https://provider.test/{index}"))
            for index in range(3)
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert maximum == 2
        release.set()
        await asyncio.gather(*calls)
        return maximum

    assert run(exercise()) == 2


def test_transport_waits_for_the_monotonic_rate_window_without_real_sleep() -> None:
    clock = FakeClock()
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json={"ok": True})

    transport = make_transport(
        handler,
        requests_per_minute=2,
        clock=clock,
        sleep=clock.sleep,
    )

    async def exercise() -> None:
        await transport.request("GET", "https://provider.test/one")
        await transport.request("GET", "https://provider.test/two")
        await transport.request("GET", "https://provider.test/three")

    run(exercise())

    assert request_count == 3
    assert clock.sleeps == [60.0]


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, ForgeErrorCode.MODEL_AUTH_FAILED, False),
        (403, ForgeErrorCode.MODEL_AUTH_FAILED, False),
        (429, ForgeErrorCode.MODEL_RATE_LIMITED, True),
        (418, ForgeErrorCode.MODEL_UNAVAILABLE, False),
        (500, ForgeErrorCode.MODEL_UNAVAILABLE, True),
        (503, ForgeErrorCode.MODEL_UNAVAILABLE, True),
    ],
)
def test_transport_maps_http_failures_without_exposing_response(
    status: int,
    code: ForgeErrorCode,
    retryable: bool,
) -> None:
    secret = "response-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"private {secret}")

    transport = make_transport(handler)

    with pytest.raises(ForgeException) as caught:
        run(transport.request("GET", "https://private-provider.test/models"))

    assert caught.value.code is code
    assert caught.value.retryable is retryable
    assert caught.value.details == {}
    rendered = " ".join((str(caught.value), repr(caught.value), repr(caught.value.as_result())))
    assert secret not in rendered
    assert "private-provider" not in rendered


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("secret connect failure"),
        httpx.ReadTimeout("secret timeout"),
    ],
)
def test_transport_maps_network_and_timeout_failures_to_safe_retryable_error(
    failure: httpx.HTTPError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    transport = make_transport(handler)

    with pytest.raises(ForgeException) as caught:
        run(transport.request("GET", "https://private-provider.test/models"))

    assert caught.value.code is ForgeErrorCode.MODEL_UNAVAILABLE
    assert caught.value.retryable is True
    assert caught.value.details == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = " ".join((str(caught.value), repr(caught.value), repr(caught.value.as_result())))
    assert "secret" not in rendered
    assert "private-provider" not in rendered
