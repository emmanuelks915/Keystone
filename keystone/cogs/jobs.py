import os
import traceback
import discord
from discord import app_commands
from discord.ext import commands

from services.jobs_tax import run_tax_job

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


class JobsCog(commands.GroupCog, group_name="jobs", group_description="Scheduled jobs (staff)"):
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
    # Autocomplete: job picker (returns UUID values)
    # ─────────────────────────────────────────────────────────────

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
            job_id = str(r.get("job_id") or "")
            jt = str(r.get("job_type") or "JOB").upper()
            enabled = "✅" if r.get("enabled") else "🛑"
            next_run = str(r.get("next_run_at") or "—")

            # Try to show something human from config (like a reason)
            label_hint = ""
            cfg = r.get("config")
            if isinstance(cfg, dict):
                reason = cfg.get("reason")
                if isinstance(reason, str) and reason.strip():
                    label_hint = reason.strip()

            # Display label (<= 100 chars)
            label = f"{enabled} {jt}"
            if label_hint:
                label += f" • {label_hint}"
            label += f" • next {next_run}"
            label = label[:100]

            # Filter by user's typing (match type, reason, or id prefix)
            hay = f"{jt} {label_hint} {job_id}".lower()
            if q and q not in hay:
                continue

            out.append(app_commands.Choice(name=label, value=job_id))

        return out[:25]

    # ─────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="list", description="Staff: List scheduled jobs in this server")
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            res = (
                sb.table("scheduled_jobs")
                .select("job_id,job_type,enabled,interval_seconds,next_run_at,last_status,last_error,config")
                .eq("guild_id", guild_id)
                .order("created_at", desc=False)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public(interaction, content="No scheduled jobs yet. Use `/jobs create_tax_weekly`.")

            embed = discord.Embed(title="Scheduled Jobs", color=discord.Color.blurple())
            for r in rows[:25]:
                full_id = str(r["job_id"])
                short = full_id[:8]
                enabled = "✅" if r.get("enabled") else "🛑"
                jt = str(r.get("job_type") or "—").upper()
                interval = int(r.get("interval_seconds") or 0)
                next_run = str(r.get("next_run_at") or "—")
                last = str(r.get("last_status") or "—")
                err = str(r.get("last_error") or "")

                hint = ""
                cfg = r.get("config")
                if isinstance(cfg, dict):
                    reason = cfg.get("reason")
                    if isinstance(reason, str) and reason.strip():
                        hint = reason.strip()

                value = (
                    f"{enabled} **{jt}**\n"
                    f"Hint: `{hint or '—'}`\n"
                    f"Interval: `{interval}s`\n"
                    f"Next: `{next_run}`\n"
                    f"Last: `{last}`\n"
                    f"Pick via autocomplete (no UUIDs needed)."
                )
                if err:
                    value += f"\nErr: `{err[:180]}`"

                embed.add_field(name=f"{short}", value=value, inline=False)

            return await self._public(interaction, embed=embed)

        except Exception as e:
            print(f"[jobs list] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error listing jobs.")

    @app_commands.command(name="create_tax_weekly", description="Staff: Create a weekly TAX_RUN job")
    @app_commands.describe(reason="Optional hint shown in job picker")
    async def create_tax_weekly(self, interaction: discord.Interaction, reason: str | None = None):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            interval = 7 * 24 * 60 * 60  # weekly
            payload = {
                "guild_id": guild_id,
                "job_type": "TAX_RUN",
                "enabled": True,
                "interval_seconds": interval,
                "config": {"reason": reason or "weekly tax"},
            }
            ins = sb.table("scheduled_jobs").insert(payload).execute()
            row = (getattr(ins, "data", None) or [None])[0]
            if not row:
                return await self._private(interaction, "Failed to create job.")

            embed = discord.Embed(
                title="Ledger • Job Created",
                description="🕒 **TAX_RUN** (weekly)",
                color=discord.Color.green(),
            )
            embed.add_field(name="By", value=f"`{interaction.user}`", inline=False)
            embed.add_field(name="Hint", value=f"`{payload['config']['reason']}`", inline=False)
            embed.timestamp = discord.utils.utcnow()
            await self._post_ledger(interaction, embed)

            return await self._public(interaction, content="✅ Created weekly TAX_RUN job.")

        except Exception as e:
            print(f"[jobs create_tax_weekly] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error creating job.")

    @app_commands.command(name="enable", description="Staff: Enable a job")
    @app_commands.describe(job="Pick a job")
    @app_commands.autocomplete(job=_job_autocomplete)
    async def enable(self, interaction: discord.Interaction, job: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("scheduled_jobs").update({"enabled": True}).eq("guild_id", guild_id).eq("job_id", job).execute()
            return await self._public(interaction, content="✅ Job enabled.")
        except Exception as e:
            print(f"[jobs enable] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error enabling job.")

    @app_commands.command(name="disable", description="Staff: Disable a job")
    @app_commands.describe(job="Pick a job")
    @app_commands.autocomplete(job=_job_autocomplete)
    async def disable(self, interaction: discord.Interaction, job: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            sb.table("scheduled_jobs").update({"enabled": False}).eq("guild_id", guild_id).eq("job_id", job).execute()
            return await self._public(interaction, content="🛑 Job disabled.")
        except Exception as e:
            print(f"[jobs disable] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error disabling job.")

    @app_commands.command(name="run_now", description="Staff: Run a job immediately (manual trigger)")
    @app_commands.describe(job="Pick a job")
    @app_commands.autocomplete(job=_job_autocomplete)
    async def run_now(self, interaction: discord.Interaction, job: str):
        await interaction.response.defer()

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        actor_id = int(interaction.user.id)

        try:
            res = (
                sb.table("scheduled_jobs")
                .select("*")
                .eq("guild_id", guild_id)
                .eq("job_id", job)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._private(interaction, "Job not found in this server.")

            job_row = rows[0]
            jt = str(job_row.get("job_type") or "").upper().strip()

            if jt != "TAX_RUN":
                return await self._private(interaction, f"run_now not implemented for `{jt}` yet.")

            config = job_row.get("config") or {}
            reason = None
            if isinstance(config, dict):
                reason = config.get("reason")

            summary = run_tax_job(
                sb,
                guild_id=guild_id,
                actor_discord_id=actor_id,
                reason=reason or "manual run",
            )

            return await self._public(
                interaction,
                content=f"✅ Ran TAX_RUN now. Collected `{summary['total']}` from `{summary['charged']}` OCs (skipped `{summary['skipped']}`).",
            )

        except Exception as e:
            print(f"[jobs run_now] error: {e}")
            traceback.print_exc()
            return await self._private(interaction, "Server error running job.")


async def setup(bot: commands.Bot):
    await bot.add_cog(JobsCog(bot))