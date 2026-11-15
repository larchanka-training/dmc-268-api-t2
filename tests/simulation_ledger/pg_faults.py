"""Plain PostgreSQL wire proxy: lose the actual COMMIT reply, never fake a DB result."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field

from sqlalchemy.engine import URL


@dataclass
class CommitFault:
    committed: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    commits: int = 0


@asynccontextmanager
async def lost_commit_reply(  # noqa: PLR0915 -- owned wire proxy lifecycle
    url: URL, *, before: bool = False
) -> AsyncIterator[tuple[URL, CommitFault]]:
    fault = CommitFault()
    tasks: set[asyncio.Task[None]] = set()

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        upstream_writer = None
        pump = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                url.host, url.port
            )

            async def send() -> None:
                assert upstream_writer is not None
                size = await reader.readexactly(4)
                upstream_writer.write(
                    size + await reader.readexactly(int.from_bytes(size, "big") - 4)
                )
                await upstream_writer.drain()
                while True:
                    kind = await reader.readexactly(1)
                    size = await reader.readexactly(4)
                    payload = await reader.readexactly(int.from_bytes(size, "big") - 4)
                    if (
                        before
                        and kind == b"Q"
                        and payload.upper().startswith(b"COMMIT")
                    ):
                        fault.commits += 1
                        fault.committed.set()
                        await fault.release.wait()
                        writer.close()
                        upstream_writer.close()
                        return
                    upstream_writer.write(kind + size + payload)
                    await upstream_writer.drain()

            pump = asyncio.create_task(send())
            while True:
                kind = await upstream_reader.readexactly(1)
                size = await upstream_reader.readexactly(4)
                payload = await upstream_reader.readexactly(
                    int.from_bytes(size, "big") - 4
                )
                if kind == b"C" and payload == b"COMMIT\0" and fault.commits == 0:
                    fault.commits += 1
                    fault.committed.set()
                    await fault.release.wait()
                    return  # Drop COMMIT CommandComplete and ReadyForQuery.
                writer.write(kind + size + payload)
                await writer.drain()
        except OSError, asyncio.IncompleteReadError:
            pass
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(
                    asyncio.CancelledError, OSError, asyncio.IncompleteReadError
                ):
                    await pump
            writer.close()
            if upstream_writer is not None:
                upstream_writer.close()
            tasks.discard(task)

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield url.set(host="127.0.0.1", port=port), fault
    finally:
        fault.release.set()
        server.close()
        await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
