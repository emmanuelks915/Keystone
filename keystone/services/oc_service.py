from __future__ import annotations

from services.db import get_supabase_client


def get_oc_by_owner_and_name(owner_discord_id: int, oc_name: str) -> dict | None:
    sb = get_supabase_client()
    oc_name = (oc_name or "").strip()

    res = (
        sb.table("ocs")
        .select("oc_id, oc_name, owner_discord_id, avatar_url")
        .eq("owner_discord_id", str(owner_discord_id))  # stored as text
        .eq("oc_name", oc_name)
        .limit(1)              # ✅ avoid .single() crash
        .execute()
    )

    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def get_oc_by_owner_and_name_or_raise(owner_discord_id: int, oc_name: str) -> dict:
    oc = get_oc_by_owner_and_name(owner_discord_id, oc_name)
    if not oc:
        raise ValueError("OC not found for that user. Check the exact name.")
    return oc
