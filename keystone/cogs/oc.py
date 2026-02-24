import re
import traceback
import discord
from discord import app_commands
from discord.ext import commands

NAME_RE = re.compile(r"^[A-Za-z0-9 _'\-]{1,64}$")
DEFAULT_CURRENCY = "TBD"  # placeholder until Rail-Bound picks a name

# ✅ Define the group as an object (not a decorator)
oc = app_commands.Group(name="oc", description="Character (OC) commands")


class OCCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def sb(self):
        # Single source of truth: bot.supabase is attached in bot.py
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    @oc.command(name="create", description="Create a new OC")
    @app_commands.describe(name="Your character's name (max 64 characters)")
    async def oc_create(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)

        raw = (name or "").strip()
        if not raw:
            return await interaction.followup.send("Please provide a valid OC name.", ephemeral=True)
        if len(raw) > 64:
            return await interaction.followup.send("OC name is too long (max **64** chars).", ephemeral=True)
        if not NAME_RE.match(raw):
            return await interaction.followup.send(
                "OC name has invalid characters. Allowed: letters, numbers, space, `- _ '`. "
                "Example: `Edwyn Faerber` or `Mirabel_Silva`.",
                ephemeral=True
            )

        user_id = int(interaction.user.id)
        sb = self.sb()

        try:
            # 1) Ensure user exists
            sb.table("users").upsert({"user_id": user_id}).execute()

            # 2) Check duplicates (same user, case-insensitive)
            existing = (
                sb.table("characters")
                .select("character_id, name")
                .eq("user_id", user_id)
                .execute()
            )
            rows = getattr(existing, "data", None) or []
            for r in rows:
                if (r.get("name") or "").casefold() == raw.casefold():
                    return await interaction.followup.send(
                        f"You already have an OC named **{r['name']}**.\nID: `{r['character_id']}`",
                        ephemeral=True
                    )

            # 3) Insert character
            ins = (
                sb.table("characters")
                .insert({"user_id": user_id, "name": raw})
                .execute()
            )
            data = getattr(ins, "data", None) or []
            if not data:
                return await interaction.followup.send("Could not create OC (no row returned).", ephemeral=True)

            char = data[0]
            character_id = char["character_id"]

            # 4) Create default wallet (currency placeholder)
            # unique(character_id, currency) prevents duplicates if re-run
            sb.table("wallets").upsert({
                "character_id": character_id,
                "currency": DEFAULT_CURRENCY,
                "balance": 0
            }).execute()

        except Exception as e:
            print(f"[oc create] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error creating OC. Try again or ping staff.", ephemeral=True)

        embed = discord.Embed(
            title="OC Created",
            description=f"✅ Created **{raw}**",
            color=discord.Color.dark_teal()
        )
        embed.add_field(name="Character ID", value=f"`{character_id}`", inline=False)
        embed.add_field(name="Wallet", value=f"`{DEFAULT_CURRENCY}` balance: `0`", inline=False)

        return await interaction.followup.send(embed=embed, ephemeral=True)

    @oc.command(name="list", description="List your OCs")
    async def oc_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = int(interaction.user.id)
        sb = self.sb()

        try:
            res = (
                sb.table("characters")
                .select("character_id, name, is_active, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[oc list] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error fetching your OCs.", ephemeral=True)

        if not rows:
            return await interaction.followup.send("You don’t have any OCs yet. Use `/oc create`.", ephemeral=True)

        lines = []
        for r in rows:
            active_tag = " **(active)**" if r.get("is_active") else ""
            lines.append(f"- **{r['name']}**{active_tag}\n  `{r['character_id']}`")

        embed = discord.Embed(
            title="Your OCs",
            description="\n".join(lines),
            color=discord.Color.dark_teal()
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    @oc.command(name="select", description="Mark one of your OCs as active")
    @app_commands.describe(character_id="The character ID to set active")
    async def oc_select(self, interaction: discord.Interaction, character_id: str):
        await interaction.response.defer(ephemeral=True)

        user_id = int(interaction.user.id)
        sb = self.sb()

        # Light validation (UUID-ish)
        cid = (character_id or "").strip()
        if len(cid) < 10:
            return await interaction.followup.send("That doesn’t look like a valid character ID.", ephemeral=True)

        try:
            # Verify ownership
            res = (
                sb.table("characters")
                .select("character_id, name")
                .eq("character_id", cid)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await interaction.followup.send("OC not found (or it isn’t yours).", ephemeral=True)

            oc_name = rows[0]["name"]

            # Set all user's characters inactive
            sb.table("characters").update({"is_active": False}).eq("user_id", user_id).execute()
            # Set selected active
            sb.table("characters").update({"is_active": True}).eq("character_id", cid).execute()

        except Exception as e:
            print(f"[oc select] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error selecting OC.", ephemeral=True)

        return await interaction.followup.send(f"✅ Active OC set to **{oc_name}**.", ephemeral=True)


async def setup(bot: commands.Bot):
    # ✅ Register the /oc group
    bot.tree.add_command(oc)
    await bot.add_cog(OCCog(bot))