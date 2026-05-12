import os
import io
import csv
import zipfile
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# Default to the channel you provided if env var isn't set
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "1473718234174718109"))

# If you want to override which tables get dumped without editing code:
# BACKUP_TABLES=characters,wallets,transactions,currencies,companies,...
DEFAULT_BACKUP_TABLES = [
    # Core economy
    "currencies",
    "wallets",
    "transactions",
    # OCs
    "characters",
    "active_characters",   # if you have it; harmless if missing
    # Companies/Banks + casino
    "companies",
    "company_members",
    "company_wallets",
    "company_transactions",
    "casino_settings",
    # Jobs/automation
    "jobs",
    "tax_config",
    "stipend_config",
]

# Discord file upload limits vary by server/boost; 25MB is a safe-ish default.
# If your server supports bigger uploads, bump this.
MAX_UPLOAD_BYTES = int(os.getenv("BACKUP_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

PAGE_SIZE = int(os.getenv("BACKUP_PAGE_SIZE", "1000"))


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


def _get_backup_tables() -> list[str]:
    raw = (os.getenv("BACKUP_TABLES") or "").strip()
    if not raw:
        return DEFAULT_BACKUP_TABLES
    tables = []
    for t in raw.split(","):
        t = t.strip()
        if t:
            tables.append(t)
    return tables or DEFAULT_BACKUP_TABLES


class BackupCog(commands.GroupCog, group_name="backup", group_description="Staff: CSV backup exporter"):
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

    async def _public_ok(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg)
        return await interaction.response.send_message(msg)

    async def _get_backup_channel(self, interaction: discord.Interaction) -> discord.abc.Messageable | None:
        """Resolve channel from guild cache or API. If not found, fall back to the current channel."""
        if interaction.guild:
            ch = interaction.guild.get_channel(BACKUP_CHANNEL_ID)
            if ch is None:
                try:
                    ch = await interaction.guild.fetch_channel(BACKUP_CHANNEL_ID)
                except Exception:
                    ch = None
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                return ch
        return interaction.channel

    def _fetch_table_all_rows(self, sb, table: str) -> list[dict]:
        """
        Fetch ALL rows from a table with paging. Safe-ish for modest sized tables.
        If the table doesn't exist / permission fails, it raises.
        """
        rows_all: list[dict] = []
        offset = 0

        while True:
            # PostgREST pagination with range
            res = (
                sb.table(table)
                .select("*")
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
            )
            chunk = getattr(res, "data", None) or []
            if not chunk:
                break
            rows_all.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        return rows_all

    def _rows_to_csv_bytes(self, rows: list[dict]) -> bytes:
        """
        Convert list[dict] to CSV bytes, unioning all keys for header.
        """
        buf = io.StringIO()

        # build stable header set
        fieldnames: list[str] = []
        seen = set()
        for r in rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)

        # if no rows, still produce an empty csv with no header
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        if fieldnames:
            writer.writeheader()
        for r in rows:
            # normalize nested dict/list into JSON-ish strings
            clean = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    clean[k] = str(v)
                else:
                    clean[k] = v
            writer.writerow(clean)

        return buf.getvalue().encode("utf-8")

    def _build_zip(self, tables_data: dict[str, list[dict]]) -> bytes:
        """
        Create a zip of csv files in memory and return bytes.
        """
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for table, rows in tables_data.items():
                csv_bytes = self._rows_to_csv_bytes(rows)
                z.writestr(f"{table}.csv", csv_bytes)
        out.seek(0)
        return out.read()

    # ─────────────────────────────────────────────────────────────
    # Commands
    # ─────────────────────────────────────────────────────────────

    @app_commands.command(name="run", description="Staff: Export CSV backup ZIP to the backup channel")
    async def run(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not self._staff_ok(interaction):
            return await self._private_err(interaction, "❌ Staff only.")
        sb = self.sb()

        tables = _get_backup_tables()
        started = datetime.now(timezone.utc)

        try:
            # Fetch data
            tables_data: dict[str, list[dict]] = {}
            ok_tables: list[str] = []
            skipped: list[str] = []

            for t in tables:
                try:
                    rows = self._fetch_table_all_rows(sb, t)
                    tables_data[t] = rows
                    ok_tables.append(f"{t}({len(rows)})")
                except Exception:
                    # Missing table or permission issue, don't kill the whole run
                    skipped.append(t)

            # Build ZIP
            zip_bytes = self._build_zip(tables_data)

            if len(zip_bytes) > MAX_UPLOAD_BYTES:
                return await self._private_err(
                    interaction,
                    f"❌ Backup ZIP is too large to upload ({len(zip_bytes)/1024/1024:.2f} MB). "
                    f"Reduce BACKUP_TABLES or increase BACKUP_MAX_UPLOAD_BYTES / server upload limit."
                )

            # Post it
            channel = await self._get_backup_channel(interaction)
            if channel is None:
                return await self._private_err(interaction, "❌ Could not resolve a channel to post the backup.")

            stamp = started.strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"keystone_backup_{stamp}.zip"

            file = discord.File(fp=io.BytesIO(zip_bytes), filename=filename)

            msg_lines = [
                "📦 **Keystone CSV Backup**",
                f"🕒 `{started.isoformat()}`",
                f"✅ Tables: {', '.join(ok_tables) if ok_tables else '(none)'}",
            ]
            if skipped:
                msg_lines.append(f"⚠️ Skipped: {', '.join(skipped)}")

            await channel.send("\n".join(msg_lines), file=file)

            dur = datetime.now(timezone.utc) - started
            return await self._private_err(
                interaction,
                f"✅ Backup uploaded to <#{BACKUP_CHANNEL_ID}> as `{filename}` "
                f"({dur.total_seconds():.1f}s)."
            )

        except Exception as e:
            print(f"[backup run] error: {e}")
            traceback.print_exc()
            return await self._private_err(interaction, "Server error generating backup.")

    @app_commands.command(name="where", description="Show where backups are uploaded")
    async def where(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        return await interaction.followup.send(
            f"📦 Backups upload to channel ID `{BACKUP_CHANNEL_ID}` (set `BACKUP_CHANNEL_ID` to change).",
            ephemeral=True,
        )

    @app_commands.command(name="tables", description="Show which tables the backup exports")
    async def tables(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        tables = _get_backup_tables()
        txt = "\n".join([f"- `{t}`" for t in tables])
        if len(txt) > 1800:
            txt = txt[:1800] + "\n…"
        return await interaction.followup.send(
            f"📦 Backup tables:\n{txt}\n\n(Override with `BACKUP_TABLES=table1,table2,...`)",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BackupCog(bot))