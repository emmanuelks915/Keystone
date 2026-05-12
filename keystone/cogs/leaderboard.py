import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency


class LeaderboardCog(commands.GroupCog, group_name="leaderboard", group_description="Leaderboards"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    async def _private(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public(self, interaction: discord.Interaction, content=None, embed=None):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    def _rank_emoji(self, idx: int) -> str:
        if idx == 1:
            return "🥇"
        if idx == 2:
            return "🥈"
        if idx == 3:
            return "🥉"
        return "🏅"

    @app_commands.command(name="ocs", description="Top OCs by wallet balance (primary currency)")
    @app_commands.describe(limit="How many to show (default 10, max 25)")
    async def ocs(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()  # public

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        limit = int(limit or 10)
        if limit < 1:
            limit = 10
        if limit > 25:
            limit = 25

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            # Get richest wallets for primary currency
            wres = (
                sb.table("wallets")
                .select("character_id,balance")
                .eq("currency_id", currency_id)
                .order("balance", desc=True)
                .limit(limit)
                .execute()
            )
            wallets = getattr(wres, "data", None) or []
            if not wallets:
                return await self._public(interaction, content="No wallets found yet for the primary currency.")

            # Fetch character names for those IDs
            char_ids = [w["character_id"] for w in wallets if w.get("character_id")]
            name_map: dict[str, str] = {}
            if char_ids:
                cres = (
                    sb.table("characters")
                    .select("character_id,name")
                    .in_("character_id", char_ids)
                    .execute()
                )
                chars = getattr(cres, "data", None) or []
                for c in chars:
                    cid = str(c.get("character_id"))
                    nm = str(c.get("name") or "").strip() or "Unknown OC"
                    name_map[cid] = nm

            lines = []
            for i, w in enumerate(wallets, start=1):
                cid = str(w.get("character_id") or "")
                bal = int(w.get("balance") or 0)
                nm = name_map.get(cid, "Unknown OC")
                lines.append(f"{self._rank_emoji(i)} **#{i}** — **{nm}**: {emoji} `{bal}` **{ticker}**")

            embed = discord.Embed(
                title="🏆 OC Wealth Leaderboard",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"Primary currency: {ticker}")
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[leaderboard ocs] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error building OC leaderboard.")

    @app_commands.command(name="banks", description="Top banks/companies by balance (primary currency)")
    @app_commands.describe(limit="How many to show (default 10, max 25)")
    async def banks(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()  # public

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        limit = int(limit or 10)
        if limit < 1:
            limit = 10
        if limit > 25:
            limit = 25

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            # Get richest company wallets for primary currency
            cwres = (
                sb.table("company_wallets")
                .select("company_id,balance")
                .eq("currency_id", currency_id)
                .order("balance", desc=True)
                .limit(limit)
                .execute()
            )
            rows = getattr(cwres, "data", None) or []
            if not rows:
                return await self._public(interaction, content="No bank/company wallets found yet for the primary currency.")

            company_ids = [r["company_id"] for r in rows if r.get("company_id")]
            name_map: dict[str, str] = {}
            if company_ids:
                cres = (
                    sb.table("companies")
                    .select("company_id,name")
                    .eq("guild_id", guild_id)
                    .in_("company_id", company_ids)
                    .execute()
                )
                comps = getattr(cres, "data", None) or []
                for c in comps:
                    cid = str(c.get("company_id"))
                    nm = str(c.get("name") or "").strip() or "Unknown Bank"
                    name_map[cid] = nm

            lines = []
            for i, r in enumerate(rows, start=1):
                company_id = str(r.get("company_id") or "")
                bal = int(r.get("balance") or 0)
                nm = name_map.get(company_id, "Unknown Bank")
                lines.append(f"{self._rank_emoji(i)} **#{i}** — **{nm}**: {emoji} `{bal}` **{ticker}**")

            embed = discord.Embed(
                title="🏦 Bank Wealth Leaderboard",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Primary currency: {ticker}")
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[leaderboard banks] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error building bank leaderboard.")


async def setup(bot: commands.Bot):
    await bot.add_cog(LeaderboardCog(bot))