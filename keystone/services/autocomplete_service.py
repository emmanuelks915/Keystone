# services/autocomplete_service.py
from __future__ import annotations

from discord import app_commands
import discord


async def oc_name_autocomplete(interaction: discord.Interaction, current: str):
    """
    Autocomplete OC names.

    If the command has a `user` option selected (like /money mint user:...), we use that user's OCs.
    Otherwise we default to the interaction user.
    """
    sb = getattr(interaction.client, "supabase", None)
    if sb is None:
        return []

    # Resolve target user (for staff tools: `user` option)
    target_user = None
    try:
        target_user = getattr(interaction.namespace, "user", None)
    except Exception:
        target_user = None

    discord_id = int(target_user.id) if target_user else int(interaction.user.id)

    try:
        res = (
            sb.table("characters")
            .select("name, created_at")
            .eq("user_id", discord_id)
            .order("created_at", desc=False)
            .execute()
        )
        rows = getattr(res, "data", None) or []
    except Exception:
        return []

    names = [r.get("name") for r in rows if r.get("name")]

    if current:
        c = current.casefold()
        names = [n for n in names if c in n.casefold()]

    # Discord autocomplete limit: 25
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]