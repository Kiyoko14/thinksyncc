import asyncio
from typing import Any

from supabase import Client, create_client

from core.config import get_settings

_client: Client | None = None


def get_supabase() -> Client:
    """Return a singleton Supabase client using the service-role key.

    The service-role key bypasses row-level security so the backend can
    perform authoritative operations. Never expose this key to the client.

    This synchronous client is intended for use in synchronous contexts
    (scripts, tests, sync helper functions). Inside ``async def`` request
    handlers use :func:`get_supabase_async` instead so that blocking network
    I/O never stalls the event loop.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return _client


class _AsyncBuilder:
    """Async proxy around a Supabase query builder.

    Every fluent builder method returns another ``_AsyncBuilder`` so chains
    keep working. The terminal ``.execute()`` is a coroutine that runs the
    blocking Supabase call in a worker thread via ``asyncio.to_thread``, so
    the event loop is never blocked on database network I/O.
    """

    def __init__(self, builder: Any) -> None:
        self._builder = builder

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._builder, name)
        if callable(attr):

            def _wrapper(*args: Any, **kwargs: Any) -> "_AsyncBuilder":
                result = attr(*args, **kwargs)
                # Re-wrap builders so the chain stays async; pass through
                # non-builder results (e.g. already-executed responses).
                if hasattr(result, "execute"):
                    return _AsyncBuilder(result)
                return result

            return _wrapper
        return attr

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self._builder.execute, *args, **kwargs)


class _AsyncClient:
    """Async proxy around the Supabase client.

    Only the builder-producing entry points (``table``, ``rpc``, ``from_``)
    are wrapped; everything else falls through to the underlying client.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name in ("table", "rpc", "from_"):

            def _builder(*args: Any, **kwargs: Any) -> _AsyncBuilder:
                return _AsyncBuilder(attr(*args, **kwargs))

            return _builder
        return attr


async def get_supabase_async() -> _AsyncClient:
    """Return an async-friendly proxy over the singleton Supabase client.

    Use inside ``async def`` functions and ``await`` the terminal
    ``.execute()`` call. The blocking network call is offloaded to a worker
    thread via ``asyncio.to_thread``, so the event loop stays free.

    Business logic and the client itself are unchanged; this is purely a
    concurrency adapter.
    """
    return _AsyncClient(get_supabase())
