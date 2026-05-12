import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.oc_service import get_active_character
from services.economy_service import transfer, get_balance

LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "1473718167929880791"))


class WalletCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
        """Best-effort: post to ledger channel without blocking the command."""
        if not interaction.guild:
            return
        channel = interaction.guild.get_channel(LEDGER_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.guild.fetch_channel(LEDGER_CHANNEL_ID)
            except Exception:
                return

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(embed=embed)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="wallet", description="Show your active OC's wallet")
    async def wallet(self, interaction: discord.Interaction):
        await interaction.response.defer()  # public

        if not interaction.guild:
            return await interaction.followup.send("Use this in a server, not DMs.", ephemeral=True)

        sb = self.sb()
        user_id = int(interaction.user.id)
        guild_id = int(interaction.guild.id)

        try:
            active = get_active_character(sb, user_id)
            if not active:
                return await interaction.followup.send("No active OC set. Use `/oc select <name>`.", ephemeral=True)

            # Always use the guild primary currency (by currency_id)
            cur = get_primary_currency(sb, guild_id)

            # Ensure wallet exists (non-destructive)
            ensure_wallet(sb, active["character_id"], cur["currency_id"])

            # Read balance by (character_id, currency_id) ONLY
            bal = get_balance(sb, active["character_id"], cur["currency_id"])
            emoji = cur.get("emoji") or ""

            embed = discord.Embed(
                title="Wallet",
                description=f"**{active['name']}**\n{emoji} **{cur['name']}**: `{bal}`",
                color=discord.Color.dark_teal(),
            )

            # Helpful debug footer (lets us confirm we’re reading the same currency_id everywhere)
            embed.set_footer(text=f"currency_id: {cur['currency_id']} • character_id: {active['character_id']}")
            embed.timestamp = discord.utils.utcnow()

            return await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[wallet] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error showing wallet.", ephemeral=True)

    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="pay", description="Pay another user from your active OC")
    @app_commands.describe(user="Who to pay", amount="Amount to send", reason="Optional note")
    async def pay(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: int,
        reason: str | None = None,
    ):
        await interaction.response.defer()  # public

        if not interaction.guild:
            return await interaction.followup.send("Use this in a server, not DMs.", ephemeral=True)
        if user.bot:
            return await interaction.followup.send("You can’t pay bots.", ephemeral=True)
        if amount <= 0:
            return await interaction.followup.send("Amount must be greater than 0.", ephemeral=True)
        if user.id == interaction.user.id:
            return await interaction.followup.send("You can’t pay yourself.", ephemeral=True)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)

            sender = get_active_character(sb, actor_id)
            if not sender:
                return await interaction.followup.send("No active OC set. Use `/oc select <name>`.", ephemeral=True)

            receiver = get_active_character(sb, int(user.id))
            if not receiver:
                return await interaction.followup.send(f"{user.display_name} has no active OC set.", ephemeral=True)

            # Ensure wallets exist
            ensure_wallet(sb, sender["character_id"], cur["currency_id"])
            ensure_wallet(sb, receiver["character_id"], cur["currency_id"])

            # ✅ Atomic transfer via RPC-backed economy_service.transfer
            try:
                transfer(
                    sb,
                    guild_id=guild_id,
                    currency_id=cur["currency_id"],
                    amount=int(amount),
                    tx_type="transfer",  # IMPORTANT: use transfer, not p2p
                    actor_discord_id=actor_id,
                    from_character_id=sender["character_id"],
                    to_character_id=receiver["character_id"],
                    reason=reason,
                )
            except RuntimeError as ex:
                # Our RPC errors typically surface as readable messages (e.g., "Insufficient funds")
                msg = str(ex).lower()
                if "insufficient" in msg:
                    return await interaction.followup.send("❌ Not enough funds.", ephemeral=True)
                raise

            emoji = cur.get("emoji") or ""
            public_msg = (
                f"💸 **{sender['name']}** paid **{receiver['name']}** "
                f"{emoji} `{amount}` **{cur['name']}**"
            )
            if reason:
                public_msg += f"\n📝 _{reason}_"

            # Ledger embed (best-effort)
            ledger = discord.Embed(
                title="Ledger • Player Transfer",
                description=f"{emoji} **{amount} {cur['ticker']}**",
                color=discord.Color.gold(),
            )
            ledger.add_field(name="From", value=f"**{sender['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="To", value=f"**{receiver['name']}** (`{user}`)", inline=False)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.set_footer(text=f"Guild: {interaction.guild.name} • Actor ID: {actor_id}")
            ledger.timestamp = discord.utils.utcnow()

            await self._post_ledger(interaction, ledger)

            return await interaction.followup.send(public_msg)

        except Exception as e:
            print(f"[pay] error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error processing payment.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletCog(bot))