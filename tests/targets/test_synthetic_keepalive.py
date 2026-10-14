import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

import targets.synthetic.app as app_module
from targets.synthetic.app import SyntheticTarget


@asynccontextmanager
async def _server_for(target: SyntheticTarget) -> AsyncIterator[tuple[str, int]]:
    server = await asyncio.start_server(target.handle_client, "127.0.0.1", 0)
    if server.sockets is None:
        raise AssertionError("server did not expose its listening socket")
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield "127.0.0.1", port


async def _read_response(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, str], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    body = await reader.readexactly(int(headers["content-length"]))
    return status, headers, body


def _get(path: str, connection: str | None = None) -> bytes:
    extra = f"Connection: {connection}\r\n" if connection else ""
    return f"GET {path} HTTP/1.1\r\nHost: test\r\n{extra}\r\n".encode("ascii")


def test_sequential_requests_share_one_connection() -> None:
    """Several requests on one socket each get a reply; Connection: close ends it."""

    async def exercise() -> None:
        target = SyntheticTarget(concurrency_limit=2, service_time_ms=1)
        async with _server_for(target) as (host, port):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                for path in ("/healthz", "/work", "/work"):
                    writer.write(_get(path))
                    await writer.drain()
                    status, headers, _ = await _read_response(reader)
                    assert status == 200
                    assert headers["connection"] == "keep-alive"

                writer.write(_get("/stats"))
                await writer.drain()
                status, _, body = await _read_response(reader)
                assert status == 200
                assert json.loads(body) == {"in_flight": 0, "queued": 0, "completed": 2}

                writer.write(_get("/healthz", connection="close"))
                await writer.drain()
                status, headers, body = await _read_response(reader)
                assert status == 200
                assert headers["connection"] == "close"
                assert await asyncio.wait_for(reader.read(), 2.0) == b""
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(exercise())


def test_malformed_request_closes_only_its_connection() -> None:
    async def exercise() -> None:
        target = SyntheticTarget(concurrency_limit=2, service_time_ms=1)
        async with _server_for(target) as (host, port):
            bad_reader, bad_writer = await asyncio.open_connection(host, port)
            bad_writer.write(b"NOT A REQUEST\r\n\r\n")
            await bad_writer.drain()
            status, headers, _ = await _read_response(bad_reader)
            assert status == 400
            assert headers["connection"] == "close"
            assert await asyncio.wait_for(bad_reader.read(), 2.0) == b""
            bad_writer.close()
            await bad_writer.wait_closed()

            good_reader, good_writer = await asyncio.open_connection(host, port)
            try:
                good_writer.write(_get("/healthz"))
                await good_writer.drain()
                status, _, _ = await _read_response(good_reader)
                assert status == 200
            finally:
                good_writer.close()
                await good_writer.wait_closed()

    asyncio.run(exercise())


def test_request_body_is_consumed_before_next_request() -> None:
    async def exercise() -> None:
        target = SyntheticTarget(concurrency_limit=2, service_time_ms=1)
        async with _server_for(target) as (host, port):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(
                    b"GET /healthz HTTP/1.1\r\nHost: t\r\nContent-Length: 5\r\n\r\nhello"
                    + _get("/healthz")
                )
                await writer.drain()
                first, _, _ = await _read_response(reader)
                second, _, _ = await _read_response(reader)
                assert (first, second) == (200, 200)
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(exercise())


def test_idle_connection_is_closed_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "IDLE_TIMEOUT_SECONDS", 0.1)

    async def exercise() -> None:
        target = SyntheticTarget(concurrency_limit=2, service_time_ms=1)
        async with _server_for(target) as (host, port):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                writer.write(_get("/healthz"))
                await writer.drain()
                status, headers, _ = await _read_response(reader)
                assert status == 200
                assert headers["connection"] == "keep-alive"
                assert await asyncio.wait_for(reader.read(), 2.0) == b""
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(exercise())
