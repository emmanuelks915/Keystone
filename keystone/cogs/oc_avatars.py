# cogs/oc_avatars.py
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List

# ---------- Guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)


class OCAvatars(commands.Cog):
    """Commands for setting/updating OC avatar art (ocs.avatar_url)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- helpers ----------

    def _supabase(self):
        """Grab the Supabase client from the bot (set in bot.py)."""
        return self.bot.supabase

    def _extract_data(self, res):
        """Handle both supabase-py styles."""
        try:
            return res.data
        except AttributeError:
            if isinstance(res, dict):
                return res.get("data", None)
            return None

    async def _search_ocs_for_owner(
        self,
        owner_discord_id: str,
        partial: str,
        limit: int = 15,
    ) -> List[dict]:
        """Autocomplete: only show this user's OCs."""
        supabase = self._supabase()
        try:
            query = (
                supabase.table("ocs")
                .select("oc_id, oc_name")
                .eq("owner_discord_id", owner_discord_id)
            )
            if partial:
                query = query.ilike("oc_name", f"%{partial}%")
            res = query.order("oc_name").limit(limit).execute()
            return self._extract_data(res) or []
        except Exception as e:
            print(f"[OCAvatars] _search_ocs_for_owner error: {e}")
            return []

    async def _oc_name_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """OC autocomplete for this user's OCs."""
        if not interaction.user:
            return []
        owner_id = str(interaction.user.id)
        rows = await self._search_ocs_for_owner(owner_id, current, limit=15)
        choices: List[app_commands.Choice[str]] = []
        for row in rows:
            oc_name = row.get("oc_name")
            if not oc_name:
                continue
            choices.append(app_commands.Choice(name=oc_name, value=oc_name))
        return choices

    # ---------- /oc_set_avatar ----------

    @app_commands.command(
        name="oc_set_avatar",
        description="Set or update the avatar art for one of your registered OCs.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Your registered OC name.",
        image_url="Direct image URL (png/jpg/webp/gif). Leave blank if using an attachment.",
        image_attachment="Optional uploaded image instead of URL.",
    )
    @app_commands.autocomplete(oc_name=_oc_name_autocomplete)
    async def oc_set_avatar(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        image_url: Optional[str] = None,
        image_attachment: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        supabase = self._supabase()
        owner_id = str(interaction.user.id)

        # pick source: attachment wins over URL if both given
        final_url: Optional[str] = None

        if image_attachment is not None:
            if not image_attachment.content_type or not image_attachment.content_type.startswith("image/"):
                return await interaction.followup.send(
                    "❌ That attachment doesn't look like an image.",
                    ephemeral=True,
                )
            final_url = image_attachment.url
        elif image_url:
            image_url = image_url.strip()
            if not (image_url.startswith("http://") or image_url.startswith("https://")):
                return await interaction.followup.send(
                    "❌ The image URL must start with `http://` or `https://`.",
                    ephemeral=True,
                )
            final_url = image_url

        if not final_url:
            return await interaction.followup.send(
                "❌ Please provide an image URL **or** an image attachment.",
                ephemeral=True,
            )

        # resolve OC row (must belong to this user)
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, avatar_url")
                .eq("owner_discord_id", owner_id)
                .ilike("oc_name", oc_name)
                .limit(1)
                .execute()
            )
            rows = self._extract_data(res) or []
        except Exception as e:
            print(f"[OCAvatars] SELECT ocs error: {e}")
            return await interaction.followup.send(
                "❌ Error looking up your OC. Make sure it's registered.",
                ephemeral=True,
            )

        if not rows:
            return await interaction.followup.send(
                f"❌ I couldn't find an OC named `{oc_name}` for your account.",
                ephemeral=True,
            )

        oc_row = rows[0]
        real_name = oc_row.get("oc_name", oc_name)
        oc_id = oc_row["oc_id"]

        # update avatar_url
        try:
            supabase.table("ocs").update(
                {"avatar_url": final_url}
            ).eq("oc_id", oc_id).execute()
        except Exception as e:
            print(f"[OCAvatars] UPDATE avatar_url error: {e}")
            return await interaction.followup.send(
                "❌ I couldn't save that avatar to the database.",
                ephemeral=True,
            )

        # confirmation embed
        embed = discord.Embed(
            title="✅ OC Avatar Updated",
            description=f"OC **{real_name}** now has updated avatar art.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=final_url)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OCAvatars(bot))
