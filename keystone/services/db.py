# services/db.py
import os
from supabase import create_client

_client = None

def get_supabase_client():
    global _client
    if _client is not None:
        return _client

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set.")

    _client = create_client(url, key)
    return _client
