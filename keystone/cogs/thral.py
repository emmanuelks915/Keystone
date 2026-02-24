# cogs/thral.py
from __future__ import annotations

import os
import json
import datetime
import discord
from typing import Optional, List
from discord import app_commands
from discord.ext import commands, tasks

from config.skyfall import AP_EMOJI
from services.ap_service import get_ap

# ---------- emoji constants ----------
THRAL_EMOJI = "<:thral:1388999536143499477>"
TOKEN_EMOJI = "<:token:1447676379536691201>"

# ---------- guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# ---------- payroll config ----------
IMPERIAL_RECRUIT_ROLE_ID = 1389006716443693157

RANK_PAY_RATES = {
    "Imperial Recruit": 10,
    "Imperial Squire": 20,
    "Imperial Knight": 50,
    "Imperial General": 100,
    "Captain": 180,
    "Lieutenant": 125,
}

PAYROLL_ANNOUNCE_CHANNEL_ID = 1376239618332037241
PAYROLL_PING_ROLE_ID = 1374730886376984632


# ---------- permission config (multi-role + superusers) ----------
def _parse_id_set(val: str) -> set[int]:
    out = set()
    for chunk in (val or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            pass
    return out


THRALL_STAFF_ROLE_IDS: set[int] = _parse_id_set(
    os.getenv(
        "THRALL_STAFF_ROLE_IDS",
        "1374730886507139076,1381086606261223545,"
        "1374730886507139075,1374730886507139072,"
        "1374730886507139074,1374730886507139073,"
        "1374730886490357828",
    )
)
THRALL_SUPERUSER_IDS: set[int] = _parse_id_set(os.getenv("THRALL_SUPERUSER_IDS", ""))


def can_manage_thral(member: discord.Member) -> bool:
    if member.id in THRALL_SUPERUSER_IDS:
        return True
    return any(r.id in THRALL_STAFF_ROLE_IDS for r in getattr(member, "roles", []))


class Thral(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.auto_payroll.start()

    # ---------- helpers ----------
    def _supabase(self):
        """Use the shared Supabase client attached in bot.py."""
        return self.bot.supabase

    def _validate_oc_name(self, oc_name: str) -> tuple[bool, str]:
        if not oc_name or not isinstance(oc_name, str):
            return False, ""
        cleaned = oc_name.strip()
        if not cleaned:
            return False, ""
        if len(cleaned) > 100:
            return False, cleaned
        return True, cleaned

    async def _resolve_oc_id(self, supabase, owner_discord_id: str, oc_name: str) -> Optional[str]:
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id")
                .eq("owner_discord_id", owner_discord_id)
                .eq("oc_name", oc_name)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0]["oc_id"] if rows else None
        except Exception as e:
            print(f"[_resolve_oc_id] Exception resolving OC {oc_name} for {owner_discord_id}: {e}")
            return None

    async def _resolve_default_oc_for_owner(self, supabase, owner_discord_id: str) -> Optional[tuple[str, str]]:
        """
        For payroll & balance fallback: pick a default OC for a given player.
        Currently: first OC row we find for that owner.
        Returns (oc_id, oc_name) or None if none exist.
        """
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name")
                .eq("owner_discord_id", owner_discord_id)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return None
            row = rows[0]
            return row["oc_id"], row.get("oc_name") or "Unknown OC"
        except Exception as e:
            print(f"[_resolve_default_oc_for_owner] Exception for owner {owner_discord_id}: {e}")
            return None

    async def _get_oc_by_id(self, supabase, oc_id: str) -> Optional[dict]:
        """
        Fetch a full OC record (including avatar_url) by oc_id.
        Returns dict with oc_id, oc_name, owner_discord_id, avatar_url or None.
        """
        try:
            res = (
                supabase.table("ocs")
                .select("oc_id, oc_name, owner_discord_id, avatar_url")
                .eq("oc_id", oc_id)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0] if rows else None
        except Exception as e:
            print(f"[_get_oc_by_id] Exception fetching OC {oc_id}: {e}")
            return None

    async def _get_thral_balance_for_oc(self, supabase, oc_id: str) -> int:
        try:
            w = (
                supabase.table("thral_wallets")
                .select("balance")
                .eq("oc_id", oc_id)
                .limit(1)
                .execute()
            )
            rows = getattr(w, "data", None) or []
            return int(rows[0]["balance"]) if rows else 0
        except Exception as e:
            print(f"[_get_thral_balance_for_oc] SELECT exception: {e}")
            return 0

    async def _get_token_balance_for_oc(self, supabase, oc_id: str) -> int:
        """
        Tokens are stored per OC (matching tokens.py), in token_wallets keyed by oc_id.
        """
        try:
            w = (
                supabase.table("token_wallets")
                .select("balance")
                .eq("oc_id", oc_id)
                .limit(1)
                .execute()
            )
            rows = getattr(w, "data", None) or []
            return int(rows[0]["balance"]) if rows else 0
        except Exception as e:
            print(f"[_get_token_balance_for_oc] SELECT exception: {e}")
            return 0

    async def _get_active_loan_summary_for_oc(self, supabase, oc_id: str) -> tuple[int, int]:
        """
        Returns (active_loan_count, total_remaining_balance)
        """
        try:
            res = (
                supabase.table("thral_loans")
                .select("remaining_balance")
                .eq("oc_id", oc_id)
                .eq("status", "active")
                .execute()
            )
            rows = getattr(res, "data", None) or []
            count = len(rows)
            total_remaining = sum(int(r.get("remaining_balance") or 0) for r in rows)
            return count, total_remaining
        except Exception as e:
            print(f"[_get_active_loan_summary_for_oc] SELECT exception: {e}")
            return 0, 0

    # ---------- OC AUTOCOMPLETE (GLOBAL) ----------
    async def _oc_autocomplete_global(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        """
        Global OC autocomplete: show any OC in the registry whose name matches `current`.
        This ignores ownership so everyone can see everyone's OCs in the dropdown.
        """
        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[oc_autocomplete_global] supabase access error: {e}")
            return []

        try:
            query = supabase.table("ocs").select("oc_name")
            if current:
                query = query.ilike("oc_name", f"%{current}%")
            res = query.order("oc_name").limit(25).execute()
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[oc_autocomplete_global] SELECT exception: {e}")
            return []

        choices: List[app_commands.Choice[str]] = []
        for row in rows:
            name = row.get("oc_name")
            if not name:
                continue
            choices.append(app_commands.Choice(name=name, value=name))
        return choices

    # ============================================================
    # ✅ consolidated balance command (Tokens + Thral + AP + Loans)
    # ============================================================
    @app_commands.command(name="balance", description="View an OC's Tokens + Thral + AP + Loans in one place")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="OC name (optional; defaults to your first OC)",
        player="Owner of the OC (optional; defaults to you)",
    )
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def balance(
        self,
        interaction: discord.Interaction,
        oc_name: Optional[str] = None,
        player: Optional[discord.User] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[balance] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        owner = player or interaction.user

        # Resolve OC
        if oc_name:
            oc_id = await self._resolve_oc_id(supabase, str(owner.id), oc_name.strip())
            if not oc_id:
                return await interaction.followup.send("OC not found.", ephemeral=False)
            oc_row = await self._get_oc_by_id(supabase, oc_id)
            display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
            avatar_url = oc_row.get("avatar_url") if oc_row else None
        else:
            oc_info = await self._resolve_default_oc_for_owner(supabase, str(owner.id))
            if not oc_info:
                return await interaction.followup.send(
                    "You don’t have a registered OC yet, so I can’t show balances.\n"
                    "Register an OC first, then try `/balance` again.",
                    ephemeral=False,
                )
            oc_id, display_name = oc_info
            oc_row = await self._get_oc_by_id(supabase, oc_id)
            avatar_url = oc_row.get("avatar_url") if oc_row else None

        # Fetch balances
        thral_bal = await self._get_thral_balance_for_oc(supabase, oc_id)
        token_bal = await self._get_token_balance_for_oc(supabase, oc_id)
        active_loan_count, total_remaining = await self._get_active_loan_summary_for_oc(supabase, oc_id)

        # AP (per OC) via service layer (oc_wallets.ap_balance)
        try:
            ap_bal = int(get_ap(oc_id))
        except Exception as e:
            print(f"[balance] AP lookup failed for oc_id={oc_id}: {e}")
            ap_bal = 0

        embed = discord.Embed(
            title="💰 Balance",
            description=f"OC **{display_name}** • Owner <@{owner.id}>",
            color=discord.Color.teal(),
        )

        embed.add_field(name=f"{TOKEN_EMOJI} Tokens", value=str(token_bal), inline=True)
        embed.add_field(name=f"{THRAL_EMOJI} Thral", value=str(thral_bal), inline=True)
        embed.add_field(name=f"{AP_EMOJI} AP", value=str(ap_bal), inline=True)

        if active_loan_count > 0:
            embed.add_field(
                name="📄 Loans",
                value=f"Active: **{active_loan_count}**\nOwed: **{total_remaining} {THRAL_EMOJI}**",
                inline=True,
            )
        else:
            embed.add_field(name="📄 Loans", value="None", inline=True)

        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.set_footer(text="Tokens/Thral/AP are per-OC. Loans shown are active Thral loans.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------- payroll core ----------
    async def _process_payroll_for_guild(
        self,
        supabase,
        guild: discord.Guild,
        month_label: Optional[str],
        by_user_id: Optional[int] = None,
    ) -> tuple[int, int, int]:
        """
        Core payroll logic used by both the /thral_payroll command and the auto scheduler.

        Returns: (member_count_paid, total_paid, skipped_no_oc)
        """
        total_paid = 0
        member_count = 0
        skipped_no_oc = 0

        for member in guild.members:
            if member.bot:
                continue

            # For now: only Imperial Recruit by role ID
            best_amount = 0
            best_role_name = None
            for role in member.roles:
                if role.id == IMPERIAL_RECRUIT_ROLE_ID:
                    amt = RANK_PAY_RATES.get("Imperial Recruit", 10)
                else:
                    continue

                if amt and amt > best_amount:
                    best_amount = amt
                    best_role_name = role.name

            if best_amount <= 0:
                continue  # no qualifying rank role

            oc_info = await self._resolve_default_oc_for_owner(supabase, str(member.id))
            if not oc_info:
                skipped_no_oc += 1
                print(f"[payroll] Skipping member {member.id} ({member.display_name}) - no OC found")
                continue

            oc_id, oc_name = oc_info
            ctx_by = str(by_user_id) if by_user_id is not None else "system"

            payload = {
                "p_oc_id": oc_id,
                "p_delta": best_amount,
                "p_reason": "payroll",
                "p_ctx": {
                    "by": ctx_by,
                    "rank_role": best_role_name,
                    "month": month_label or "",
                    "oc_name": oc_name,
                },
            }

            try:
                res = supabase.rpc("thral_adjust", payload).execute()
                if hasattr(res, "error") and res.error:
                    print(f"[payroll] RPC error for member {member.id}: {res.error}")
                    continue
            except Exception as e:
                print(f"[payroll] RPC exception for member {member.id}: {e}")
                continue

            member_count += 1
            total_paid += best_amount

            # Auto-garnish for active auto-mode loans
            try:
                loans_res = (
                    supabase.table("thral_loans")
                    .select("loan_id, remaining_balance, repayment_mode")
                    .eq("oc_id", oc_id)
                    .eq("status", "active")
                    .limit(1)
                    .execute()
                )
                loans_rows = getattr(loans_res, "data", None) or []
            except Exception as e:
                print(f"[payroll] loan lookup exception for member {member.id}: {e}")
                loans_rows = []

            if loans_rows:
                loan_row = loans_rows[0]
                if loan_row.get("repayment_mode", "manual") == "auto":
                    remaining_balance = loan_row.get("remaining_balance") or 0

                    raw_garnish = int(best_amount * 0.25)
                    garnish = min(raw_garnish, remaining_balance)

                    if garnish > 0:
                        repay_payload = {
                            "p_loan_id": loan_row["loan_id"],
                            "p_amount": garnish,
                            "p_ctx": {
                                "by": ctx_by,
                                "type": "auto_garnish",
                                "month": month_label or "",
                                "note": "Auto-garnished from monthly payroll",
                            },
                        }
                        try:
                            repay_res = supabase.rpc("thral_loan_repay", repay_payload).execute()
                            if hasattr(repay_res, "error") and repay_res.error:
                                print(f"[payroll] auto-garnish RPC error for member {member.id}: {repay_res.error}")
                        except Exception as e:
                            print(f"[payroll] auto-garnish RPC exception for member {member.id}: {e}")

        print(
            f"[payroll] Completed: members_paid={member_count}, total_paid={total_paid}, "
            f"skipped_no_oc={skipped_no_oc}"
        )
        return member_count, total_paid, skipped_no_oc

    # ---------- auto payroll scheduler ----------
    @tasks.loop(time=datetime.time(hour=5, minute=0, tzinfo=datetime.timezone.utc))  # ~midnight US Eastern
    async def auto_payroll(self):
        """Runs once per day at the configured time; only pays on the 1st of the month."""
        await self.bot.wait_until_ready()

        now = datetime.datetime.now(datetime.timezone.utc)
        if now.day != 1:
            return  # only run on the 1st

        guild = self.bot.get_guild(SKYFALL_GUILD_ID) or (self.bot.guilds[0] if self.bot.guilds else None)
        if not guild:
            print("[auto_payroll] No guilds found; skipping.")
            return

        month_label = now.strftime("%Y-%m")

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[auto_payroll] supabase access error: {e}")
            return

        member_count, total_paid, skipped_no_oc = await self._process_payroll_for_guild(
            supabase, guild, month_label, by_user_id=None
        )

        channel = self.bot.get_channel(PAYROLL_ANNOUNCE_CHANNEL_ID)
        if not channel:
            print(f"[auto_payroll] Could not find channel ID {PAYROLL_ANNOUNCE_CHANNEL_ID}")
            return

        mention = f"<@&{PAYROLL_PING_ROLE_ID}>"
        desc_lines = [
            f"Monthly wages have been automatically paid out for **{month_label}**.",
            f"Paid **{total_paid} {THRAL_EMOJI}** to **{member_count}** members.",
            "Use `/balance` to check your Tokens/Thral/AP.",
        ]
        if skipped_no_oc:
            desc_lines.append(
                f"Note: {skipped_no_oc} members with the rank role had no registered OC and were skipped."
            )

        public_embed = discord.Embed(
            title=f"{THRAL_EMOJI} Wages Have Been Paid",
            description="\n".join(desc_lines),
            color=discord.Color.gold(),
        )
        public_embed.set_footer(text=f"Period: {month_label} (auto payroll)")

        await channel.send(content=mention, embed=public_embed)

    @auto_payroll.before_loop
    async def before_auto_payroll(self):
        print("[auto_payroll] Waiting until bot is ready...")
        await self.bot.wait_until_ready()
        print("[auto_payroll] Loop started.")

    # ---------- staff: grant/revoke/fine ----------
    @app_commands.command(name="thral_grant", description="STAFF: grant Thral to an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(player="Owner of the OC", oc_name="Exact OC name", amount="Positive amount")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_grant(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        await interaction.response.defer(ephemeral=False)
        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_grant] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        payload = {
            "p_oc_id": oc_id,
            "p_delta": amount,
            "p_reason": "grant",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id)},
        }
        try:
            res = supabase.rpc("thral_adjust", payload).execute()
            if hasattr(res, "error") and res.error:
                return await interaction.followup.send(f"Database operation failed: `{res.error}`", ephemeral=False)
        except Exception as e:
            return await interaction.followup.send(f"Could not grant Thral (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(res, "data", None)
        if isinstance(data_container, list):
            data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            data = data_container
        else:
            data = {}

        new_bal = data.get("new_balance", "—")
        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Granted",
            description=(
                f"OC **{display_oc_name}** • Owner <@{player.id}>\n"
                f"Amount: **+{amount}** {THRAL_EMOJI}"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="New Balance", value=f"{new_bal} {THRAL_EMOJI}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_revoke", description="STAFF: revoke Thral from an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(player="Owner of the OC", oc_name="Exact OC name", amount="Positive amount")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_revoke(self, interaction: discord.Interaction, player: discord.User, oc_name: str, amount: int):
        await interaction.response.defer(ephemeral=False)
        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_revoke] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        payload = {
            "p_oc_id": oc_id,
            "p_delta": -abs(amount),
            "p_reason": "revoke",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id)},
        }
        try:
            res = supabase.rpc("thral_adjust", payload).execute()
            if hasattr(res, "error") and res.error:
                return await interaction.followup.send(f"Database operation failed: `{res.error}`", ephemeral=False)
        except Exception as e:
            return await interaction.followup.send(f"Could not revoke Thral (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(res, "data", None)
        if isinstance(data_container, list):
            data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            data = data_container
        else:
            data = {}

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Revoked",
            description=(f"OC **{display_oc_name}** • Owner <@{player.id}>\n" f"Amount: **-{amount}** {THRAL_EMOJI}"),
            color=discord.Color.red(),
        )
        embed.add_field(name="New Balance", value=f"{data.get('new_balance','—')} {THRAL_EMOJI}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_fine", description="STAFF: create a Thral fine (loan-style with interest)")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        player="Owner of the OC",
        oc_name="Exact OC name",
        amount="Base fine amount (principal)",
        interest_percent="Total interest over the full term (e.g. 10 = 10%)",
        term_months="Loan term in months (controls compounding, default 3)",
        note="Reason for the fine",
    )
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_fine(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        oc_name: str,
        amount: int,
        interest_percent: float,
        term_months: int = 3,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)

        if amount <= 0:
            return await interaction.followup.send("Fine amount must be positive.", ephemeral=False)

        if interest_percent < 0:
            return await interaction.followup.send("Interest percent cannot be negative.", ephemeral=False)

        if term_months <= 0:
            term_months = 1

        is_valid, clean_oc_name = self._validate_oc_name(oc_name)
        if not is_valid:
            return await interaction.followup.send("Invalid OC name provided.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_fine] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(player.id), clean_oc_name)
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_oc_name = oc_row.get("oc_name", clean_oc_name) if oc_row else clean_oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        total_interest_rate = max(0.0, float(interest_percent) / 100.0)
        try:
            interest_rate = (1.0 + total_interest_rate) ** (1.0 / float(term_months)) - 1.0
        except Exception as e:
            print(f"[thral_fine] interest math error: {e}")
            interest_rate = total_interest_rate / max(term_months, 1)

        fine_ctx = {"by": str(interaction.user.id), "msg": str(interaction.id), "type": "fine", "note": note or ""}

        loan_payload = {
            "p_oc_id": oc_id,
            "p_amount": amount,
            "p_interest_rate": interest_rate,
            "p_term_months": term_months,
            "p_repayment_mode": "manual",
            "p_ctx": fine_ctx,
        }

        try:
            loan_res = supabase.rpc("thral_loan_request_v2", loan_payload).execute()
            if hasattr(loan_res, "error") and loan_res.error:
                return await interaction.followup.send(
                    f"Could not create fine (database error): `{loan_res.error}`",
                    ephemeral=False,
                )
        except Exception as e:
            return await interaction.followup.send(f"Could not create fine (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(loan_res, "data", None)
        if isinstance(data_container, list):
            loan_data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            loan_data = data_container
        else:
            loan_data = {}

        loan_id = loan_data.get("new_loan_id")
        total_due = loan_data.get("total_due")
        if total_due is None:
            total_due = int(round(amount * (1.0 + total_interest_rate)))

        adjust_error: Optional[str] = None
        adjust_payload = {
            "p_oc_id": oc_id,
            "p_delta": -abs(amount),
            "p_reason": "fine_principal",
            "p_ctx": {**fine_ctx, "loan_id": loan_id},
        }
        try:
            adj_res = supabase.rpc("thral_adjust", adjust_payload).execute()
            if hasattr(adj_res, "error") and adj_res.error:
                adjust_error = str(adj_res.error)
        except Exception as e:
            adjust_error = str(e)

        extra_line = ""
        if adjust_error:
            extra_line = (
                "\n\n⚠️ **Warning:** The fine record was created, but I could not "
                "immediately remove the principal from the OC's wallet. "
                "Please check their balance and adjust manually if needed."
            )

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Fine Created",
            description=(
                f"OC **{display_oc_name}** • Owner <@{player.id}>\n"
                f"Base fine (principal): **{amount} {THRAL_EMOJI}**\n"
                f"Total interest (if full term): **{interest_percent:.1f}%**\n"
                f"Estimated total due: **{total_due} {THRAL_EMOJI}**\n"
                f"Term: **{term_months} month(s)**\n"
                f"Repayment mode: `manual`\n"
                f"Loan/Fine ID: `{loan_id}`\n"
                f"{'Reason: ' + note if note else ''}"
                f"{extra_line}"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="This fine behaves like a loan: it accrues interest until repaid via /thral_loan_repay.")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------- STAFF: monthly payroll (manual trigger) ----------
    @app_commands.command(name="thral_payroll", description="STAFF: process monthly Thral payroll based on rank roles")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(month_label="Optional label for this run (e.g. '2025-12' or 'December 2025')")
    async def thral_payroll(self, interaction: discord.Interaction, month_label: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=True)

        if interaction.guild is None:
            return await interaction.followup.send("This command can only be used in a server.", ephemeral=True)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_payroll] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=True)

        if not month_label:
            now = datetime.datetime.now(datetime.timezone.utc)
            month_label = now.strftime("%Y-%m")

        member_count, total_paid, skipped_no_oc = await self._process_payroll_for_guild(
            supabase, interaction.guild, month_label, by_user_id=interaction.user.id
        )

        desc_lines = [
            f"Processed payroll for **{member_count}** members.",
            f"Total paid: **{total_paid} {THRAL_EMOJI}**.",
        ]
        if skipped_no_oc:
            desc_lines.append(f"Skipped **{skipped_no_oc}** members with a rank role but no OC in the registry.")

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Monthly Thral Payroll",
            description="\n".join(desc_lines),
            color=discord.Color.blue(),
        )
        if month_label:
            embed.set_footer(text=f"Period: {month_label}")

        await interaction.followup.send(embed=embed, ephemeral=True)

        channel = interaction.guild.get_channel(PAYROLL_ANNOUNCE_CHANNEL_ID)
        if channel:
            mention = f"<@&{PAYROLL_PING_ROLE_ID}>"
            pub_desc_lines = [
                "Monthly wages have been paid.",
                f"Paid **{total_paid} {THRAL_EMOJI}** to **{member_count}** members.",
                "Use `/balance` to check your Tokens/Thral/AP.",
            ]
            if skipped_no_oc:
                pub_desc_lines.append(
                    f"Note: {skipped_no_oc} members with the rank role had no OC registered and were skipped."
                )

            pub_embed = discord.Embed(
                title=f"{THRAL_EMOJI} Wages Have Been Paid",
                description="\n".join(pub_desc_lines),
                color=discord.Color.gold(),
            )
            if month_label:
                pub_embed.set_footer(text=f"Period: {month_label} (manual run)")
            await channel.send(content=mention, embed=pub_embed)

    # ---------- players: pay / balance / history ----------
    @app_commands.command(name="thral_pay", description="Donate Thral from one of your OCs to another player's OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        from_oc="Your OC name (payer)",
        to_player="Recipient player",
        to_oc="Recipient OC name",
        amount="Positive amount to donate",
    )
    @app_commands.autocomplete(from_oc=_oc_autocomplete_global, to_oc=_oc_autocomplete_global)
    async def thral_pay(
        self,
        interaction: discord.Interaction,
        from_oc: str,
        to_player: discord.User,
        to_oc: str,
        amount: int,
    ):
        await interaction.response.defer(ephemeral=False)
        if amount <= 0:
            return await interaction.followup.send("Amount must be positive.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_pay] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        from_oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), from_oc.strip())
        if not from_oc_id:
            return await interaction.followup.send("Your payer OC was not found.", ephemeral=False)

        to_oc_id = await self._resolve_oc_id(supabase, str(to_player.id), to_oc.strip())
        if not to_oc_id:
            return await interaction.followup.send("Recipient OC was not found.", ephemeral=False)

        from_row = await self._get_oc_by_id(supabase, from_oc_id)
        to_row = await self._get_oc_by_id(supabase, to_oc_id)

        from_display = from_row.get("oc_name", from_oc) if from_row else from_oc
        to_display = to_row.get("oc_name", to_oc) if to_row else to_oc

        thumb_url = None
        if from_row and from_row.get("avatar_url"):
            thumb_url = from_row["avatar_url"]
        elif to_row and to_row.get("avatar_url"):
            thumb_url = to_row["avatar_url"]

        payload = {
            "p_from_oc": from_oc_id,
            "p_to_oc": to_oc_id,
            "p_amount": amount,
            "p_reason": "donation",
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id), "to": str(to_player.id)},
        }
        try:
            res = supabase.rpc("thral_transfer", payload).execute()
            if hasattr(res, "error") and res.error:
                return await interaction.followup.send(f"Database operation failed: `{res.error}`", ephemeral=False)
        except Exception as e:
            return await interaction.followup.send(f"Could not transfer Thral (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(res, "data", None)
        if isinstance(data_container, list):
            data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            data = data_container
        else:
            data = {}

        from_bal = data.get("from_new_balance", "—")
        to_bal = data.get("to_new_balance", "—")

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Donation",
            description=(
                f"**{from_display}** → **{to_display}**\n"
                f"Donor: <@{interaction.user.id}> • Recipient: <@{to_player.id}>\n"
                f"Amount: **{amount}** {THRAL_EMOJI}"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Your New Balance", value=f"{from_bal} {THRAL_EMOJI}")
        embed.add_field(name="Recipient New Balance", value=f"{to_bal} {THRAL_EMOJI}")
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_balance", description="Check an OC's Thral balance (legacy; use /balance)")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(oc_name="Your OC (default) or specify with player", player="Owner (optional)")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_balance(self, interaction: discord.Interaction, oc_name: str, player: Optional[discord.User] = None):
        await interaction.response.defer(ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_balance] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        owner = player or interaction.user
        oc_id = await self._resolve_oc_id(supabase, str(owner.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        bal = await self._get_thral_balance_for_oc(supabase, oc_id)

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Balance",
            description=f"OC **{display_name}** • Owner <@{owner.id}>",
            color=discord.Color.teal(),
        )
        embed.add_field(name="Balance", value=f"{bal} {THRAL_EMOJI}")
        embed.set_footer(text="Legacy command. Prefer /balance.")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_history", description="Show recent Thral transactions for an OC")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(oc_name="OC name", limit="Number of entries (max 25, default 10)")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_history(self, interaction: discord.Interaction, oc_name: str, limit: Optional[int] = 10):
        await interaction.response.defer(ephemeral=False)
        limit = max(1, min(25, limit or 10))

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_history] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        try:
            tx = (
                supabase.table("thral_tx")
                .select("delta, reason, created_at, ctx")
                .eq("oc_id", oc_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            rows = getattr(tx, "data", None) or []
        except Exception as e:
            print(f"[thral_history] SELECT exception: {e}")
            return await interaction.followup.send(f"Could not fetch history (DB error): `{e}`", ephemeral=False)

        if not rows:
            return await interaction.followup.send("No transactions yet.", ephemeral=False)

        embed = discord.Embed(title=f"📜 Thral History • {display_name}", color=discord.Color.dark_teal())
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        for r in rows:
            delta = r["delta"]
            sign = "＋" if delta > 0 else "－"
            when = r["created_at"]
            reason = r.get("reason", "txn")
            try:
                timestamp = int(discord.utils.parse_time(when).timestamp())
                time_str = f"<t:{timestamp}:R>"
            except (ValueError, TypeError, AttributeError) as e:
                print(f"[thral_history] Failed to parse timestamp '{when}': {e}")
                time_str = str(when)
            embed.add_field(name=f"{sign}{abs(delta)} {THRAL_EMOJI} • {reason}", value=time_str, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------- loans: request / balance / repay ----------
    @app_commands.command(name="thral_loan_request", description="Request a Thral loan from the central bank")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        oc_name="Your OC name",
        amount="Thral amount to borrow (must be positive, max 600)",
        term_months="Loan term in months (for flavor / tracking)",
        repayment_mode="Repayment mode: 'manual' or 'auto' (default manual)",
    )
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_loan_request(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        amount: int,
        term_months: int,
        repayment_mode: Optional[str] = "manual",
    ):
        await interaction.response.defer(ephemeral=False)

        if amount <= 0:
            return await interaction.followup.send("Loan amount must be positive.", ephemeral=False)
        if term_months <= 0:
            return await interaction.followup.send("Loan term must be at least 1 month.", ephemeral=False)
        if amount > 600:
            return await interaction.followup.send("You can borrow at most **600** Thral per loan.", ephemeral=False)

        mode = (repayment_mode or "manual").lower()
        if mode not in ("manual", "auto"):
            mode = "manual"

        if amount <= 300:
            total_interest_rate = 0.10
        else:
            total_interest_rate = 0.20

        try:
            interest_rate = (1.0 + total_interest_rate) ** (1.0 / float(term_months)) - 1.0
        except Exception as e:
            print(f"[thral_loan_request] interest rate math error: {e}")
            interest_rate = total_interest_rate / max(term_months, 1)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_loan_request] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found. Make sure you used the exact name.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        try:
            existing = (
                supabase.table("thral_loans")
                .select("loan_id, status, remaining_balance")
                .eq("oc_id", oc_id)
                .eq("status", "active")
                .limit(1)
                .execute()
            )
            existing_rows = getattr(existing, "data", None) or []
            if existing_rows:
                return await interaction.followup.send(
                    "This OC already has an active Thral loan.\nYou must repay or close the existing loan before taking another.",
                    ephemeral=False,
                )
        except Exception as e:
            print(f"[thral_loan_request] existing-loan check exception: {e}")
            return await interaction.followup.send(
                "Could not check existing loans (database error). Please try again later.",
                ephemeral=False,
            )

        payload = {
            "p_oc_id": oc_id,
            "p_amount": amount,
            "p_interest_rate": interest_rate,
            "p_term_months": term_months,
            "p_repayment_mode": mode,
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id), "type": "loan_request"},
        }

        try:
            res = supabase.rpc("thral_loan_request_v2", payload).execute()
            if hasattr(res, "error") and res.error:
                return await interaction.followup.send(f"Could not create loan (database error): `{res.error}`", ephemeral=False)
        except Exception as e:
            return await interaction.followup.send(f"Could not create loan (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(res, "data", None)
        if isinstance(data_container, list):
            data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            data = data_container
        else:
            data = {}

        loan_id = data.get("new_loan_id")
        if not loan_id:
            print(f"[thral_loan_request] Missing new_loan_id in RPC result: {getattr(res, '__dict__', res)}")
            return await interaction.followup.send("Loan created, but could not read loan ID from database.", ephemeral=False)

        total_rate_pct = int(total_interest_rate * 100)

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Loan Created",
            description=f"OC **{display_name}** has taken a loan from the Central Bank.\n_One active loan per OC, max 600 {THRAL_EMOJI} per loan._",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Amount", value=f"{amount} {THRAL_EMOJI}", inline=True)
        embed.add_field(name="Total Interest (if full term)", value=f"{total_rate_pct}%", inline=True)
        embed.add_field(name="Term", value=f"{term_months} months", inline=True)
        embed.add_field(name="Repayment Mode", value=mode, inline=True)
        embed.add_field(name="Loan ID", value=str(loan_id), inline=False)
        embed.set_footer(text="Paying your loan early reduces the total interest you pay.")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_loan_balance", description="View active Thral loans for one of your OCs")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(oc_name="Your OC name")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_loan_balance(self, interaction: discord.Interaction, oc_name: str):
        await interaction.response.defer(ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_loan_balance] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        try:
            res = (
                supabase.table("thral_loans")
                .select(
                    "loan_id, principal, total_due, remaining_balance, "
                    "interest_rate, term_months, status, repayment_mode, "
                    "created_at, updated_at"
                )
                .eq("oc_id", oc_id)
                .eq("status", "active")
                .order("created_at", desc=True)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[thral_loan_balance] SELECT exception: {e}")
            return await interaction.followup.send(f"Could not fetch loan data (DB error): `{e}`", ephemeral=False)

        if not rows:
            return await interaction.followup.send(f"OC **{display_name}** has no active loans.", ephemeral=False)

        embed = discord.Embed(title=f"📄 Active Thral Loans • {display_name}", color=discord.Color.gold())
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        for loan in rows:
            lid = loan.get("loan_id")
            principal = loan.get("principal") or 0
            total_due = loan.get("total_due") or 0
            remaining = loan.get("remaining_balance") or 0
            rate = loan.get("interest_rate") or 0
            term = loan.get("term_months") or 0
            mode = loan.get("repayment_mode", "manual")
            created_at = loan.get("created_at")

            if principal and total_due:
                try:
                    eff_rate_pct = (total_due / principal - 1.0) * 100.0
                except ZeroDivisionError:
                    eff_rate_pct = rate * 100.0 * max(term, 1)
            else:
                eff_rate_pct = rate * 100.0 * max(term, 1)

            name = f"Loan {str(lid)[:8]}…"
            value_lines = [
                f"Principal: **{principal} {THRAL_EMOJI}**",
                f"Total (if full term): **{total_due} {THRAL_EMOJI}**",
                f"Remaining Now: **{remaining} {THRAL_EMOJI}**",
                f"Effective Interest (full term): **{eff_rate_pct:.1f}%**",
                f"Term: **{term} mo** • Mode: `{mode}`",
            ]
            if created_at:
                value_lines.append(f"Started: `{created_at}`")
            embed.add_field(name=name, value="\n".join(value_lines), inline=False)

        embed.set_footer(text="Paying early reduces total interest owed. 'Remaining Now' is what you currently owe.")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(name="thral_loan_repay", description="Repay an active Thral loan for one of your OCs")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(oc_name="Your OC name", amount="Amount of Thral to pay toward the oldest active loan")
    @app_commands.autocomplete(oc_name=_oc_autocomplete_global)
    async def thral_loan_repay(self, interaction: discord.Interaction, oc_name: str, amount: int):
        await interaction.response.defer(ephemeral=False)

        if amount <= 0:
            return await interaction.followup.send("Repayment amount must be positive.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_loan_repay] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        oc_id = await self._resolve_oc_id(supabase, str(interaction.user.id), oc_name.strip())
        if not oc_id:
            return await interaction.followup.send("OC not found.", ephemeral=False)

        oc_row = await self._get_oc_by_id(supabase, oc_id)
        display_name = oc_row.get("oc_name", oc_name) if oc_row else oc_name
        avatar_url = oc_row.get("avatar_url") if oc_row else None

        try:
            res = (
                supabase.table("thral_loans")
                .select("loan_id, remaining_balance, total_due, interest_rate, term_months, status")
                .eq("oc_id", oc_id)
                .eq("status", "active")
                .order("created_at")
                .limit(1)
                .execute()
            )
            loans = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[thral_loan_repay] SELECT loan exception: {e}")
            return await interaction.followup.send(f"Could not fetch loan (DB error): `{e}`", ephemeral=False)

        if not loans:
            return await interaction.followup.send(f"OC **{display_name}** has no active loans to repay.", ephemeral=False)

        loan = loans[0]
        loan_id = loan["loan_id"]

        payload = {
            "p_loan_id": loan_id,
            "p_amount": amount,
            "p_ctx": {"by": str(interaction.user.id), "msg": str(interaction.id)},
        }

        try:
            res = supabase.rpc("thral_loan_repay", payload).execute()
            if hasattr(res, "error") and res.error:
                return await interaction.followup.send(f"Could not apply repayment (database error): `{res.error}`", ephemeral=False)
        except Exception as e:
            return await interaction.followup.send(f"Could not apply repayment (RPC exception): `{e}`", ephemeral=False)

        data_container = getattr(res, "data", None)
        if isinstance(data_container, list):
            data = data_container[0] if data_container else {}
        elif isinstance(data_container, dict):
            data = data_container
        else:
            data = {}

        amount_paid = data.get("amount_paid", amount)
        remaining = data.get("remaining_balance", 0)
        wallet_after = data.get("new_wallet_balance", "—")
        bank_balance = data.get("bank_balance", "—")
        status = data.get("status", "active")

        embed = discord.Embed(
            title=f"{THRAL_EMOJI} Thral Loan Repayment",
            description=(
                f"OC **{display_name}** • <@{interaction.user.id}>\n"
                f"Paid: **{amount_paid} {THRAL_EMOJI}** toward loan `{str(loan_id)[:8]}…`"
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="Remaining Balance", value=f"{remaining} {THRAL_EMOJI}", inline=True)
        embed.add_field(name="Your Wallet After", value=f"{wallet_after} {THRAL_EMOJI}", inline=True)
        embed.add_field(name="Bank Balance", value=f"{bank_balance} {THRAL_EMOJI}", inline=True)
        embed.set_footer(text=f"Loan status: {status}")
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------- STAFF: global loan view ----------
    @app_commands.command(name="thral_loans_all", description="STAFF: view a summary of Thral loans across all OCs")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(
        status_filter="Loan status filter: 'active' (default) or 'all'",
        limit="Max number of loans to display (1-50, default 25)",
    )
    async def thral_loans_all(self, interaction: discord.Interaction, status_filter: Optional[str] = "active", limit: Optional[int] = 25):
        await interaction.response.defer(ephemeral=True)

        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=True)

        limit = max(1, min(50, limit or 25))

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_loans_all] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=True)

        try:
            query = (
                supabase.table("thral_loans")
                .select(
                    "loan_id, oc_id, principal, total_due, remaining_balance, "
                    "interest_rate, term_months, status, repayment_mode, created_at"
                )
                .order("created_at", desc=True)
                .limit(limit)
            )
            sf = (status_filter or "active").lower()
            if sf != "all":
                query = query.eq("status", "active")

            res = query.execute()
            loans = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[thral_loans_all] SELECT loans exception: {e}")
            return await interaction.followup.send(f"Could not fetch loans (DB error): `{e}`", ephemeral=True)

        if not loans:
            msg = "No loans found." if (status_filter or "active").lower() == "all" else "No active loans found."
            return await interaction.followup.send(msg, ephemeral=True)

        oc_ids = {row["oc_id"] for row in loans if row.get("oc_id")}
        oc_map = {}

        if oc_ids:
            try:
                oc_res = (
                    supabase.table("ocs")
                    .select("oc_id, oc_name, owner_discord_id")
                    .in_("oc_id", list(oc_ids))
                    .execute()
                )
                oc_rows = getattr(oc_res, "data", None) or []
                for r in oc_rows:
                    oc_map[r["oc_id"]] = {"name": r.get("oc_name") or "Unknown OC", "owner": r.get("owner_discord_id")}
            except Exception as e:
                print(f"[thral_loans_all] SELECT OCs exception: {e}")

        total_principal = sum((row.get("principal") or 0) for row in loans)
        total_remaining = sum((row.get("remaining_balance") or 0) for row in loans)

        sf_label = (status_filter or "active").lower()
        title = f"🏛️ All Thral Loans ({len(loans)})" if sf_label == "all" else f"🏛️ Active Thral Loans ({len(loans)})"

        embed = discord.Embed(title=title, color=discord.Color.gold())
        embed.description = (
            f"Total principal: **{total_principal} {THRAL_EMOJI}**\n"
            f"Total remaining balance: **{total_remaining} {THRAL_EMOJI}**"
        )

        for row in loans:
            lid = row.get("loan_id")
            oc_id = row.get("oc_id")
            principal = row.get("principal") or 0
            remaining = row.get("remaining_balance") or 0
            total_due = row.get("total_due") or 0
            status = row.get("status", "active")
            mode = row.get("repayment_mode", "manual")
            created_at = row.get("created_at")

            oc_info = oc_map.get(oc_id, {})
            oc_name = oc_info.get("name", "Unknown OC")
            owner_id = oc_info.get("owner")

            name = f"Loan {str(lid)[:8]}… • {oc_name}"
            if owner_id:
                name += f" • <@{owner_id}>"

            value_lines = [
                f"Principal: **{principal} {THRAL_EMOJI}**",
                f"Remaining: **{remaining} {THRAL_EMOJI}** / Total: **{total_due} {THRAL_EMOJI}**",
                f"Mode: `{mode}` • Status: `{status}`",
            ]
            if created_at:
                value_lines.append(f"Started: `{created_at}`")

            embed.add_field(name=name, value="\n".join(value_lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- bank: central Thral bank balance ----------
    @app_commands.command(name="thral_bank", description="STAFF: view the central Thral bank balance")
    @app_commands.guilds(SKYFALL_GUILD)
    async def thral_bank(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not isinstance(interaction.user, discord.Member) or not can_manage_thral(interaction.user):
            return await interaction.followup.send("❌ You don't have permission.", ephemeral=False)

        try:
            supabase = self._supabase()
        except Exception as e:
            print(f"[thral_bank] supabase access error: {e}")
            return await interaction.followup.send(f"Server configuration error: `{e}`", ephemeral=False)

        try:
            res = supabase.table("thral_bank").select("name, balance, updated_at").limit(1).execute()
            rows = getattr(res, "data", None) or []
        except Exception as e:
            print(f"[thral_bank] SELECT exception: {e}")
            return await interaction.followup.send(f"Could not fetch bank balance (DB error): `{e}`", ephemeral=False)

        if not rows:
            bank_name = "Central Bank"
            balance = 0
            updated_at = None
        else:
            row = rows[0]
            bank_name = row.get("name", "Central Bank")
            balance = row.get("balance", 0)
            updated_at = row.get("updated_at")

        embed = discord.Embed(
            title=f"🏛️ Central Thral Bank {THRAL_EMOJI}",
            description=f"**{bank_name}**",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Total Thral Held", value=f"{balance} {THRAL_EMOJI}", inline=False)

        if updated_at:
            embed.set_footer(text=f"Last updated: {updated_at}")

        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Thral(bot))
