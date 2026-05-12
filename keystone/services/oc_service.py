# services/oc_service.py
from __future__ import annotations

from typing import Any


def get_active_character(sb: Any, user_id: int) -> dict | None:
    """
    Returns the user's active character row: {character_id, name}
    """
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


def list_characters(sb: Any, user_id: int) -> list[dict]:
    """
    Returns all of a user's characters (basic fields).
    """
    res = (
        sb.table("characters")
        .select("character_id, name, is_active, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return getattr(res, "data", None) or []


def find_character_by_name(sb: Any, user_id: int, name: str) -> dict | None:
    """
    Case-insensitive exact name match against the user's characters.
    Returns row: {character_id, name, is_active}
    """
    target = (name or "").strip().casefold()
    if not target:
        return None

    rows = list_characters(sb, user_id)
    for r in rows:
        if (r.get("name") or "").casefold() == target:
            return r
    return None