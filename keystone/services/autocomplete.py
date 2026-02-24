# services/autocomplete.py
from discord import app_commands
from services.db import get_supabase_client

async def oc_name_autocomplete(interaction, current: str):
    sb = get_supabase_client()
    cur = (current or "").strip()

    res = (
        sb.table("ocs")
        .select("oc_name")
        .eq("owner_discord_id", str(interaction.user.id))
        .ilike("oc_name", f"%{cur}%")
        .limit(25)
        .execute()
    )

    rows = getattr(res, "data", None) or []
    return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows]
