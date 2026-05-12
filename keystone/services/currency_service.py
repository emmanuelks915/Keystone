from __future__ import annotations

from typing import Any


def get_primary_currency(sb, guild_id: int) -> dict[str, Any]:
    """
    Returns the primary currency row for a guild.
    Expected columns: currency_id (uuid), guild_id (bigint), name, ticker, emoji, is_primary (bool)
    """
    res = (
        sb.table("currencies")
        .select("*")
        .eq("guild_id", int(guild_id))
        .eq("is_primary", True)
        .limit(1)
        .execute()
    )

    rows = getattr(res, "data", None) or []
    if not rows:
        raise RuntimeError("No primary currency set for this server. Staff must configure a primary currency.")
    return rows[0]


def ensure_wallet(sb, character_id: str, currency_id: str) -> dict[str, Any]:
    """
    Ensures a wallet row exists for (character_id, currency_id).
    Non-destructive: NEVER overwrites balance.
    Returns the wallet row (existing or created).
    """
    # Try fetch first
    res = (
        sb.table("wallets")
        .select("*")
        .eq("character_id", character_id)
        .eq("currency_id", currency_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if rows:
        return rows[0]

    # Create if missing
    ins = (
        sb.table("wallets")
        .insert(
            {
                "character_id": character_id,
                "currency_id": currency_id,
                "balance": 0,
            }
        )
        .execute()
    )
    created = getattr(ins, "data", None) or []
    if not created:
        # If insert failed due to race, fetch again
        res2 = (
            sb.table("wallets")
            .select("*")
            .eq("character_id", character_id)
            .eq("currency_id", currency_id)
            .limit(1)
            .execute()
        )
        rows2 = getattr(res2, "data", None) or []
        if rows2:
            return rows2[0]
        raise RuntimeError("Failed to ensure wallet row.")
    return created[0]


# Backwards-compat alias (if any old code imports this)
get_guild_primary_currency = get_primary_currency