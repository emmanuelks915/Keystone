# bot.py — Keystone
import os
import asyncio
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Optional Supabase (safe if not installed / not configured)
try:
    from supabase import create_client  # type: ignore
except Exception:
    create_client = None  # type: ignore

# Scheduled jobs
from services.job_runner import JobRunner
from services.jobs_tax import run_tax_job
from services.jobs_stipend import run_stipend_job


# ── Env ────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Rail-Bound (home guild). Default to old Skyfall ID if env not set (update this!)
GUILD_ID = int(os.getenv("GUILD_ID", "1374730886234374235") or "0")

# Supabase env (optional)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# IMPORTANT:
# Do NOT sync on every boot by default. Discord has daily command-create limits,
# and clearing/recreating commands can burn through that limit very fast.
#
# Normal development:
#   SYNC_ON_BOOT=false
#
# When you intentionally changed slash command names/options:
#   1) Set SYNC_ON_BOOT=true
#   2) Start the bot once and confirm sync succeeds
#   3) Set SYNC_ON_BOOT=false again
#
# You can also use the prefix command:
#   !sync_commands
SYNC_ON_BOOT = os.getenv("SYNC_ON_BOOT", "false").strip().lower() in ("1", "true", "yes", "y")
KEYSTONE_MODE = os.getenv("KEYSTONE_MODE", "true").strip().lower() in ("1", "true", "yes", "y")

DEFAULT_STAFF_ROLE_IDS = {1462497965775257827, 1462498058242625749}


def _parse_staff_role_ids() -> set[int]:
    raw = (os.getenv("STAFF_ROLE_IDS") or "").strip()
    if not raw:
        return set(DEFAULT_STAFF_ROLE_IDS)

    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))

    return out or set(DEFAULT_STAFF_ROLE_IDS)


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


def _is_daily_command_create_limit(error: Exception) -> bool:
    """
    Discord error 30034:
    Max number of daily application command creates has been reached.
    """
    return isinstance(error, discord.HTTPException) and getattr(error, "code", None) == 30034


KEYSTONE_EXTENSIONS = [
    "cogs.admin",
    "cogs.ping",
    "cogs.oc",
    "cogs.ledger",
    "cogs.wallet",
    "cogs.wallet_admin",
    "cogs.currency_admin",
    "cogs.tax",
    "cogs.jobs",
    "cogs.stipend",
    "cogs.leaderboard",
    "cogs.casino",
    "cogs.backup",
    "cogs.inventory",
    "cogs.items",
    "cogs.stats",
    "cogs.traits",
    "cogs.checks",
    "cogs.xp",
    "cogs.dice",
    "cogs.travel",
    "cogs.rp_tracker",
    "cogs.commerce",
    "cogs.postwindow",
    "cogs.giveaway",
    "cogs.help",
    "cogs.signal_bell",
    "cogs.journal_tracker",


]


class KeystoneBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.start_time = datetime.now(timezone.utc)
        self.guild_id: int | None = GUILD_ID if GUILD_ID else None
        self.staff_role_ids: set[int] = _parse_staff_role_ids()
        self.dev_user_ids: set[int] = _parse_dev_ids()
        self.job_runner: JobRunner | None = None

    async def setup_hook(self):
        print("\n🧱 Initializing Keystone...")
        print(f"👷 Staff role IDs: {sorted(self.staff_role_ids)}")
        print(f"🛠️ Dev user IDs: {sorted(self.dev_user_ids)}")

        await self.load_extensions()

        if SYNC_ON_BOOT:
            await self.safe_sync_application_commands(source="bootstrap")
        else:
            print("⏭️ [bootstrap] Slash command sync skipped.")
            print("   Set SYNC_ON_BOOT=true once, or run !sync_commands, only when command names/options changed.")

        await self.start_job_runner()

        print("✅ Keystone initialization complete")

    async def safe_sync_application_commands(self, *, source: str = "manual") -> list[app_commands.AppCommand]:
        """
        Safe guild command sync.

        This intentionally does NOT clear guild commands first.

        Old behavior:
            clear guild commands -> sync empty -> copy global commands -> sync full set

        That burns Discord's daily command-create budget because every boot can recreate
        the full command set.

        New behavior:
            copy current in-memory commands to the home guild -> sync once

        This updates existing commands and creates only genuinely new ones.
        """
        if not self.guild_id:
            print(f"⚠️ [{source}] Cannot sync: GUILD_ID is not configured.")
            return []

        guild_obj = discord.Object(id=self.guild_id)

        print(f"📌 [{source}] Copying current app commands to guild {self.guild_id}...")
        self.tree.copy_global_to(guild=guild_obj)

        print(f"🔁 [{source}] Syncing guild commands safely for guild {self.guild_id}...")
        try:
            synced = await self.tree.sync(guild=guild_obj)
            print(f"✅ [{source}] Synced {len(synced)} guild commands.")
            print(f"📋 [{source}] Guild commands: {[c.name for c in synced]}")
            return synced

        except Exception as e:
            if _is_daily_command_create_limit(e):
                print("🚫 Discord command-create limit reached for today.")
                print("   Your bot is online, but slash command changes cannot sync until Discord's daily limit resets.")
                print("   Keep SYNC_ON_BOOT=false so the bot does not keep retrying every restart.")
                print(f"   Raw error: {e!r}")
                return []

            print(f"❌ [{source}] Guild sync failed: {e!r}")
            traceback.print_exc()
            return []

    def can_run_dev_command(self, member: discord.abc.User | discord.Member) -> bool:
        uid = int(member.id)

        if uid in self.dev_user_ids:
            return True

        if isinstance(member, discord.Member):
            if member.guild_permissions.administrator:
                return True
            return any(role.id in self.staff_role_ids for role in member.roles)

        return False

    async def start_job_runner(self):
        try:
            if getattr(self, "supabase", None) is None:
                print("⚠️ JobRunner not started (Supabase not configured).")
                return

            self.job_runner = JobRunner(bot=self, poll_seconds=30)

            async def _handle_tax_run(job_row: dict) -> str:
                sb = self.supabase
                guild_id = int(job_row["guild_id"])
                config = job_row.get("config") or {}
                reason = config.get("reason") if isinstance(config, dict) else None
                actor_id = int(self.user.id) if self.user else 0

                def _do():
                    return run_tax_job(
                        sb,
                        guild_id=guild_id,
                        actor_discord_id=actor_id,
                        reason=reason or "scheduled tax",
                    )

                summary = await asyncio.to_thread(_do)
                return (
                    f"Collected {summary['total']} from {summary['charged']} OCs "
                    f"(skipped {summary['skipped']})."
                )

            async def _handle_stipend_run(job_row: dict) -> str:
                sb = self.supabase
                guild_id = int(job_row["guild_id"])
                config = job_row.get("config") or {}
                reason = config.get("reason") if isinstance(config, dict) else None
                actor_id = int(self.user.id) if self.user else 0

                summary = await run_stipend_job(
                    bot=self,
                    sb=sb,
                    guild_id=guild_id,
                    actor_discord_id=actor_id,
                    reason=reason or "scheduled stipend",
                )

                return (
                    f"Paid {summary['paid_total']} to {summary['recipients']} OCs "
                    f"(skipped {summary['skipped']})."
                )

            self.job_runner.register("TAX_RUN", _handle_tax_run)
            self.job_runner.register("STIPEND_RUN", _handle_stipend_run)
            self.job_runner.start()

            print("🕒 JobRunner started (poll=30s) with TAX_RUN + STIPEND_RUN handlers registered")

        except Exception as e:
            print(f"❌ Failed to start JobRunner: {e}")
            traceback.print_exc()

    async def load_extensions(self):
        loaded, failed = [], []

        if KEYSTONE_MODE:
            extensions = KEYSTONE_EXTENSIONS
            print("🧱 KEYSTONE_MODE=true → Loading curated cogs only")
        else:
            extensions = [
                f"cogs.{fn[:-3]}"
                for fn in os.listdir("./cogs")
                if fn.endswith(".py") and not fn.startswith("_")
            ]
            print("🍊 KEYSTONE_MODE=false → Loading ALL cogs in ./cogs")

        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"INFO: Loaded extension: {ext}")
                loaded.append(ext)
            except Exception as e:
                print(f"❌ Failed to load {ext}: {e}")
                traceback.print_exc()
                failed.append(ext)

        print(f"📦 Extensions: {len(loaded)} loaded, {len(failed)} failed")
        if loaded:
            print("✅ Loaded: " + ", ".join(loaded))
        if failed:
            print("❌ Failed: " + ", ".join(failed))

    async def on_ready(self):
        print("\n🧱 Keystone Online!")

        if self.user:
            print(f"🔹 User: {self.user} (ID: {self.user.id})")

        print(f"🔹 Guilds: {len(self.guilds)}")
        print(f"🔹 Uptime: {datetime.now(timezone.utc) - self.start_time}")

        if self.guild_id:
            guild = self.get_guild(self.guild_id)
            if guild:
                print(f"🏠 Home Server: {guild.name} (ID: {guild.id})")
            else:
                print(f"⚠️ Configured guild {self.guild_id} not found!")

    async def close(self):
        print("\n🔌 Shutting down gracefully...")

        try:
            if self.job_runner is not None:
                await self.job_runner.stop()
                print("🕒 JobRunner stopped")
        except Exception:
            traceback.print_exc()

        await super().close()


