from __future__ import annotations

def get_character_by_name(sb, user_id: int, name: str) -> dict | None:
    res = (
        sb.table("characters")
        .select("character_id, name, is_active")
        .eq("user_id", user_id)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    target = (name or "").strip().casefold()
    for r in rows:
        if (r.get("name") or "").casefold() == target:
            return r
    return None

def get_active_character(sb, user_id: int) -> dict | None:
    res = (
        sb.table("characters")
        .select("character_id, name")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None