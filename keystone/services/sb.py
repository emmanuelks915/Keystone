from __future__ import annotations
from discord.ext import commands

def sb(bot: commands.Bot):
    """Get the Supabase client attached on the bot. Raises a clean error if missing."""
    client = getattr(bot, "supabase", None)
    if client is None:
        raise RuntimeError("Supabase is not configured on this bot (bot.supabase is missing).")
    return client