# cogs/sp.py
from __future__ import annotations

from typing import Optional, List, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from services.db import get_supabase_client

# ---------- guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# ---------- permissions ----------
STATS_STAFF_ROLE_ID = 1374730886490357822  # stats staff role

# ---------- logging ----------
SP_LOG_CHANNEL_ID = 1456440906633707520

# ---------- SP config ----------
# Which stats are refundable during a reset:
REFUND_STAT_KEYS = [
    "dexterity",
    "reflexes",
    "strength",
    "durability",
    "mana",
    "magic_output",
    "magic_control",
    "luck",  # include luck in refund; remove if you don't want that
]

# Which stats players are allowed to allocate into:
ALLOCATABLE_STAT_KEYS = [
    "dexterity",
    "reflexes",
    "strength",
    "durability",
    "mana",
    "magic_output",
    "magic_control",
    "luck",
]


def _is_stats_staff(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(r.id == STATS_STAFF_ROLE_ID for r in interaction.user.roles)


def _clean_stat_key(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "_")


class SPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sb = get_supabase_client()

    # -------------------------
    # Logging helper
    # -------------------------
    async def _send_sp_log(
        self,
        *,
        interaction: discord.Interaction,
        title: str,
        oc_name: str,
        oc_id: str,
        owner_discord_id: str,
        fields: List[tuple[str, str, bool]],
        color: discord.Color = discord.Color.orange(),
    ) -> None:
        """Best-effort log to SP_LOG_CHANNEL_ID; never raise."""
        try:
            guild = interaction.guild
            if not guild:
                return

            channel = guild.get_channel(SP_LOG_CHANNEL_ID)
            if channel is None:
                # Try fetch if not cached
                try:
                    channel = await guild.fetch_channel(SP_LOG_CHANNEL_ID)
                except Exception:
                    return

            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return

            actor = interaction.user
            embed = discord.Embed(title=title, color=color)
            embed.add_field(name="OC", value=f"**{oc_name}**\n`{oc_id}`", inline=True)
            embed.add_field(name="Owner", value=f"<@{owner_discord_id}>\n`{owner_discord_id}`", inline=True)
            embed.add_field(name="Actor", value=f"{actor.mention}\n`{actor.id}`", inline=True)

            for n, v, inline in fields:
                embed.add_field(name=n, value=v, inline=inline)

            embed.set_footer(text=f"Guild: {interaction.guild_id} • Command: /sp")
            await channel.send(embed=embed)
        except Exception:
            return

    # -------------------------
    # Autocomplete: OC names
    # -------------------------
    async def oc_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        q = (current or "").strip()
        res = (
            self.sb.table("ocs")
            .select("oc_name")
            .ilike("oc_name", f"%{q}%")
            .order("oc_name")
            .limit(25)
            .execute()
        )
        names = [r["oc_name"] for r in (res.data or []) if r.get("oc_name")]
        seen = set()
        out: List[app_commands.Choice[str]] = []
        for n in names:
            key = n.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(app_commands.Choice(name=n, value=n))
        return out[:20]

    # -------------------------
    # DB helpers
    # -------------------------
    def _get_oc_by_name(self, oc_name: str) -> Dict:
        res = self.sb.table("ocs").select("*").eq("oc_name", oc_name).limit(1).execute()
        if not res.data:
            raise ValueError(f"OC not found: {oc_name}")
        return res.data[0]

    def _get_stats_row(self, oc_id: str) -> Dict:
        res = self.sb.table("oc_stats").select("*").eq("oc_id", oc_id).limit(1).execute()
        return res.data[0] if res.data else {"oc_id": oc_id}

    def _ensure_sp_wallet(self, oc_id: str) -> int:
        try:
            self.sb.table("oc_sp_wallets").upsert({"oc_id": oc_id}).execute()
        except Exception:
            pass

        res = (
            self.sb.table("oc_sp_wallets")
            .select("unallocated_sp")
            .eq("oc_id", oc_id)
            .limit(1)
            .execute()
        )
        return int(res.data[0]["unallocated_sp"]) if res.data else 0

    def _set_sp_wallet(self, oc_id: str, new_value: int) -> None:
        self.sb.table("oc_sp_wallets").upsert({"oc_id": oc_id, "unallocated_sp": int(new_value)}).execute()

    def _ensure_reset_wallet(self, oc_id: str) -> Tuple[int, int]:
        try:
            self.sb.table("oc_reset_wallets").upsert({"oc_id": oc_id}).execute()
        except Exception:
            pass

        res = (
            self.sb.table("oc_reset_wallets")
            .select("free_resets_total,free_resets_used")
            .eq("oc_id", oc_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return 1, 0
        return int(res.data[0]["free_resets_total"]), int(res.data[0]["free_resets_used"])

    def _log_sp(
        self,
        guild_id: str,
        oc_id: str,
        oc_name: str,
        owner_discord_id: str,
        action: str,
        amount: int,
        created_by_discord_id: str,
        stat_key: Optional[str] = None,
        reason: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> None:
        payload = {
            "guild_id": guild_id,
            "oc_id": oc_id,
            "oc_name": oc_name,
            "owner_discord_id": owner_discord_id,
            "action": action,
            "amount": int(amount),
            "stat_key": stat_key,
            "reason": reason,
            "created_by_discord_id": created_by_discord_id,
            "meta": meta or {},
        }
        self.sb.table("oc_sp_logs").insert(payload).execute()

    # -------------------------
    # Command group (GUILD LOCK APPLIED HERE)
    # -------------------------
    sp = app_commands.Group(name="sp", description="SP tools (grant, spend, balance, reset)")
    sp = app_commands.guilds(SKYFALL_GUILD)(sp)

    # -------------------------
    # /sp balance
    # -------------------------
    @sp.command(name="balance", description="View an OC's unallocated SP balance.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_balance(self, interaction: discord.Interaction, oc_name: str):
        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")

        is_staff = _is_stats_staff(interaction)
        if not is_staff and owner_id and owner_id != str(interaction.user.id):
            return await interaction.response.send_message("❌ You can only view SP for your own OC.", ephemeral=True)

        bal = self._ensure_sp_wallet(oc_id)
        total, used = self._ensure_reset_wallet(oc_id)
        free_left = max(0, total - used)

        await interaction.response.send_message(
            f"**{oc_name}**\n"
            f"🧠 **Unallocated SP:** `{bal}`\n"
            f"♻️ **Free Resets Remaining:** `{free_left}`",
            ephemeral=True,
        )

    # -------------------------
    # /sp grant (staff)
    # -------------------------
    @sp.command(name="grant", description="(Stats Staff) Grant SP to an OC (adds to unallocated SP pool).")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_grant(self, interaction: discord.Interaction, oc_name: str, amount: int, reason: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("❌ You don’t have permission to grant SP.", ephemeral=True)

        if amount <= 0:
            return await interaction.response.send_message("Amount must be > 0.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")
        guild_id = str(interaction.guild_id or SKYFALL_GUILD_ID)
        actor_id = str(interaction.user.id)

        current = self._ensure_sp_wallet(oc_id)
        new_val = current + int(amount)
        self._set_sp_wallet(oc_id, new_val)

        self._log_sp(
            guild_id=guild_id,
            oc_id=oc_id,
            oc_name=oc_name,
            owner_discord_id=owner_id,
            action="grant",
            amount=int(amount),
            created_by_discord_id=actor_id,
            reason=reason or "staff grant",
            meta={"old_balance": current, "new_balance": new_val},
        )

        await self._send_sp_log(
            interaction=interaction,
            title="🟩 SP Granted",
            oc_name=oc_name,
            oc_id=str(oc_id),
            owner_discord_id=owner_id or "unknown",
            fields=[
                ("Amount", f"`+{amount}`", True),
                ("Reason", reason or "staff grant", False),
                ("Balance", f"`{current} → {new_val}`", True),
            ],
            color=discord.Color.green(),
        )

        await interaction.followup.send(f"✅ Granted **{amount} SP** to **{oc_name}**. New unallocated SP: `{new_val}`")

    # -------------------------
    # /sp spend (player allocation)
    # -------------------------
    @sp.command(name="spend", description="Spend unallocated SP to increase a stat.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_spend(
        self,
        interaction: discord.Interaction,
        oc_name: str,
        stat: str,
        amount: int,
        note: Optional[str] = None,
    ):
        if amount <= 0:
            return await interaction.response.send_message("Amount must be > 0.", ephemeral=True)

        stat_key = _clean_stat_key(stat)
        if stat_key not in ALLOCATABLE_STAT_KEYS:
            return await interaction.response.send_message(
                f"Invalid stat. Options: {', '.join(ALLOCATABLE_STAT_KEYS)}",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")
        is_staff = _is_stats_staff(interaction)

        if not is_staff and owner_id and owner_id != str(interaction.user.id):
            return await interaction.followup.send("❌ You can only spend SP for your own OC.")

        current_sp = self._ensure_sp_wallet(oc_id)
        if current_sp < amount:
            return await interaction.followup.send(f"❌ Not enough SP. You have `{current_sp}`, need `{amount}`.")

        stats = self._get_stats_row(oc_id)
        old_val = int(stats.get(stat_key, 0) or 0)
        new_val = old_val + int(amount)

        # 1) Deduct SP
        new_sp = current_sp - int(amount)
        self._set_sp_wallet(oc_id, new_sp)

        # 2) Apply stat change
        self.sb.table("oc_stats").upsert({"oc_id": oc_id, stat_key: new_val}).execute()

        # 3) Log oc_stat_logs (optional, matches your existing patterns)
        try:
            self.sb.table("oc_stat_logs").insert({
                "oc_id": oc_id,
                "stat_key": stat_key,
                "delta": int(amount),
                "old_value": old_val,
                "new_value": new_val,
                "actor_discord_id": str(interaction.user.id),
                "reason": note or "player SP spend",
            }).execute()
        except Exception:
            pass

        # 4) Log SP spend
        self._log_sp(
            guild_id=str(interaction.guild_id or SKYFALL_GUILD_ID),
            oc_id=oc_id,
            oc_name=oc_name,
            owner_discord_id=owner_id,
            action="spend",
            amount=int(amount),
            created_by_discord_id=str(interaction.user.id),
            stat_key=stat_key,
            reason=note or "spend",
            meta={"old_sp": current_sp, "new_sp": new_sp, "old_stat": old_val, "new_stat": new_val},
        )

        await self._send_sp_log(
            interaction=interaction,
            title="🟦 SP Spent (Allocation)",
            oc_name=oc_name,
            oc_id=str(oc_id),
            owner_discord_id=owner_id or "unknown",
            fields=[
                ("Spent", f"`-{amount}`", True),
                ("Stat", f"`{stat_key}`", True),
                ("Stat Value", f"`{old_val} → {new_val}`", True),
                ("SP Balance", f"`{current_sp} → {new_sp}`", True),
                ("Note", note or "—", False),
            ],
            color=discord.Color.blue(),
        )

        await interaction.followup.send(
            f"✅ **{oc_name}** spent **{amount} SP** into **{stat_key}**: `{old_val} → {new_val}`.\n"
            f"🧠 Unallocated SP now: `{new_sp}`"
        )

    # -------------------------
    # /sp reset (player free reset)
    # Refunds current stats into SP pool and zeroes stats
    # -------------------------
    @sp.command(name="reset", description="Use your free stat reset: refunds stats into SP and clears stats to 0.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_reset(self, interaction: discord.Interaction, oc_name: str, confirm: str):
        if (confirm or "").strip().upper() != "RESET":
            return await interaction.response.send_message("Type `RESET` in confirm to proceed.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")

        if owner_id and owner_id != str(interaction.user.id):
            return await interaction.followup.send("❌ You can only reset your own OC.")

        total, used = self._ensure_reset_wallet(oc_id)
        if used >= total:
            return await interaction.followup.send("❌ No free resets remaining for this OC.")

        stats = self._get_stats_row(oc_id)
        refund = sum(int(stats.get(k, 0) or 0) for k in REFUND_STAT_KEYS)

        # Update reset wallet
        self.sb.table("oc_reset_wallets").upsert({
            "oc_id": oc_id,
            "free_resets_total": total,
            "free_resets_used": used + 1,
            "updated_at": "now()",
        }).execute()
        # update last_reset_at separately to avoid "now()" string issues on some clients
        try:
            self.sb.table("oc_reset_wallets").update({"last_reset_at": "now()"}).eq("oc_id", oc_id).execute()
        except Exception:
            pass

        current_sp = self._ensure_sp_wallet(oc_id)
        new_sp = current_sp + refund
        self._set_sp_wallet(oc_id, new_sp)

        # Zero stats
        updates = {"oc_id": oc_id}
        for k in REFUND_STAT_KEYS:
            updates[k] = 0
        self.sb.table("oc_stats").upsert(updates).execute()

        # Stat logs
        try:
            rows = []
            for k in REFUND_STAT_KEYS:
                old_val = int(stats.get(k, 0) or 0)
                if old_val == 0:
                    continue
                rows.append({
                    "oc_id": oc_id,
                    "stat_key": k,
                    "delta": -old_val,
                    "old_value": old_val,
                    "new_value": 0,
                    "actor_discord_id": str(interaction.user.id),
                    "reason": "free stat reset",
                })
            if rows:
                self.sb.table("oc_stat_logs").insert(rows).execute()
        except Exception:
            pass

        self._log_sp(
            guild_id=str(interaction.guild_id or SKYFALL_GUILD_ID),
            oc_id=oc_id,
            oc_name=oc_name,
            owner_discord_id=owner_id,
            action="reset_refund",
            amount=refund,
            created_by_discord_id=str(interaction.user.id),
            reason="free reset refund",
            meta={"old_sp": current_sp, "new_sp": new_sp, "free_total": total, "free_used": used + 1},
        )

        await self._send_sp_log(
            interaction=interaction,
            title="♻️ Free Stat Reset Used",
            oc_name=oc_name,
            oc_id=str(oc_id),
            owner_discord_id=owner_id or "unknown",
            fields=[
                ("Refunded SP", f"`+{refund}`", True),
                ("SP Balance", f"`{current_sp} → {new_sp}`", True),
                ("Free Resets", f"`{used + 1}/{total}`", True),
            ],
            color=discord.Color.gold(),
        )

        await interaction.followup.send(
            f"♻️ **{oc_name}** reset complete.\n"
            f"Refunded **{refund} SP** into unallocated pool.\n"
            f"🧠 Unallocated SP: `{new_sp}` • Free resets used: `{used + 1}/{total}`"
        )

    # -------------------------
    # /sp reset_staff (staff forced reset)
    # -------------------------
    @sp.command(name="reset_staff", description="(Stats Staff) Force a stat reset (refunds stats into SP and clears stats).")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_reset_staff(self, interaction: discord.Interaction, oc_name: str, confirm: str, reason: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("❌ You don’t have permission to do staff resets.", ephemeral=True)
        if (confirm or "").strip().upper() != "RESET":
            return await interaction.response.send_message("Type `RESET` in confirm to proceed.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")

        stats = self._get_stats_row(oc_id)
        refund = sum(int(stats.get(k, 0) or 0) for k in REFUND_STAT_KEYS)

        current_sp = self._ensure_sp_wallet(oc_id)
        new_sp = current_sp + refund
        self._set_sp_wallet(oc_id, new_sp)

        updates = {"oc_id": oc_id}
        for k in REFUND_STAT_KEYS:
            updates[k] = 0
        self.sb.table("oc_stats").upsert(updates).execute()

        # stat logs
        try:
            rows = []
            for k in REFUND_STAT_KEYS:
                old_val = int(stats.get(k, 0) or 0)
                if old_val == 0:
                    continue
                rows.append({
                    "oc_id": oc_id,
                    "stat_key": k,
                    "delta": -old_val,
                    "old_value": old_val,
                    "new_value": 0,
                    "actor_discord_id": str(interaction.user.id),
                    "reason": reason or "staff reset",
                })
            if rows:
                self.sb.table("oc_stat_logs").insert(rows).execute()
        except Exception:
            pass

        self._log_sp(
            guild_id=str(interaction.guild_id or SKYFALL_GUILD_ID),
            oc_id=oc_id,
            oc_name=oc_name,
            owner_discord_id=owner_id,
            action="reset_refund",
            amount=refund,
            created_by_discord_id=str(interaction.user.id),
            reason=reason or "staff reset refund",
            meta={"old_sp": current_sp, "new_sp": new_sp, "staff": True},
        )

        await self._send_sp_log(
            interaction=interaction,
            title="🟥 Staff Stat Reset",
            oc_name=oc_name,
            oc_id=str(oc_id),
            owner_discord_id=owner_id or "unknown",
            fields=[
                ("Refunded SP", f"`+{refund}`", True),
                ("SP Balance", f"`{current_sp} → {new_sp}`", True),
                ("Reason", reason or "staff reset", False),
            ],
            color=discord.Color.red(),
        )

        await interaction.followup.send(
            f"✅ Staff reset complete for **{oc_name}**.\nRefunded **{refund} SP**.\nUnallocated SP: `{new_sp}`"
        )

    # -------------------------
    # /sp reset_grant (staff adds extra free resets)
    # -------------------------
    @sp.command(name="reset_grant", description="(Stats Staff) Grant additional free stat resets to an OC.")
    @app_commands.autocomplete(oc_name=oc_name_autocomplete)
    async def sp_reset_grant(self, interaction: discord.Interaction, oc_name: str, amount: int, reason: Optional[str] = None):
        if not _is_stats_staff(interaction):
            return await interaction.response.send_message("❌ You don’t have permission to grant resets.", ephemeral=True)
        if amount <= 0:
            return await interaction.response.send_message("Amount must be > 0.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        oc = self._get_oc_by_name(oc_name)
        oc_id = oc["oc_id"]
        owner_id = str(oc.get("owner_discord_id") or "")

        total, used = self._ensure_reset_wallet(oc_id)
        new_total = total + int(amount)

        self.sb.table("oc_reset_wallets").upsert({
            "oc_id": oc_id,
            "free_resets_total": new_total,
            "free_resets_used": used,
        }).execute()

        self._log_sp(
            guild_id=str(interaction.guild_id or SKYFALL_GUILD_ID),
            oc_id=oc_id,
            oc_name=oc_name,
            owner_discord_id=owner_id,
            action="reset_grant",
            amount=int(amount),
            created_by_discord_id=str(interaction.user.id),
            reason=reason or "reset grant",
            meta={"old_total": total, "new_total": new_total, "used": used},
        )

        await self._send_sp_log(
            interaction=interaction,
            title="🟪 Free Reset Granted",
            oc_name=oc_name,
            oc_id=str(oc_id),
            owner_discord_id=owner_id or "unknown",
            fields=[
                ("Granted", f"`+{amount}`", True),
                ("Free Resets", f"`{used}/{new_total}` (remaining `{max(0, new_total - used)}`)", False),
                ("Reason", reason or "reset grant", False),
            ],
            color=discord.Color.purple(),
        )

        await interaction.followup.send(
            f"✅ Granted **{amount}** additional free reset(s) to **{oc_name}**.\n"
            f"Free resets: `{used}/{new_total}` (remaining `{max(0, new_total - used)}`)"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SPCog(bot))
