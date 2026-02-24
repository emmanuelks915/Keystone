import discord
from config.skyfall import STAFF_ROLE_ID

def is_staff(member: discord.Member) -> bool:
    return any(getattr(r, "id", None) == STAFF_ROLE_ID for r in getattr(member, "roles", []))
