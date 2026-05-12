import os
import traceback
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.currency_service import get_primary_currency, ensure_wallet
from services.oc_service import get_active_character
from services.economy_service import apply_company_transaction

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


class CasinoCog(commands.GroupCog, group_name="casino", group_description="House games (uses House Bank)"):
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

    async def _public(self, interaction: discord.Interaction, content=None, embed=None, ephemeral: bool = False):
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        return await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)

    async def _post_ledger(self, interaction: discord.Interaction, embed: discord.Embed):
        """Best-effort: post to ledger channel without blocking."""
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
        res = sb.table("casino_settings").select("*").eq("guild_id", int(guild_id)).limit(1).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
        return {
            "guild_id": int(guild_id),
            "house_company_id": None,
            "max_bet": 1000,
            "fee_bps": 0,
            "enabled": True,
        }

    def _upsert_settings(self, sb, guild_id: int, patch: dict):
        row = {"guild_id": int(guild_id), **patch}
        sb.table("casino_settings").upsert(row, on_conflict="guild_id").execute()

    # ✅ SAFE ENSURE: does not overwrite balance
    def _ensure_company_wallet(self, sb, company_id: str, currency_id: str):
        existing = (
            sb.table("company_wallets")
            .select("company_id")
            .eq("company_id", company_id)
            .eq("currency_id", currency_id)
            .limit(1)
            .execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            return

        sb.table("company_wallets").insert(
            {"company_id": company_id, "currency_id": currency_id, "balance": 0}
        ).execute()

    def _get_company_balance(self, sb, company_id: str, currency_id: str) -> int:
        res = (
            sb.table("company_wallets")
            .select("balance")
            .eq("company_id", company_id)
            .eq("currency_id", currency_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return int(rows[0].get("balance") or 0) if rows else 0

    def _get_company_name(self, sb, company_id: str) -> str:
        res = sb.table("companies").select("name").eq("company_id", company_id).limit(1).execute()
        rows = getattr(res, "data", None) or []
        return str(rows[0].get("name") or "House") if rows else "House"

    async def _bank_autocomplete(self, interaction: discord.Interaction, current: str):
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

    def _calc_fee(self, bet: int, fee_bps: int) -> int:
        if bet <= 0 or fee_bps <= 0:
            return 0
        return int((bet * fee_bps) // 10000)

    def _window_since_iso(self, days: int) -> str:
        days = int(days)
        if days < 1:
            days = 1
        if days > 90:
            days = 90
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return since.isoformat()

    def _fetch_casino_company_txs(
        self,
        sb,
        *,
        guild_id: int,
        currency_id: str,
        house_id: str,
        since_iso: str,
        limit: int = 1000,
    ):
        """
        Pull only casino-related company transactions for this house.
        NOTE: your table does not have tx_id, so we don't select it.
        """
        res = (
            sb.table("company_transactions")
            .select(
                "tx_type,amount,from_company_id,to_company_id,from_character_id,to_character_id,reason,created_at,actor_discord_id"
            )
            .eq("guild_id", int(guild_id))
            .eq("currency_id", currency_id)
            .gte("created_at", since_iso)
            .ilike("reason", "casino %")
            .or_(f"from_company_id.eq.{house_id},to_company_id.eq.{house_id}")
            .order("created_at", desc=True)
            .limit(int(limit))
            .execute()
        )
        return getattr(res, "data", None) or []

    def _resolve_character_names(self, sb, char_ids: list[str]) -> dict[str, str]:
        if not char_ids:
            return {}
        cres = sb.table("characters").select("character_id,name").in_("character_id", char_ids).execute()
        rows = getattr(cres, "data", None) or []
        out: dict[str, str] = {}
        for r in rows:
            out[str(r["character_id"])] = str(r.get("name") or "Unknown")
        return out

    # ─────────────────────────────────────────────────────────────
    # Stats aggregation helpers
    # ─────────────────────────────────────────────────────────────

    def _aggregate_stats(self, txs: list[dict], *, house_id: str) -> tuple[int, int, int, dict[str, int]]:
        deposits_into_house = 0
        withdrawals_from_house = 0
        bet_count = 0
        net_by_char: dict[str, int] = {}

        for t in txs:
            tx_type = str(t.get("tx_type") or "").upper()
            amt = int(t.get("amount") or 0)

            from_company = t.get("from_company_id")
            to_company = t.get("to_company_id")
            from_char = t.get("from_character_id")
            to_char = t.get("to_character_id")

            if tx_type == "DEPOSIT" and str(to_company) == str(house_id):
                deposits_into_house += amt
                bet_count += 1
                if from_char:
                    net_by_char[str(from_char)] = net_by_char.get(str(from_char), 0) - amt

            elif tx_type == "WITHDRAW" and str(from_company) == str(house_id):
                withdrawals_from_house += amt
                if to_char:
                    net_by_char[str(to_char)] = net_by_char.get(str(to_char), 0) + amt

        return bet_count, deposits_into_house, withdrawals_from_house, net_by_char

    def _fmt_top(self, items: list[tuple[str, int]], name_by_char: dict[str, str], ticker: str, take: int = 5) -> str:
        if not items:
            return "—"
        out = []
        for cid, net in items[:take]:
            nm = name_by_char.get(cid, cid[:8])
            sign = "+" if net >= 0 else ""
            out.append(f"• **{nm}**: `{sign}{net}` {ticker}")
        return "\n".join(out)

    # ─────────────────────────────────────────────────────────────
    # Staff commands: settings
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="view_settings", description="Staff: View casino settings")
    async def view_settings(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        s = self._get_settings(sb, guild_id)

        house_id = s.get("house_company_id")
        house_name = self._get_company_name(sb, house_id) if house_id else "(not set)"

        embed = discord.Embed(title="🎰 Casino Settings", color=discord.Color.blurple())
        embed.add_field(name="Enabled", value="✅ Yes" if s.get("enabled") else "🛑 No", inline=True)
        embed.add_field(name="Max Bet", value=f"`{int(s.get('max_bet') or 0)}`", inline=True)
        embed.add_field(name="Fee (bps)", value=f"`{int(s.get('fee_bps') or 0)}`", inline=True)
        embed.add_field(name="House Bank", value=f"**{house_name}**", inline=False)
        embed.timestamp = discord.utils.utcnow()

        return await self._public(interaction, embed=embed)

    @app_commands.command(name="set_house", description="Staff: Set the casino House Bank")
    @app_commands.describe(bank="Which bank will act as the House")
    @app_commands.autocomplete(bank=_bank_autocomplete)
    async def set_house(self, interaction: discord.Interaction, bank: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = (
                sb.table("companies")
                .select("company_id,name")
                .eq("guild_id", guild_id)
                .eq("company_id", bank)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._private(interaction, "That bank wasn’t found in this server.")

            self._upsert_settings(sb, guild_id, {"house_company_id": bank})
            name = str(rows[0].get("name") or "House")

            ledger = discord.Embed(
                title="Ledger • Casino House Set",
                description=f"🎰 House Bank set to **{name}**",
                color=discord.Color.green(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public(interaction, content=f"✅ Casino house set to **{name}**.")

        except Exception as e:
            print(f"[casino set_house] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error setting house bank.")

    @app_commands.command(name="set_max_bet", description="Staff: Set the casino max bet")
    @app_commands.describe(amount="Maximum bet allowed")
    async def set_max_bet(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if amount <= 0:
            return await self._private(interaction, "Max bet must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        try:
            self._upsert_settings(sb, guild_id, {"max_bet": int(amount)})

            ledger = discord.Embed(
                title="Ledger • Casino Max Bet Updated",
                description=f"🎰 Max bet set to `{amount}`",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public(interaction, content=f"✅ Casino max bet set to `{amount}`.")
        except Exception as e:
            print(f"[casino set_max_bet] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error setting max bet.")

    @app_commands.command(name="set_fee", description="Staff: Set the casino fee (basis points)")
    @app_commands.describe(fee_bps="Fee in basis points (100=1%, 250=2.5%, 0=none)")
    async def set_fee(self, interaction: discord.Interaction, fee_bps: int):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if fee_bps < 0 or fee_bps > 5000:
            return await self._private(interaction, "Fee must be between 0 and 5000 bps (0%–50%).")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        try:
            self._upsert_settings(sb, guild_id, {"fee_bps": int(fee_bps)})

            ledger = discord.Embed(
                title="Ledger • Casino Fee Updated",
                description=f"🎰 Fee set to `{fee_bps}` bps",
                color=discord.Color.blue(),
            )
            ledger.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public(interaction, content=f"✅ Casino fee set to `{fee_bps}` bps.")
        except Exception as e:
            print(f"[casino set_fee] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error setting fee.")

    # ─────────────────────────────────────────────────────────────
    # Public: House Balance
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="house_balance", description="Show the House Bank balance (primary currency)")
    async def house_balance(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not s.get("enabled"):
                return await self._private(interaction, "Casino is disabled.")

            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set. Staff: `/casino set_house`")

            cur = get_primary_currency(sb, guild_id)
            self._ensure_company_wallet(sb, house_id, cur["currency_id"])

            bal = self._get_company_balance(sb, house_id, cur["currency_id"])
            name = self._get_company_name(sb, house_id)
            emoji = cur.get("emoji") or ""

            embed = discord.Embed(
                title="🎰 House Bank • Balance",
                description=f"🏦 **{name}**\n{emoji} **{cur['name']}**: `{bal}`",
                color=discord.Color.dark_teal(),
            )
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[casino house_balance] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error getting house balance.")

    # ─────────────────────────────────────────────────────────────
    # NEW: /casino my_stats
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="my_stats", description="Show your active OC's casino stats")
    @app_commands.describe(days="How many days back to look (default 7, max 90)")
    async def my_stats(self, interaction: discord.Interaction, days: int = 7):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not s.get("enabled"):
                return await self._private(interaction, "Casino is disabled.")

            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set. Staff: `/casino set_house`")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            player = get_active_character(sb, actor_id)
            if not player:
                return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")

            since_iso = self._window_since_iso(days)
            txs = self._fetch_casino_company_txs(
                sb, guild_id=guild_id, currency_id=currency_id, house_id=house_id, since_iso=since_iso, limit=1000
            )

            char_id = str(player["character_id"])

            bets = 0
            wagered = 0
            received = 0

            for t in txs:
                tx_type = str(t.get("tx_type") or "").upper()
                amt = int(t.get("amount") or 0)
                from_char = str(t.get("from_character_id") or "")
                to_char = str(t.get("to_character_id") or "")
                to_company = str(t.get("to_company_id") or "")
                from_company = str(t.get("from_company_id") or "")

                if tx_type == "DEPOSIT" and to_company == str(house_id) and from_char == char_id:
                    bets += 1
                    wagered += amt
                elif tx_type == "WITHDRAW" and from_company == str(house_id) and to_char == char_id:
                    received += amt

            net = received - wagered

            embed = discord.Embed(
                title="🎰 Casino • My Stats",
                description=f"**{player['name']}** • Window: last `{min(max(int(days),1),90)}` day(s)",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Plays", value=f"`{bets}`", inline=True)
            embed.add_field(name="Wagered", value=f"{emoji} `{wagered}`", inline=True)
            embed.add_field(name="Received", value=f"{emoji} `{received}`", inline=True)
            embed.add_field(name="Net", value=f"{emoji} `{net}` {ticker}", inline=False)
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[casino my_stats] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error generating your stats.")

    # ─────────────────────────────────────────────────────────────
    # NEW: /casino leaderboard
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="Show casino leaderboard (top winners/losers)")
    @app_commands.describe(days="How many days back to look (default 7, max 90)")
    async def leaderboard(self, interaction: discord.Interaction, days: int = 7):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not s.get("enabled"):
                return await self._private(interaction, "Casino is disabled.")

            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set. Staff: `/casino set_house`")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            since_iso = self._window_since_iso(days)
            txs = self._fetch_casino_company_txs(
                sb, guild_id=guild_id, currency_id=currency_id, house_id=house_id, since_iso=since_iso, limit=1000
            )

            _, wagered, paid_out, net_by_char = self._aggregate_stats(txs, house_id=house_id)

            name_by_char = self._resolve_character_names(sb, list(net_by_char.keys()))
            winners = sorted(net_by_char.items(), key=lambda kv: kv[1], reverse=True)
            losers = sorted(net_by_char.items(), key=lambda kv: kv[1])

            embed = discord.Embed(
                title="🏆 Casino Leaderboard",
                description=f"Window: last `{min(max(int(days),1),90)}` day(s) • {emoji} **{ticker}**",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Total Wagered", value=f"{emoji} `{wagered}`", inline=True)
            embed.add_field(name="Total Paid Out", value=f"{emoji} `{paid_out}`", inline=True)
            embed.add_field(name="Top Winners", value=self._fmt_top(winners, name_by_char, ticker, 10), inline=False)
            embed.add_field(name="Top Losers", value=self._fmt_top(losers, name_by_char, ticker, 10), inline=False)
            embed.timestamp = discord.utils.utcnow()

            if len(txs) >= 1000:
                embed.set_footer(text="Note: results capped at 1000 rows. Narrow the date range if needed.")

            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[casino leaderboard] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error generating leaderboard.")

    # ─────────────────────────────────────────────────────────────
    # NEW: /casino audit (staff-only, ephemeral)
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="audit", description="Staff: Show recent casino transactions (human-readable)")
    @app_commands.describe(limit="How many rows (default 20, max 50)", days="Lookback window in days (default 7, max 90)")
    async def audit(self, interaction: discord.Interaction, limit: int = 20, days: int = 7):
        await interaction.response.defer(ephemeral=True)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            s = self._get_settings(sb, guild_id)
            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set.")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"

            limit = int(limit)
            if limit < 1:
                limit = 1
            if limit > 50:
                limit = 50

            since_iso = self._window_since_iso(days)
            txs = self._fetch_casino_company_txs(
                sb, guild_id=guild_id, currency_id=currency_id, house_id=house_id, since_iso=since_iso, limit=limit
            )

            if not txs:
                return await self._public(interaction, content="No casino transactions found in that window.", ephemeral=True)

            char_ids = set()
            for t in txs:
                if t.get("from_character_id"):
                    char_ids.add(str(t["from_character_id"]))
                if t.get("to_character_id"):
                    char_ids.add(str(t["to_character_id"]))
            name_by_char = self._resolve_character_names(sb, list(char_ids))

            house_name = self._get_company_name(sb, house_id)

            lines = []
            for t in txs:
                tx_type = str(t.get("tx_type") or "").upper()
                amt = int(t.get("amount") or 0)
                ts = str(t.get("created_at") or "")
                reason = str(t.get("reason") or "")

                fc = t.get("from_character_id")
                tc = t.get("to_character_id")

                from_name = name_by_char.get(str(fc), str(fc)[:8]) if fc else "-"
                to_name = name_by_char.get(str(tc), str(tc)[:8]) if tc else "-"

                if tx_type == "DEPOSIT":
                    lines.append(f"`{ts[:19]}` DEPOSIT  {emoji}{amt}  **{from_name}** → **{house_name}**  • {reason}")
                elif tx_type == "WITHDRAW":
                    lines.append(f"`{ts[:19]}` WITHDRAW {emoji}{amt}  **{house_name}** → **{to_name}**  • {reason}")
                else:
                    lines.append(f"`{ts[:19]}` {tx_type:<7} {emoji}{amt}  • {reason}")

            text = "\n".join(lines)
            if len(text) > 3800:
                text = text[:3800] + "\n…(truncated)"

            embed = discord.Embed(
                title="🧾 Casino Audit",
                description=f"House: **{house_name}** • Currency: **{ticker}** • Showing `{len(txs)}` rows",
                color=discord.Color.dark_grey(),
            )
            embed.add_field(name="Recent Activity", value=text, inline=False)
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[casino audit] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error generating audit.")

    # ─────────────────────────────────────────────────────────────
    # Public game: Coinflip
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="coinflip", description="Flip a coin against the House (bet -> win 2x minus fee)")
    @app_commands.describe(bet="Your bet amount", side="heads or tails")
    async def coinflip(self, interaction: discord.Interaction, bet: int, side: str):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if bet <= 0:
            return await self._private(interaction, "Bet must be > 0.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not s.get("enabled"):
                return await self._private(interaction, "Casino is disabled.")
            max_bet = int(s.get("max_bet") or 0)
            if max_bet > 0 and bet > max_bet:
                return await self._private(interaction, f"Bet too high. Max bet is `{max_bet}`.")

            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set. Staff: `/casino set_house`")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"
            fee_bps = int(s.get("fee_bps") or 0)
            fee = self._calc_fee(bet, fee_bps)

            player = get_active_character(sb, actor_id)
            if not player:
                return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, player["character_id"], currency_id)
            self._ensure_company_wallet(sb, house_id, currency_id)

            apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=currency_id,
                tx_type="DEPOSIT",
                amount=int(bet),
                actor_discord_id=actor_id,
                from_character_id=player["character_id"],
                to_company_id=house_id,
                reason=f"casino coinflip bet={bet} side={side}",
            )

            pick = (side or "").lower().strip()
            if pick not in ("heads", "tails"):
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="WITHDRAW",
                    amount=int(bet),
                    actor_discord_id=actor_id,
                    from_company_id=house_id,
                    to_character_id=player["character_id"],
                    reason="casino coinflip invalid side refund",
                )
                return await self._private(interaction, "Side must be `heads` or `tails`.")

            result = "heads" if secrets.randbelow(2) == 0 else "tails"
            win = (pick == result)

            payout = max(0, int(bet * 2 - fee))
            profit = payout - bet

            house_bal = self._get_company_balance(sb, house_id, currency_id)
            if win and payout > house_bal:
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="WITHDRAW",
                    amount=int(bet),
                    actor_discord_id=actor_id,
                    from_company_id=house_id,
                    to_character_id=player["character_id"],
                    reason="casino coinflip refund (house insolvent)",
                )
                return await self._private(interaction, "House bank can’t cover the payout right now. Bet refunded.")

            if win and payout > 0:
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="WITHDRAW",
                    amount=int(payout),
                    actor_discord_id=actor_id,
                    from_company_id=house_id,
                    to_character_id=player["character_id"],
                    reason=f"casino coinflip WIN result={result} bet={bet} fee={fee}",
                )

            house_name = self._get_company_name(sb, house_id)

            if win:
                msg = (
                    f"🪙 **{player['name']}** flipped **{result}** and WON!\n"
                    f"Bet: {emoji} `{bet}` • Payout: {emoji} `{payout}` • Profit: {emoji} `{profit}`"
                )
            else:
                msg = (
                    f"🪙 **{player['name']}** flipped **{result}** and lost.\n"
                    f"Lost: {emoji} `{bet}`"
                )

            ledger = discord.Embed(
                title="Ledger • Casino Coinflip",
                description=f"{emoji} **{ticker}** • House: **{house_name}**",
                color=discord.Color.green() if win else discord.Color.red(),
            )
            ledger.add_field(name="Player", value=f"**{player['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Pick → Result", value=f"`{pick}` → `{result}`", inline=True)
            ledger.add_field(name="Bet", value=f"`{bet}`", inline=True)
            if win:
                ledger.add_field(name="Payout", value=f"`{payout}`", inline=True)
                if fee:
                    ledger.add_field(name="Fee", value=f"`{fee}` ({fee_bps} bps)", inline=True)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private(interaction, "❌ Not enough funds for that bet.")
            raise
        except Exception as e:
            print(f"[casino coinflip] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error running coinflip.")

    # ─────────────────────────────────────────────────────────────
    # Public game: Roll
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="roll", description="Roll 1–100 against a target (higher target = safer, lower payout)")
    @app_commands.describe(bet="Your bet amount", target="Win if roll >= target (2..99). Default 55")
    async def roll(self, interaction: discord.Interaction, bet: int, target: int = 55):
        await interaction.response.defer()

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if bet <= 0:
            return await self._private(interaction, "Bet must be > 0.")

        target = int(target)
        if target < 2 or target > 99:
            return await self._private(interaction, "Target must be between 2 and 99.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            s = self._get_settings(sb, guild_id)
            if not s.get("enabled"):
                return await self._private(interaction, "Casino is disabled.")
            max_bet = int(s.get("max_bet") or 0)
            if max_bet > 0 and bet > max_bet:
                return await self._private(interaction, f"Bet too high. Max bet is `{max_bet}`.")

            house_id = s.get("house_company_id")
            if not house_id:
                return await self._private(interaction, "House bank is not set. Staff: `/casino set_house`")

            cur = get_primary_currency(sb, guild_id)
            currency_id = cur["currency_id"]
            emoji = cur.get("emoji") or ""
            ticker = cur.get("ticker") or cur.get("name") or "CUR"
            fee_bps = int(s.get("fee_bps") or 0)
            fee = self._calc_fee(bet, fee_bps)

            player = get_active_character(sb, actor_id)
            if not player:
                return await self._private(interaction, "No active OC set. Use `/oc select <name>`.")

            ensure_wallet(sb, player["character_id"], currency_id)
            self._ensure_company_wallet(sb, house_id, currency_id)

            apply_company_transaction(
                sb,
                guild_id=guild_id,
                currency_id=currency_id,
                tx_type="DEPOSIT",
                amount=int(bet),
                actor_discord_id=actor_id,
                from_character_id=player["character_id"],
                to_company_id=house_id,
                reason=f"casino roll bet={bet} target={target}",
            )

            roll = secrets.randbelow(100) + 1
            win = roll >= target

            denom = (101 - target)
            mult = 100 / denom
            payout = max(0, int(bet * mult) - fee)
            profit = payout - bet

            house_bal = self._get_company_balance(sb, house_id, currency_id)
            if win and payout > house_bal:
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="WITHDRAW",
                    amount=int(bet),
                    actor_discord_id=actor_id,
                    from_company_id=house_id,
                    to_character_id=player["character_id"],
                    reason="casino roll refund (house insolvent)",
                )
                return await self._private(interaction, "House bank can’t cover the payout right now. Bet refunded.")

            if win and payout > 0:
                apply_company_transaction(
                    sb,
                    guild_id=guild_id,
                    currency_id=currency_id,
                    tx_type="WITHDRAW",
                    amount=int(payout),
                    actor_discord_id=actor_id,
                    from_company_id=house_id,
                    to_character_id=player["character_id"],
                    reason=f"casino roll WIN roll={roll} target={target} bet={bet} fee={fee}",
                )

            house_name = self._get_company_name(sb, house_id)

            if win:
                msg = (
                    f"🎲 **{player['name']}** rolled `{roll}` vs target `{target}` and WON!\n"
                    f"Bet: {emoji} `{bet}` • Payout: {emoji} `{payout}` • Profit: {emoji} `{profit}`"
                )
            else:
                msg = (
                    f"🎲 **{player['name']}** rolled `{roll}` vs target `{target}` and lost.\n"
                    f"Lost: {emoji} `{bet}`"
                )

            ledger = discord.Embed(
                title="Ledger • Casino Roll",
                description=f"{emoji} **{ticker}** • House: **{house_name}**",
                color=discord.Color.green() if win else discord.Color.red(),
            )
            ledger.add_field(name="Player", value=f"**{player['name']}** (`{interaction.user}`)", inline=False)
            ledger.add_field(name="Roll / Target", value=f"`{roll}` / `{target}`", inline=True)
            ledger.add_field(name="Bet", value=f"`{bet}`", inline=True)
            if win:
                ledger.add_field(name="Payout", value=f"`{payout}`", inline=True)
                ledger.add_field(name="Multiplier", value=f"`{mult:.2f}x`", inline=True)
                if fee:
                    ledger.add_field(name="Fee", value=f"`{fee}` ({fee_bps} bps)", inline=True)
            ledger.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, ledger)

            return await self._public(interaction, content=msg)

        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_FUNDS":
                return await self._private(interaction, "❌ Not enough funds for that bet.")
            raise
        except Exception as e:
            print(f"[casino roll] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error running roll.")


async def setup(bot: commands.Bot):
    await bot.add_cog(CasinoCog(bot))