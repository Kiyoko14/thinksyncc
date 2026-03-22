from supabase import Client, create_client

from core.config import get_settings

_client: Client | None = None


def get_supabase() -> Client:
    """Return a singleton Supabase client using the service-role key.

    The service-role key bypasses row-level security so the backend can
    perform authoritative operations. Never expose this key to the client.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return _client
