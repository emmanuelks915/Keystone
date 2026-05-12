import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

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


class CurrencyAdminCog(commands.GroupCog, group_name="currency", group_description="Staff currency tools"):
    # Subcommand groups show as: /currency set primary, /currency view primary
    set_group = app_commands.Group(name="set", description="Staff: Set currency options")
    view_group = app_commands.Group(name="view", description="Staff: View currency info")

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

    async def _public_ok(self, interaction: discord.Interaction, content=None, embed=None):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed)
        return await interaction.response.send_message(content=content, embed=embed)

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
        """Best-effort: post to ledger channel without blocking command."""
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
    # Autocomplete
    # ─────────────────────────────────────────────────────────────

    async def _currency_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)

        q = (current or "").lower().strip()
        try:
            res = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception:
            rows = []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            ticker = str(r.get("ticker") or "")
            emoji = str(r.get("emoji") or "")
            primary = " ⭐" if r.get("is_primary") else ""
            label = f"{emoji} {name} ({ticker}){primary}".strip()

            if q and q not in f"{name} {ticker}".lower():
                continue

            out.append(app_commands.Choice(name=label[:100], value=str(r["currency_id"])))

        return out[:25]

    # ─────────────────────────────────────────────────────────────
    # /currency create, /currency list
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="create", description="Staff: Create a currency in this server")
    @app_commands.describe(
        name="Currency name (e.g., Crowns)",
        ticker="Short code (e.g., CRN)",
        emoji="Emoji (e.g., 🪙)",
    )
    async def create(self, interaction: discord.Interaction, name: str, ticker: str, emoji: str):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        name = name.strip()
        ticker = ticker.strip().upper()
        emoji = emoji.strip()

        if not name:
            return await self._private_err(interaction, "Name can’t be empty.")
        if not ticker or len(ticker) > 10:
            return await self._private_err(interaction, "Ticker must be 1–10 characters.")
        if len(emoji) > 32:
            return await self._private_err(interaction, "Emoji looks too long. Use a normal emoji like 🪙.")

        try:
            existing = (
                sb.table("currencies")
                .select("currency_id")
                .eq("guild_id", guild_id)
                .limit(1)
                .execute()
            )
            has_any = bool(getattr(existing, "data", None))

            row = {
                "guild_id": guild_id,
                "name": name,
                "ticker": ticker,
                "emoji": emoji,
                "is_primary": not has_any,
            }

            sb.table("currencies").insert(row).execute()

            msg = f"✅ Created {emoji} **{name}** (`{ticker}`)"
            if row["is_primary"]:
                msg += " and set it as **primary** ⭐"

            ledger = discord.Embed(
                title="Ledger • Currency Created",
                description=f"{emoji} **{name}** (`{ticker}`)",
                color=discord.Color.green(),
            )
            ledger.add_field(name="Primary?", value="Yes ⭐" if row["is_primary"] else "No", inline=True)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=msg)

        except Exception as e:
            print(f"[currency create] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error creating currency.")

    @app_commands.command(name="list", description="Staff: List currencies in this server")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .execute()
            )
            rows = getattr(res, "data", None) or []

            if not rows:
                return await self._public_ok(interaction, content="No currencies found. Use `/currency create`.")

            rows.sort(key=lambda r: (not bool(r.get("is_primary")), str(r.get("name") or "")))

            lines = []
            for r in rows:
                emoji = r.get("emoji") or ""
                name = r.get("name") or "?"
                ticker = r.get("ticker") or "?"
                star = " ⭐" if r.get("is_primary") else ""
                lines.append(f"- {emoji} **{name}** (`{ticker}`){star}")

            embed = discord.Embed(
                title="Currencies",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            )
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[currency list] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error listing currencies.")

    # ─────────────────────────────────────────────────────────────
    # /currency set primary
    # ─────────────────────────────────────────────────────────────

    @set_group.command(name="primary", description="Staff: Set the primary currency")
    @app_commands.describe(currency="Choose the currency to make primary")
    @app_commands.autocomplete(currency=_currency_autocomplete)
    async def set_primary(self, interaction: discord.Interaction, currency: str):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        currency_id = currency

        try:
            chk = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .eq("currency_id", currency_id)
                .limit(1)
                .execute()
            )
            row = (getattr(chk, "data", None) or [None])[0]
            if not row:
                return await self._private_err(interaction, "That currency was not found in this server.")

            sb.table("currencies").update({"is_primary": False}).eq("guild_id", guild_id).execute()
            sb.table("currencies").update({"is_primary": True}).eq("currency_id", currency_id).execute()

            emoji = row.get("emoji") or ""
            name = row.get("name") or "?"
            ticker = row.get("ticker") or "?"

            ledger = discord.Embed(
                title="Ledger • Primary Currency Changed",
                description=f"{emoji} **{name}** (`{ticker}`) ⭐",
                color=discord.Color.gold(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"✅ Set primary currency to {emoji} **{name}** (`{ticker}`) ⭐")

        except Exception as e:
            print(f"[currency set primary] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error setting primary currency.")

    # ─────────────────────────────────────────────────────────────
    # /currency view primary
    # ─────────────────────────────────────────────────────────────

    @view_group.command(name="primary", description="Staff: View the primary currency")
    async def view_primary(self, interaction: discord.Interaction):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .eq("is_primary", True)
                .limit(1)
                .execute()
            )
            row = (getattr(res, "data", None) or [None])[0]
            if not row:
                return await self._public_ok(interaction, content="No primary currency set. Use `/currency set primary`.")

            emoji = row.get("emoji") or ""
            name = row.get("name") or "?"
            ticker = row.get("ticker") or "?"

            embed = discord.Embed(
                title="Primary Currency",
                description=f"{emoji} **{name}** (`{ticker}`) ⭐",
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"currency_id: {row['currency_id']}")
            embed.timestamp = discord.utils.utcnow()
            return await self._public_ok(interaction, embed=embed)

        except Exception as e:
            print(f"[currency view primary] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error reading primary currency.")

    # ─────────────────────────────────────────────────────────────
    # /currency rename (safe)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="rename", description="Staff: Rename or edit a currency safely")
    @app_commands.describe(
        currency="Which currency to edit",
        name="New display name (leave blank to keep)",
        ticker="New ticker (leave blank to keep)",
        emoji="New emoji (leave blank to keep)",
    )
    @app_commands.autocomplete(currency=_currency_autocomplete)
    async def rename(
        self,
        interaction: discord.Interaction,
        currency: str,
        name: str | None = None,
        ticker: str | None = None,
        emoji: str | None = None,
    ):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        currency_id = currency

        try:
            chk = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .eq("currency_id", currency_id)
                .limit(1)
                .execute()
            )
            row = (getattr(chk, "data", None) or [None])[0]
            if not row:
                return await self._private_err(interaction, "That currency was not found in this server.")

            new_name = (name or "").strip()
            new_ticker = (ticker or "").strip()
            new_emoji = (emoji or "").strip()

            updates = {}

            if new_name:
                updates["name"] = new_name

            if new_ticker:
                new_ticker = new_ticker.upper()
                if len(new_ticker) > 10:
                    return await self._private_err(interaction, "Ticker must be 1–10 characters.")
                updates["ticker"] = new_ticker

            if new_emoji:
                if len(new_emoji) > 32:
                    return await self._private_err(interaction, "Emoji looks too long.")
                updates["emoji"] = new_emoji

            if not updates:
                return await self._private_err(interaction, "Provide at least one field to change (name/ticker/emoji).")

            sb.table("currencies").update(updates).eq("currency_id", currency_id).execute()

            before = f"{row.get('emoji') or ''} {row.get('name') or '?'} ({row.get('ticker') or '?'})"
            after_name = updates.get("name", row.get("name") or "?")
            after_ticker = updates.get("ticker", row.get("ticker") or "?")
            after_emoji = updates.get("emoji", row.get("emoji") or "")
            after = f"{after_emoji} {after_name} ({after_ticker})"

            ledger = discord.Embed(
                title="Ledger • Currency Updated",
                description=f"**Before:** {before}\n**After:** {after}",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"✅ Updated currency: {after}")

        except Exception as e:
            print(f"[currency rename] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error updating currency.")

    # ─────────────────────────────────────────────────────────────
    # /currency delete (guarded)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="delete", description="Staff: Delete a currency (guarded)")
    @app_commands.describe(
        currency="Which currency to delete",
        confirm="Must be TRUE to confirm deletion",
    )
    @app_commands.autocomplete(currency=_currency_autocomplete)
    async def delete(self, interaction: discord.Interaction, currency: str, confirm: bool):
        await interaction.response.defer()  # public

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private_err(interaction, "Use this in a server, not DMs.")
        if not confirm:
            return await self._private_err(interaction, "Deletion cancelled. (Set `confirm` to TRUE to delete.)")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        currency_id = currency

        try:
            chk = (
                sb.table("currencies")
                .select("currency_id,name,ticker,emoji,is_primary")
                .eq("guild_id", guild_id)
                .eq("currency_id", currency_id)
                .limit(1)
                .execute()
            )
            row = (getattr(chk, "data", None) or [None])[0]
            if not row:
                return await self._private_err(interaction, "That currency was not found in this server.")

            if row.get("is_primary"):
                return await self._private_err(
                    interaction,
                    "❌ You can’t delete the **primary** currency. Use `/currency set primary` first.",
                )

            # Guard: refuse if referenced by wallets/transactions
            w = (
                sb.table("wallets")
                .select("wallet_id", count="exact")
                .eq("currency_id", currency_id)
                .execute()
            )
            wallet_count = int(getattr(w, "count", 0) or 0)

            t = (
                sb.table("transactions")
                .select("tx_id", count="exact")
                .eq("currency_id", currency_id)
                .execute()
            )
            tx_count = int(getattr(t, "count", 0) or 0)

            if wallet_count > 0 or tx_count > 0:
                return await self._private_err(
                    interaction,
                    f"❌ Can’t delete: this currency is in use. "
                    f"(wallets={wallet_count}, transactions={tx_count})",
                )

            sb.table("currencies").delete().eq("currency_id", currency_id).execute()

            emoji = row.get("emoji") or ""
            name = row.get("name") or "?"
            ticker = row.get("ticker") or "?"

            ledger = discord.Embed(
                title="Ledger • Currency Deleted",
                description=f"{emoji} **{name}** (`{ticker}`)",
                color=discord.Color.red(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public_ok(interaction, content=f"🗑️ Deleted currency {emoji} **{name}** (`{ticker}`)")

        except Exception as e:
            print(f"[currency delete] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error deleting currency.")


async def setup(bot: commands.Bot):
    await bot.add_cog(CurrencyAdminCog(bot))