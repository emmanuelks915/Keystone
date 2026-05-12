import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.oc_service import find_character_by_name
from services.economy_service import transfer, get_balance
from services.autocomplete_service import oc_name_autocomplete  # ✅ autocomplete

LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "1473718167929880791"))


def _parse_dev_ids() -> set[int]:
    raw = (os.getenv("DEV_USER_IDS") or "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _is_dev(user_id: int) -> bool:
    return user_id in _parse_dev_ids()


def _has_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


class WalletAdminCog(commands.GroupCog, group_name="money", group_description="Staff money tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        return _has_admin(interaction) or _is_dev(interaction.user.id)

    async def _private_err(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public_ok(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
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

    @app_commands.command(name="mint", description="Staff: add money to a specific OC")
    @app_commands.describe(
        user="Who receives money",
        oc_name="Which OC to receive it (required)",
        amount="Amount to add",
        reason="Optional note",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def mint(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        oc_name: str,
        amount: int,
        reason: str | None = None,
    ):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be greater than 0.")
        if user.bot:
            return await self._private_err(interaction, "You can’t mint money to bots.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)

            target = find_character_by_name(sb, int(user.id), oc_name)
            if not target:
                return await self._private_err(
                    interaction,
                    f"That OC wasn’t found for {user.display_name}. (Make sure the name matches.)",
                )

            ensure_wallet(sb, target["character_id"], cur["currency_id"])

            transfer(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                amount=int(amount),
                tx_type="mint",
                actor_discord_id=actor_id,
                from_character_id=None,
                to_character_id=target["character_id"],
                reason=reason,
            )

            emoji = cur.get("emoji") or ""
            msg = f"✅ Minted {emoji} `{amount}` **{cur['name']}** to **{target['name']}** ({user.mention})"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Mint",
                description=f"{emoji} **+{amount} {cur['ticker']}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="To", value=f"**{target['name']}** (`{user}`)", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()

            await self._post_ledger(interaction, ledger)
            return await self._public_ok(interaction, content=msg)

        except Exception as e:
            print(f"[money mint] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error minting money.")

    @app_commands.command(name="burn", description="Staff: remove money from a specific OC")
    @app_commands.describe(
        user="Who loses money",
        oc_name="Which OC to remove it from (required)",
        amount="Amount to remove",
        reason="Optional note",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def burn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        oc_name: str,
        amount: int,
        reason: str | None = None,
    ):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private_err(interaction, "Amount must be greater than 0.")
        if user.bot:
            return await self._private_err(interaction, "You can’t burn money from bots.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)

            target = find_character_by_name(sb, int(user.id), oc_name)
            if not target:
                return await self._private_err(
                    interaction,
                    f"That OC wasn’t found for {user.display_name}. (Make sure the name matches.)",
                )

            ensure_wallet(sb, target["character_id"], cur["currency_id"])

            bal = get_balance(sb, target["character_id"], cur["currency_id"])
            if bal < amount:
                return await self._private_err(
                    interaction,
                    f"❌ Not enough funds. Current balance is `{bal}`.",
                )

            transfer(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                amount=int(amount),
                tx_type="burn",
                actor_discord_id=actor_id,
                from_character_id=target["character_id"],
                to_character_id=None,
                reason=reason,
            )

            emoji = cur.get("emoji") or ""
            msg = f"✅ Burned {emoji} `{amount}` **{cur['name']}** from **{target['name']}** ({user.mention})"
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Burn",
                description=f"{emoji} **-{amount} {cur['ticker']}**",
                color=discord.Color.red(),
            )
            ledger.add_field(name="From", value=f"**{target['name']}** (`{user}`)", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()

            await self._post_ledger(interaction, ledger)
            return await self._public_ok(interaction, content=msg)

        except Exception as e:
            print(f"[money burn] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error burning money.")

    @app_commands.command(name="setbalance", description="Staff: set an OC's balance exactly")
    @app_commands.describe(
        user="Whose OC to edit",
        oc_name="Which OC to set balance for (required)",
        amount="New balance (exact)",
        reason="Optional note",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def setbalance(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        oc_name: str,
        amount: int,
        reason: str | None = None,
    ):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if amount < 0:
            return await self._private_err(interaction, "Balance can’t be negative.")
        if user.bot:
            return await self._private_err(interaction, "You can’t set balance for bots.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            cur = get_primary_currency(sb, guild_id)

            target = find_character_by_name(sb, int(user.id), oc_name)
            if not target:
                return await self._private_err(
                    interaction,
                    f"That OC wasn’t found for {user.display_name}. (Make sure the name matches.)",
                )

            ensure_wallet(sb, target["character_id"], cur["currency_id"])

            old = get_balance(sb, target["character_id"], cur["currency_id"])
            new = int(amount)

            emoji = cur.get("emoji") or ""

            if old == new:
                msg = (
                    f"✅ Balance for **{target['name']}** ({user.mention}) is already "
                    f"{emoji} `{new}` **{cur['name']}** (no change)."
                )
                return await self._public_ok(interaction, content=msg)

            # ✅ IMPORTANT: SETBALANCE is atomic and expects "amount = NEW balance"
            # and uses from_character_id as the wallet to set.
            transfer(
                sb,
                guild_id=guild_id,
                currency_id=cur["currency_id"],
                amount=new,  # NEW BALANCE (not delta)
                tx_type="setbalance",
                actor_discord_id=actor_id,
                from_character_id=target["character_id"],
                to_character_id=None,
                reason=f"setbalance {old} -> {new}" + (f" | {reason}" if reason else ""),
            )

            delta = new - old

            msg = (
                f"✅ Set **{target['name']}** ({user.mention}) balance to "
                f"{emoji} `{new}` **{cur['name']}** (was `{old}`)"
            )
            if reason:
                msg += f"\n📝 _{reason}_"

            ledger = discord.Embed(
                title="Ledger • Set Balance",
                description=f"{emoji} **{cur['ticker']}**",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="OC", value=f"**{target['name']}** (`{user}`)", inline=False)
            ledger.add_field(name="Old → New", value=f"`{old}` → `{new}`", inline=False)
            ledger.add_field(name="Δ Change", value=f"`{delta:+d}`", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()

            await self._post_ledger(interaction, ledger)
            return await self._public_ok(interaction, content=msg)

        except Exception as e:
            print(f"[money setbalance] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error setting balance.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WalletAdminCog(bot))