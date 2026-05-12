import math
import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands
from postgrest.exceptions import APIError

from services.currency_service import get_primary_currency
from services.economy_service import get_balance, apply_company_transaction

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


class TaxCog(commands.GroupCog, group_name="tax", group_description="Server taxes (staff)"):
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

    async def _private(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public(self, interaction: discord.Interaction, content=None, embed=None):
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
    # Settings helpers
    # ─────────────────────────────────────────────────────────────

    def _get_settings(self, sb, guild_id: int) -> dict:
        res = sb.table("tax_settings").select("*").eq("guild_id", guild_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
        # default shape (not yet created)
        return {
            "guild_id": guild_id,
            "treasury_company_id": None,
            "rate_percent": 0,
            "flat_amount": 0,
            "min_balance": 0,
            "enabled": False,
        }

    def _upsert_settings(self, sb, payload: dict) -> dict:
        res = sb.table("tax_settings").upsert(payload, on_conflict="guild_id").execute()
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else payload

    def _bank_autocomplete(self, interaction: discord.Interaction, current: str):
        # placeholder (discord requires async; implemented below)
        return []

    async def bank_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").lower().strip()

        res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).execute()
        rows = getattr(res, "data", None) or []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            if q and q not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=str(r["company_id"])))
        return out[:25]

    def _get_bank_name(self, sb, bank_id: str) -> str:
        res = sb.table("companies").select("name").eq("company_id", bank_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("name") or "Treasury") if rows else "Treasury"

    # ─────────────────────────────────────────────────────────────
    # Character discovery (works even if you don't know schema)
    # ─────────────────────────────────────────────────────────────

    def _find_guild_character_ids(self, sb, guild_id: int, currency_id: str) -> list[str]:
        """
        Try best-effort ways to get the set of character_ids relevant to this guild.

        Priority:
          A) characters.guild_id exists -> filter by it
          B) else: all character_ids seen in transactions for this guild
          C) else: all wallets in primary currency
        """
        # A) Try characters.guild_id
        try:
            r = sb.table("characters").select("character_id").eq("guild_id", guild_id).execute()
            rows = getattr(r, "data", None) or []
            ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
            if ids:
                return list(dict.fromkeys(ids))
        except APIError:
            # column probably doesn't exist
            pass

        # B) transactions for this guild
        ids: list[str] = []
        try:
            r = (
                sb.table("transactions")
                .select("from_character_id,to_character_id")
                .eq("guild_id", guild_id)
                .limit(1000)
                .execute()
            )
            rows = getattr(r, "data", None) or []
            for row in rows:
                fc = row.get("from_character_id")
                tc = row.get("to_character_id")
                if fc:
                    ids.append(str(fc))
                if tc:
                    ids.append(str(tc))
            ids = list(dict.fromkeys([x for x in ids if x]))
            if ids:
                return ids
        except Exception:
            pass

        # C) fallback: all wallets in primary currency
        r = sb.table("wallets").select("character_id").eq("currency_id", currency_id).limit(1000).execute()
        rows = getattr(r, "data", None) or []
        ids = [str(x["character_id"]) for x in rows if x.get("character_id")]
        return list(dict.fromkeys(ids))

    def _fetch_characters(self, sb, character_ids: list[str]) -> list[dict]:
        if not character_ids:
            return []
        res = sb.table("characters").select("character_id,name").in_("character_id", character_ids).execute()
        rows = getattr(res, "data", None) or []
        # keep stable order by name
        rows.sort(key=lambda r: str(r.get("name") or "").lower())
        return rows

    # ─────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="view", description="Staff: View tax settings")
    async def view(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        s = self._get_settings(sb, guild_id)
        treasury = s.get("treasury_company_id")
        treasury_name = self._get_bank_name(sb, treasury) if treasury else "—"

        embed = discord.Embed(title="Tax Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value=str(bool(s.get("enabled"))), inline=True)
        embed.add_field(name="Treasury", value=treasury_name, inline=True)
        embed.add_field(name="Rate %", value=str(s.get("rate_percent") or 0), inline=True)
        embed.add_field(name="Flat", value=str(s.get("flat_amount") or 0), inline=True)
        embed.add_field(name="Min Balance", value=str(s.get("min_balance") or 0), inline=True)
        embed.timestamp = discord.utils.utcnow()
        return await self._public(interaction, embed=embed)

    @app_commands.command(name="set_treasury", description="Staff: Set the treasury bank that receives taxes")
    @app_commands.describe(bank="Which bank receives taxes")
    @app_commands.autocomplete(bank=bank_autocomplete)
    async def set_treasury(self, interaction: discord.Interaction, bank: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        # validate bank exists in guild
        res = sb.table("companies").select("company_id,name").eq("guild_id", guild_id).eq("company_id", bank).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if not rows:
            return await self._private(interaction, "That bank wasn’t found in this server.")

        self._upsert_settings(sb, {"guild_id": guild_id, "treasury_company_id": bank})

        return await self._public(interaction, content=f"✅ Treasury set to **{rows[0]['name']}**.")

    @app_commands.command(name="set_rate", description="Staff: Set tax rate percent (0-100)")
    @app_commands.describe(percent="Example: 5 = 5%")
    async def set_rate(self, interaction: discord.Interaction, percent: float):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if percent < 0 or percent > 100:
            return await self._private(interaction, "Percent must be between 0 and 100.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        self._upsert_settings(sb, {"guild_id": guild_id, "rate_percent": float(percent)})
        return await self._public(interaction, content=f"✅ Tax rate set to `{percent}%`.")

    @app_commands.command(name="set_flat", description="Staff: Set flat tax amount added to percent tax")
    @app_commands.describe(amount="Flat amount (>= 0)")
    async def set_flat(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if amount < 0:
            return await self._private(interaction, "Flat amount must be >= 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        self._upsert_settings(sb, {"guild_id": guild_id, "flat_amount": int(amount)})
        return await self._public(interaction, content=f"✅ Flat tax set to `{amount}`.")

    @app_commands.command(name="set_min_balance", description="Staff: Skip OCs below this balance")
    @app_commands.describe(amount="Minimum balance to be taxed (>= 0)")
    async def set_min_balance(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if amount < 0:
            return await self._private(interaction, "Minimum balance must be >= 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        self._upsert_settings(sb, {"guild_id": guild_id, "min_balance": int(amount)})
        return await self._public(interaction, content=f"✅ Min balance set to `{amount}`.")

    @app_commands.command(name="enable", description="Staff: Enable taxes")
    async def enable(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        self._upsert_settings(sb, {"guild_id": guild_id, "enabled": True})
        return await self._public(interaction, content="✅ Taxes enabled.")

    @app_commands.command(name="disable", description="Staff: Disable taxes")
    async def disable(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        self._upsert_settings(sb, {"guild_id": guild_id, "enabled": False})
        return await self._public(interaction, content="🛑 Taxes disabled.")

    @app_commands.command(name="preview", description="Staff: Preview next tax run totals")
    async def preview(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        s = self._get_settings(sb, guild_id)
        cur = get_primary_currency(sb, guild_id)
        treasury = s.get("treasury_company_id")
        if not treasury:
            return await self._private(interaction, "Set a treasury first with `/tax set_treasury`.")

        ids = self._find_guild_character_ids(sb, guild_id, cur["currency_id"])
        chars = self._fetch_characters(sb, ids)

        rate = float(s.get("rate_percent") or 0)
        flat = int(s.get("flat_amount") or 0)
        min_bal = int(s.get("min_balance") or 0)

        total = 0
        will_charge = 0
        skipped = 0

        for ch in chars:
            bal = get_balance(sb, ch["character_id"], cur["currency_id"])
            if bal < min_bal:
                skipped += 1
                continue
            tax = int(math.floor(bal * (rate / 100.0))) + flat
            if tax <= 0:
                skipped += 1
                continue
            if bal < tax:
                skipped += 1
                continue
            total += tax
            will_charge += 1

        treasury_name = self._get_bank_name(sb, treasury)
        emoji = cur.get("emoji") or ""

        embed = discord.Embed(title="Tax Preview", color=discord.Color.gold())
        embed.add_field(name="Currency", value=f"{emoji} {cur['name']}", inline=True)
        embed.add_field(name="Treasury", value=treasury_name, inline=True)
        embed.add_field(name="Rate + Flat", value=f"`{rate}% + {flat}`", inline=False)
        embed.add_field(name="Will Charge", value=str(will_charge), inline=True)
        embed.add_field(name="Skipped", value=str(skipped), inline=True)
        embed.add_field(name="Total Collected", value=f"{emoji} `{total}`", inline=False)
        embed.timestamp = discord.utils.utcnow()
        return await self._public(interaction, embed=embed)

    @app_commands.command(name="run", description="Staff: Run taxes now (moves funds to treasury)")
    @app_commands.describe(reason="Optional note for ledger")
    async def run(self, interaction: discord.Interaction, reason: str | None = None):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not bool(s.get("enabled")):
                return await self._private(interaction, "Taxes are disabled. Use `/tax enable`.")
            treasury = s.get("treasury_company_id")
            if not treasury:
                return await self._private(interaction, "Set a treasury first with `/tax set_treasury`.")

            cur = get_primary_currency(sb, guild_id)
            ids = self._find_guild_character_ids(sb, guild_id, cur["currency_id"])
            chars = self._fetch_characters(sb, ids)

            rate = float(s.get("rate_percent") or 0)
            flat = int(s.get("flat_amount") or 0)
            min_bal = int(s.get("min_balance") or 0)

            total = 0
            charged = 0
            skipped = 0

            for ch in chars:
                cid = ch["character_id"]
                bal = get_balance(sb, cid, cur["currency_id"])
                if bal < min_bal:
                    skipped += 1
                    continue

                tax = int(math.floor(bal * (rate / 100.0))) + flat
                if tax <= 0:
                    skipped += 1
                    continue
                if bal < tax:
                    skipped += 1
                    continue

                # Atomic: character -> treasury
                try:
                    apply_company_transaction(
                        sb,
                        guild_id=guild_id,
                        currency_id=cur["currency_id"],
                        amount=int(tax),
                        actor_discord_id=actor_id,
                        tx_type="DEPOSIT",
                        from_character_id=cid,
                        to_company_id=treasury,
                        reason=reason or "tax run",
                    )
                except RuntimeError:
                    skipped += 1
                    continue

                total += tax
                charged += 1

            emoji = cur.get("emoji") or ""
            treasury_name = self._get_bank_name(sb, treasury)

            msg = f"✅ Tax run complete. Collected {emoji} `{total}` into **{treasury_name}**. Charged `{charged}`, skipped `{skipped}`."
            await self._public(interaction, content=msg)

            ledger = discord.Embed(
                title="Ledger • Tax Run",
                description=f"🏛️ Collected {emoji} **{total} {cur['ticker']}** → **{treasury_name}**",
                color=discord.Color.dark_gold(),
            )
            ledger.add_field(name="Charged", value=str(charged), inline=True)
            ledger.add_field(name="Skipped", value=str(skipped), inline=True)
            ledger.add_field(name="Rate + Flat", value=f"`{rate}% + {flat}`", inline=False)
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            if reason:
                ledger.add_field(name="Reason", value=reason, inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

        except Exception as e:
            print(f"[tax run] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error running taxes.")


async def setup(bot: commands.Bot):
    await bot.add_cog(TaxCog(bot))