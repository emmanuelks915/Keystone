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


# ── Env ────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Rail-Bound (home guild). Default to old Skyfall ID if env not set (update this!)
GUILD_ID = int(os.getenv("GUILD_ID", "1374730886234374235"))

# Supabase env (optional for now)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# Bootstrap sync on boot (guild-only). Default ON.
SYNC_ON_BOOT = (os.getenv("SYNC_ON_BOOT", "true").strip().lower() in ("1", "true", "yes", "y"))

# Keystone mode: load only curated cogs (default ON)
KEYSTONE_MODE = (os.getenv("KEYSTONE_MODE", "true").strip().lower() in ("1", "true", "yes", "y"))

# Keystone v1 cog allowlist
KEYSTONE_EXTENSIONS = [
    "cogs.admin",   # /sync /reload dev tools
    "cogs.ping",    # /ping sanity check
    "cogs.oc",      # ✅ new OC group: /oc create|list|select
    "cogs.items",   # item definitions / basic inventory foundation (swap if needed)
    "cogs.ledger",  # logging hooks (swap if needed)
]


# ── Bot class ──────────────────────────────────────────────────────────────────
class KeystoneBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # keep for now (can turn off later)
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.start_time = datetime.now(timezone.utc)
        self.guild_id: int | None = GUILD_ID if GUILD_ID else None

    async def setup_hook(self):
        """
        Initialize Keystone: load cogs, then (optionally) do a guild-only bootstrap sync.

        Notes:
        - In KEYSTONE_MODE, we only load a curated set of cogs so we don't carry Tangerine scope.
        - I can flip KEYSTONE_MODE=false in .env if I ever need to load everything.
        """
        print("\n🧱 Initializing Keystone...")
        await self.load_extensions()

        # BOOTSTRAP GUILD SYNC (safe — not global)
        if SYNC_ON_BOOT and self.guild_id:
            try:
                guild_obj = discord.Object(id=self.guild_id)

                print(f"📌 [bootstrap] Copying global commands to guild {self.guild_id}...")
                self.tree.copy_global_to(guild=guild_obj)

                print(f"🔁 [bootstrap] Starting guild sync for guild {self.guild_id}...")
                synced = await self.tree.sync(guild=guild_obj)

                print(f"✅ [bootstrap] Finished guild sync for guild {self.guild_id}: {len(synced)} commands")
                print(f"📋 [bootstrap] Guild commands: {[c.name for c in synced]}")
            except Exception as e:
                print(f"❌ [bootstrap] Guild sync failed: {e!r}")
                traceback.print_exc()

        print("✅ Keystone initialization complete")

    async def load_extensions(self):
        """Load cogs. In Keystone mode, load only the allowlist."""
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
        print(f"\n🧱 Keystone Online!")
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
        await super().close()


# ── Boot ───────────────────────────────────────────────────────────────────────
bot = KeystoneBot()


# Log every slash-command interaction (confirms bot receives interactions)
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.application_command:
        try:
            qn = interaction.command.qualified_name if interaction.command else "unknown"
            print(f"➡️ Interaction received: {qn} by {interaction.user} ({interaction.user.id})")
        except Exception:
            print("➡️ Interaction received (application_command), could not read command name")

    # Let discord.py route the interaction to app commands
    await bot.process_application_commands(interaction)


# Global error handler for slash commands
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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


# Supabase client (optional)
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    bot.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase client attached to Keystone")
else:
    print(
        "⚠️ Supabase not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing). "
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