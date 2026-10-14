"""Async synthetic target with known capacity.

For ``CONCURRENCY_LIMIT`` permits and a fixed ``SERVICE_TIME_MS``, its nominal
capacity is C = CONCURRENCY_LIMIT / (SERVICE_TIME_MS / 1000) requests per second.
"""

import asyncio
import contextlib
import json
import logging
import os
from urllib.parse import urlsplit

DEFAULT_CONCURRENCY_LIMIT = 10
DEFAULT_SERVICE_TIME_MS = 100
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
IDLE_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


class SyntheticTarget:
    """Serve fixed-duration work behind a bounded concurrency semaphore."""

    def __init__(self, concurrency_limit: int, service_time_ms: int) -> None:
        if concurrency_limit <= 0:
            raise ValueError("CONCURRENCY_LIMIT must be greater than zero")
        if service_time_ms <= 0:
            raise ValueError("SERVICE_TIME_MS must be greater than zero")

        self.concurrency_limit = concurrency_limit
        self.service_time_ms = service_time_ms
        self._service_time_seconds = service_time_ms / 1000
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self.in_flight = 0
        self.queued = 0
        self.completed = 0

    def stats(self) -> dict[str, int]:
        """Return the current request counts."""
        return {
            "in_flight": self.in_flight,
            "queued": self.queued,
            "completed": self.completed,
        }

    async def _work(self) -> None:
        self.queued += 1
        acquired = False
        completed = False
        try:
            await self._semaphore.acquire()
            acquired = True
            self.queued -= 1
            self.in_flight += 1
            await asyncio.sleep(self._service_time_seconds)
            completed = True
        finally:
            if acquired:
                self.in_flight -= 1
                if completed:
                    self.completed += 1
                self._semaphore.release()
            else:
                self.queued -= 1

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve HTTP/1.1 requests on one connection until it is closed.

        The connection ends when the client closes it, sends ``Connection: close``,
        goes idle for ``IDLE_TIMEOUT_SECONDS``, or a request is malformed or its
        handler fails. Only this connection is affected in each case.
        """
        try:
            while await self._serve_one(reader, writer):
                pass
        except Exception:
            logger.exception("closing connection after handler error")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve_one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """Handle one request; return True if the connection stays open."""
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), IDLE_TIMEOUT_SECONDS
            )
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            return False
        except asyncio.LimitOverrunError:
            await self._respond(
                writer, 400, "Bad Request", {"error": "bad request"}, keep_alive=False
            )
            return False

        lines = raw_head.split(b"\r\n")
        request_line = lines[0]
        try:
            method, request_target, version = request_line.decode("ascii").split()
        except (UnicodeDecodeError, ValueError):
            await self._respond(
                writer, 400, "Bad Request", {"error": "bad request"}, keep_alive=False
            )
            return False

        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if sep:
                headers[name.strip().lower().decode("ascii", "replace")] = (
                    value.strip().decode("ascii", "replace")
                )

        connection_tokens = {
            token.strip().lower()
            for token in headers.get("connection", "").split(",")
        }
        keep_alive = "close" not in connection_tokens

        try:
            body_length = int(headers.get("content-length", "0"))
        except ValueError:
            body_length = -1
        if body_length < 0:
            await self._respond(
                writer, 400, "Bad Request", {"error": "bad request"}, keep_alive=False
            )
            return False
        if body_length:
            # Discard any request body so the next request starts at the right byte.
            await asyncio.wait_for(reader.readexactly(body_length), IDLE_TIMEOUT_SECONDS)

        if version != "HTTP/1.1":
            await self._respond(
                writer,
                400,
                "Bad Request",
                {"error": "HTTP/1.1 required"},
                keep_alive=False,
            )
            return False
        if method != "GET":
            await self._respond(
                writer,
                405,
                "Method Not Allowed",
                {"error": "GET required"},
                keep_alive=keep_alive,
            )
            return keep_alive

        path = urlsplit(request_target).path
        if path == "/healthz":
            await self._respond(
                writer, 200, "OK", {"status": "ok"}, keep_alive=keep_alive
            )
        elif path == "/stats":
            await self._respond(writer, 200, "OK", self.stats(), keep_alive=keep_alive)
        elif path == "/work":
            await self._work()
            await self._respond(
                writer, 200, "OK", {"status": "ok"}, keep_alive=keep_alive
            )
        else:
            await self._respond(
                writer, 404, "Not Found", {"error": "not found"}, keep_alive=keep_alive
            )
        return keep_alive

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        payload: object,
        *,
        keep_alive: bool,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        connection = "keep-alive" if keep_alive else "close"
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: {connection}\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()


def target_from_env() -> SyntheticTarget:
    """Create a target using ``CONCURRENCY_LIMIT`` and ``SERVICE_TIME_MS``."""
    concurrency_limit = int(
        os.environ.get("CONCURRENCY_LIMIT", str(DEFAULT_CONCURRENCY_LIMIT))
    )
    service_time_ms = int(os.environ.get("SERVICE_TIME_MS", str(DEFAULT_SERVICE_TIME_MS)))
    return SyntheticTarget(concurrency_limit, service_time_ms)


async def serve(target: SyntheticTarget, host: str, port: int) -> None:
    """Listen for HTTP requests until the process is stopped."""
    server = await asyncio.start_server(target.handle_client, host, port)
    async with server:
        await server.serve_forever()


def main() -> None:
    target = target_from_env()
    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    asyncio.run(serve(target, host, port))


if __name__ == "__main__":
    main()
