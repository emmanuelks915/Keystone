import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.jobs_stipend import run_stipend_job

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


class StipendCog(commands.GroupCog, group_name="stipend", group_description="Stipends / paychecks (staff)"):
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

    async def _job_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").lower().strip()

        try:
            res = (
                sb.table("scheduled_jobs")
                .select("job_id,job_type,enabled,next_run_at,config")
                .eq("guild_id", guild_id)
                .order("created_at", desc=False)
                .limit(50)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception:
            return []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            jt = str(r.get("job_type") or "").upper()
            if jt != "STIPEND_RUN":
                continue

            job_id = str(r.get("job_id") or "")
            enabled = "✅" if r.get("enabled") else "🛑"
            next_run = str(r.get("next_run_at") or "—")
            hint = ""
            cfg = r.get("config")
            if isinstance(cfg, dict):
                hint = str(cfg.get("reason") or "").strip()

            label = f"{enabled} STIPEND_RUN"
            if hint:
                label += f" • {hint}"
            label += f" • next {next_run}"
            label = label[:100]

            hay = f"{label} {job_id}".lower()
            if q and q not in hay:
                continue

            out.append(app_commands.Choice(name=label, value=job_id))
        return out[:25]

    # ─────────────────────────────────────────────────────────────
    # Settings commands
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="view", description="Staff: View stipend settings")
    async def view(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            s = sb.table("stipend_settings").select("*").eq("guild_id", guild_id).limit(1).execute()
            rows = getattr(s, "data", None) or []
            settings = rows[0] if rows else {"enabled": False, "base_amount": 0, "max_recipients": 500}

            b = sb.table("stipend_role_bonuses").select("role_id,bonus_amount").eq("guild_id", guild_id).execute()
            bonus_rows = getattr(b, "data", None) or []

            embed = discord.Embed(title="Stipend Settings", color=discord.Color.blurple())
            embed.add_field(name="Enabled", value="✅ Yes" if settings.get("enabled") else "🛑 No", inline=True)
            embed.add_field(name="Base Amount", value=f"`{int(settings.get('base_amount') or 0)}`", inline=True)
            embed.add_field(name="Max Recipients", value=f"`{int(settings.get('max_recipients') or 500)}`", inline=True)

            if bonus_rows:
                lines = []
                for r in bonus_rows[:20]:
                    rid = int(r["role_id"])
                    amt = int(r.get("bonus_amount") or 0)
                    role = interaction.guild.get_role(rid)
                    rname = role.name if role else str(rid)
                    lines.append(f"- **{rname}**: `+{amt}`")
                embed.add_field(name="Role Bonuses", value="\n".join(lines), inline=False)
            else:
                embed.add_field(name="Role Bonuses", value="(none)", inline=False)

            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[stipend view] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error viewing stipend settings.")

    @app_commands.command(name="enable", description="Staff: Enable stipends")
    async def enable(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("stipend_settings").upsert(
                {"guild_id": guild_id, "enabled": True},
                on_conflict="guild_id",
            ).execute()
            return await self._public(interaction, content="✅ Stipends enabled.")
        except Exception as e:
            print(f"[stipend enable] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error enabling stipends.")

    @app_commands.command(name="disable", description="Staff: Disable stipends")
    async def disable(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("stipend_settings").upsert(
                {"guild_id": guild_id, "enabled": False},
                on_conflict="guild_id",
            ).execute()
            return await self._public(interaction, content="🛑 Stipends disabled.")
        except Exception as e:
            print(f"[stipend disable] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error disabling stipends.")

    @app_commands.command(name="set_base", description="Staff: Set base stipend amount")
    @app_commands.describe(amount="Base amount paid to each OC")
    async def set_base(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if amount < 0:
            return await self._private(interaction, "Amount cannot be negative.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("stipend_settings").upsert(
                {"guild_id": guild_id, "base_amount": int(amount)},
                on_conflict="guild_id",
            ).execute()
            return await self._public(interaction, content=f"✅ Base stipend set to `{amount}`.")
        except Exception as e:
            print(f"[stipend set_base] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error setting base stipend.")

    @app_commands.command(name="add_bonus", description="Staff: Add/replace a role bonus for stipends")
    @app_commands.describe(role="Discord role", amount="Bonus amount added if member has this role")
    async def add_bonus(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("stipend_role_bonuses").upsert(
                {"guild_id": guild_id, "role_id": int(role.id), "bonus_amount": int(amount)},
                on_conflict="guild_id,role_id",
            ).execute()
            return await self._public(interaction, content=f"✅ Bonus for **{role.name}** set to `+{amount}`.")
        except Exception as e:
            print(f"[stipend add_bonus] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error adding role bonus.")

    @app_commands.command(name="remove_bonus", description="Staff: Remove a role bonus")
    @app_commands.describe(role="Discord role")
    async def remove_bonus(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("stipend_role_bonuses").delete().eq("guild_id", guild_id).eq("role_id", int(role.id)).execute()
            return await self._public(interaction, content=f"🗑️ Removed bonus for **{role.name}**.")
        except Exception as e:
            print(f"[stipend remove_bonus] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error removing role bonus.")

    # ─────────────────────────────────────────────────────────────
    # Scheduling + run_now
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="schedule", description="Staff: Schedule stipend payouts (daily/weekly)")
    @app_commands.describe(frequency="daily or weekly", reason="Optional hint shown in job picker")
    async def schedule(self, interaction: discord.Interaction, frequency: str, reason: str | None = None):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        freq = (frequency or "").lower().strip()
        if freq not in ("daily", "weekly"):
            return await self._private(interaction, "Frequency must be `daily` or `weekly`.")

        interval = 24 * 60 * 60 if freq == "daily" else 7 * 24 * 60 * 60

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            # Find existing STIPEND_RUN job, update it; otherwise insert.
            existing = (
                sb.table("scheduled_jobs")
                .select("job_id")
                .eq("guild_id", guild_id)
                .eq("job_type", "STIPEND_RUN")
                .limit(1)
                .execute()
            )
            rows = getattr(existing, "data", None) or []

            payload = {
                "guild_id": guild_id,
                "job_type": "STIPEND_RUN",
                "enabled": True,
                "interval_seconds": int(interval),
                "config": {"reason": reason or f"{freq} stipend"},
            }

            if rows:
                sb.table("scheduled_jobs").update(payload).eq("job_id", rows[0]["job_id"]).execute()
                return await self._public(interaction, content=f"✅ Updated stipend schedule to **{freq}**.")
            else:
                sb.table("scheduled_jobs").insert(payload).execute()
                return await self._public(interaction, content=f"✅ Scheduled stipends **{freq}**.")

        except Exception as e:
            print(f"[stipend schedule] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error scheduling stipend.")

    @app_commands.command(name="run_now", description="Staff: Run stipends right now (manual test)")
    @app_commands.describe(reason="Optional reason text")
    async def run_now(self, interaction: discord.Interaction, reason: str | None = None):
        await interaction.response.defer()
        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            summary = await run_stipend_job(
                bot=self.bot,
                sb=sb,
                guild_id=guild_id,
                actor_discord_id=int(interaction.user.id),
                reason=reason or "manual stipend run",
            )

            embed = discord.Embed(
                title="Ledger • Stipend Run",
                description="💵 Stipends paid",
                color=discord.Color.green(),
            )
            embed.add_field(name="Paid Total", value=f"`{summary['paid_total']}`", inline=True)
            embed.add_field(name="Recipients", value=f"`{summary['recipients']}`", inline=True)
            embed.add_field(name="Skipped", value=f"`{summary['skipped']}`", inline=True)
            embed.add_field(name="Base Amount", value=f"`{summary['base_amount']}`", inline=True)
            embed.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            embed.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed)

            return await self._public(
                interaction,
                content=f"✅ Paid stipends to `{summary['recipients']}` OCs. Total paid: `{summary['paid_total']}`.",
            )

        except Exception as e:
            print(f"[stipend run_now] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, f"Error running stipends: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(StipendCog(bot))