bot = KeystoneBot()


@bot.command(name="sync_commands")
async def sync_commands(ctx: commands.Context):
    """
    Manual safe slash-command sync.

    Usage:
        !sync_commands

    This does NOT clear commands first.
    It is safe to use after changing slash command options, but do not spam it.
    """
    if not bot.can_run_dev_command(ctx.author):
        return await ctx.reply("❌ Staff/dev only.", mention_author=False)

    await ctx.reply("🔁 Safely syncing slash commands for the home guild...", mention_author=False)
    synced = await bot.safe_sync_application_commands(source=f"manual:{ctx.author.id}")

    if synced:
        await ctx.reply(f"✅ Synced `{len(synced)}` slash command groups/commands.", mention_author=False)
    else:
        await ctx.reply(
            "⚠️ Sync did not complete or nothing was returned. Check the console logs. "
            "If you hit Discord's daily create limit, leave `SYNC_ON_BOOT=false` and try again after it resets.",
            mention_author=False,
        )


@bot.command(name="sync_status")
async def sync_status(ctx: commands.Context):
    """
    Quick local status check for command syncing config.
    """
    if not bot.can_run_dev_command(ctx.author):
        return await ctx.reply("❌ Staff/dev only.", mention_author=False)

    await ctx.reply(
        "\n".join(
            [
                "🧱 **Keystone Sync Status**",
                f"`GUILD_ID`: `{bot.guild_id}`",
                f"`SYNC_ON_BOOT`: `{SYNC_ON_BOOT}`",
                "`Clear-before-sync`: `disabled permanently in this patch`",
                "Manual safe sync: `!sync_commands`",
            ]
        ),
        mention_author=False,
    )


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.application_command:
        try:
            qn = interaction.command.qualified_name if interaction.command else "unknown"
            print(f"➡️ Interaction received: {qn} by {interaction.user} ({interaction.user.id})")
        except Exception:
            print("➡️ Interaction received (application_command), could not read command name")

    await bot.process_application_commands(interaction)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    print("❌ App command error:", repr(error))
    traceback.print_exc()

    try:
        msg = f"Error: {type(error).__name__}: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    bot.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase client attached to Keystone")
else:
    print(
        "⚠️ Supabase not configured "
        "(SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing). "
        "Anything that depends on Supabase will be disabled for now."
    )


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Add it to your .env file.")

    async with bot:
        try:
            await bot.start(TOKEN)
        except KeyboardInterrupt:
            print("\n🛑 Received keyboard interrupt")
        except Exception as e:
            print(f"💥 Fatal error: {e}")
            traceback.print_exc()
        finally:
            if not bot.is_closed():
                await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
