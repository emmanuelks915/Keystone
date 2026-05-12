# services/inventory_service.py

from __future__ import annotations
from typing import Optional


def upsert_item(
    sb,
    *,
    guild_id: int,
    name: str,
    item_class: str = "misc",
    wu: Optional[int] = None,
    sheet_url: Optional[str] = None,
    notes: Optional[str] = None,
    metadata: Optional[dict] = None,
    is_active: bool = True,
) -> dict:
    """
    Creates an item if it doesn't exist (by guild + case-insensitive name).
    If it exists, returns it (optionally updates fields if you want later).
    """
    name_clean = (name or "").strip()
    if not name_clean:
        raise ValueError("name required")

    # find existing by case-insensitive name
    res = (
        sb.table("items")
        .select("*")
        .eq("guild_id", int(guild_id))
        .ilike("name", name_clean)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if rows:
        return rows[0]

    row = {
        "guild_id": int(guild_id),
        "name": name_clean,
        "item_class": (item_class or "misc").strip().lower(),
        "wu": int(wu) if wu is not None else None,
        "sheet_url": (sheet_url or "").strip() or None,
        "notes": (notes or "").strip() or None,
        "metadata": metadata or None,
        "is_active": bool(is_active),
    }
    ins = sb.table("items").insert(row).execute()
    data = getattr(ins, "data", None) or []
    return data[0] if data else row


def get_item(sb, *, guild_id: int, item_id: str) -> Optional[dict]:
    res = (
        sb.table("items")
        .select("*")
        .eq("guild_id", int(guild_id))
        .eq("item_id", str(item_id))
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def apply_delta(
    sb,
    *,
    guild_id: int,
    character_id: str,
    item_id: str,
    delta: int,
    actor_discord_id: int,
    context: str,
    note: str | None = None,
) -> dict:
    payload = {
        "p_guild_id": int(guild_id),
        "p_character_id": str(character_id),
        "p_item_id": str(item_id),
        "p_delta": int(delta),
        "p_actor_discord_id": int(actor_discord_id),
        "p_context": str(context),
        "p_note": note,
    }
    try:
        res = sb.rpc("apply_inventory_delta", payload).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else {}
    except Exception as e:
        msg = str(e)
        if "INSUFFICIENT_QTY" in msg:
            raise RuntimeError("INSUFFICIENT_QTY")
        if "DELTA_ZERO" in msg:
            raise RuntimeError("DELTA_ZERO")
        raise


# ─────────────────────────────────────────────────────────────────────────────
# ✅ Convenience wrappers (this is the FIX for your shop cog import)
# ─────────────────────────────────────────────────────────────────────────────

def add_item(
    sb,
    *,
    guild_id: int,
    character_id: str,
    item_id: str,
    qty: int,
    actor_discord_id: int,
    context: str,
    note: str | None = None,
) -> dict:
    """
    Adds qty of an item to a character's inventory.
    Wrapper used by shop fulfillment and other cogs.
    """
    qty_i = int(qty)
    if qty_i <= 0:
        raise RuntimeError("DELTA_ZERO")
    return apply_delta(
        sb,
        guild_id=guild_id,
        character_id=character_id,
        item_id=item_id,
        delta=qty_i,
        actor_discord_id=actor_discord_id,
        context=context,
        note=note,
    )


def remove_item(
    sb,
    *,
    guild_id: int,
    character_id: str,
    item_id: str,
    qty: int,
    actor_discord_id: int,
    context: str,
    note: str | None = None,
) -> dict:
    """
    Removes qty of an item from a character's inventory.
    """
    qty_i = int(qty)
    if qty_i <= 0:
        raise RuntimeError("DELTA_ZERO")
    return apply_delta(
        sb,
        guild_id=guild_id,
        character_id=character_id,
        item_id=item_id,
        delta=-qty_i,
        actor_discord_id=actor_discord_id,
        context=context,
        note=note,
    )