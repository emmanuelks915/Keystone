# cogs/item.py — Keystone Inventory Item Admin (Discord-facing)
# Public by default (non-ephemeral) for accountability.

import os
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands


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


class ItemCog(commands.GroupCog, group_name="item", group_description="Inventory item admin tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        super().__init__()

    # ── Supabase handle ────────────────────────────────────────────────────────
    def sb(self):
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            raise RuntimeError("Supabase is not configured on the bot.")
        return sb

    # ── Permission ─────────────────────────────────────────────────────────────
    def _staff_ok(self, interaction: discord.Interaction) -> bool:
        if _has_admin(interaction) or _is_dev(interaction.user.id):
            return True
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        staff_roles: set[int] = getattr(self.bot, "staff_role_ids", set())
        return any(r.id in staff_roles for r in interaction.user.roles)

    # ── Reply helpers ──────────────────────────────────────────────────────────
    async def _private(self, interaction: discord.Interaction, msg: str):
        if interaction.response.is_done():
            return await interaction.followup.send(msg, ephemeral=True)
        return await interaction.response.send_message(msg, ephemeral=True)

    async def _public(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ):
        kwargs = {"content": content, "embed": embed, "ephemeral": ephemeral}
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── DB helpers ─────────────────────────────────────────────────────────────
    def _get_item(self, sb, guild_id: int, item_id: str) -> Optional[dict]:
        res = (
            sb.table("items")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("item_id", str(item_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    def _list_items(self, sb, guild_id: int, *, active_only: bool = True, limit: int = 50) -> list[dict]:
        q = sb.table("items").select("*").eq("guild_id", int(guild_id))
        if active_only:
            q = q.eq("is_active", True)
        res = q.order("name", desc=False).limit(int(limit)).execute()
        return getattr(res, "data", None) or []

    async def _item_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        gid = int(interaction.guild.id)
        q = (current or "").strip().lower()

        items = self._list_items(sb, gid, active_only=False, limit=50)
        out: list[app_commands.Choice[str]] = []
        for it in items:
            name = str(it.get("name") or "")
            if q and q not in name.lower():
                continue
            tag = "✅" if it.get("is_active", True) else "🗑️"
            item_class = str(it.get("item_class") or "—")
            wu = it.get("wu")
            wu_txt = f"WU {int(wu)}" if wu is not None else "WU —"
            out.append(app_commands.Choice(name=f"{tag} {name[:60]} • {item_class[:20]} • {wu_txt}", value=str(it["item_id"])))
        return out[:25]

    # ───────────────────────────────────────────────────────────────────────────
    # COMMANDS
    # ───────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="create", description="Staff: Create a new inventory item definition")
    @app_commands.describe(
        name="Item name (unique per guild)",
        item_class="Class/category (e.g., Consumable, Weapon, Material)",
        wu="Weight Units (integer >= 0)",
        sheet_url="Link to the item sheet",
        notes="Optional notes for staff",
        is_active="Whether the item is active",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        item_class: str,
        wu: int,
        sheet_url: str,
        notes: str = "",
        is_active: bool = True,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        name = (name or "").strip()
        item_class = (item_class or "").strip()
        sheet_url = (sheet_url or "").strip()
        notes = (notes or "").strip()

        if not name:
            return await self._public(interaction, content="Name is required.", ephemeral=False)
        if len(name) > 120:
            return await self._public(interaction, content="Name too long (max 120).", ephemeral=False)
        if not item_class:
            return await self._public(interaction, content="item_class is required.", ephemeral=False)
        if wu < 0:
            return await self._public(interaction, content="wu must be >= 0.", ephemeral=False)
        if sheet_url and len(sheet_url) > 500:
            return await self._public(interaction, content="sheet_url too long (max 500).", ephemeral=False)

        sb = self.sb()
        gid = int(interaction.guild.id)

        row: dict[str, Any] = {
            "guild_id": gid,
            "name": name,
            "item_class": item_class[:64],
            "wu": int(wu),
            "sheet_url": sheet_url[:500] if sheet_url else None,
            "notes": notes[:1000] if notes else None,
            "is_active": bool(is_active),
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
        }

        try:
            res = sb.table("items").insert(row).execute()
            data = getattr(res, "data", None) or []
            item_id = str(data[0].get("item_id")) if data else "unknown"

            embed = discord.Embed(title="✅ Inventory Item Created", color=discord.Color.green())
            embed.add_field(name="Name", value=f"**{name}**", inline=False)
            embed.add_field(name="Class", value=f"`{item_class}`", inline=True)
            embed.add_field(name="WU", value=f"`{int(wu)}`", inline=True)
            if sheet_url:
                embed.add_field(name="Sheet", value=sheet_url, inline=False)
            if notes:
                embed.add_field(name="Notes", value=notes[:900], inline=False)
            embed.set_footer(text=f"Item ID: {item_id[:8]}")
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception as e:
            # If you added the unique index on (guild_id, lower(name)), this will catch duplicates.
            print(f"[item create] error: {e}")
            traceback.print_exc()
            return await self._public(
                interaction,
                content="❌ Could not create item. If the name already exists (even different casing), pick a different name.",
                ephemeral=False,
            )

    @app_commands.command(name="edit", description="Staff: Edit an inventory item definition")
    @app_commands.autocomplete(item=_item_autocomplete)
    @app_commands.describe(
        item="Which item",
        name="New name (optional)",
        item_class="New class (optional)",
        wu="New WU (optional)",
        sheet_url="New sheet url (optional)",
        notes="New notes (optional)",
        is_active="Set active/inactive (optional)",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        item: str,
        name: Optional[str] = None,
        item_class: Optional[str] = None,
        wu: Optional[int] = None,
        sheet_url: Optional[str] = None,
        notes: Optional[str] = None,
        is_active: Optional[bool] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        gid = int(interaction.guild.id)

        try:
            it = self._get_item(sb, gid, item)
            if not it:
                return await self._public(interaction, content="Item not found.", ephemeral=False)

            patch: dict[str, Any] = {"updated_at": self._now_iso()}

            if name is not None:
                name = name.strip()
                if not name:
                    return await self._public(interaction, content="Name can't be blank.", ephemeral=False)
                patch["name"] = name[:120]

            if item_class is not None:
                item_class = item_class.strip()
                if not item_class:
                    return await self._public(interaction, content="item_class can't be blank.", ephemeral=False)
                patch["item_class"] = item_class[:64]

            if wu is not None:
                if int(wu) < 0:
                    return await self._public(interaction, content="wu must be >= 0.", ephemeral=False)
                patch["wu"] = int(wu)

            if sheet_url is not None:
                sheet_url = sheet_url.strip()
                patch["sheet_url"] = sheet_url[:500] if sheet_url else None

            if notes is not None:
                notes = notes.strip()
                patch["notes"] = notes[:1000] if notes else None

            if is_active is not None:
                patch["is_active"] = bool(is_active)

            if len(patch.keys()) == 1:
                return await self._public(interaction, content="No changes provided.", ephemeral=False)

            sb.table("items").update(patch).eq("guild_id", gid).eq("item_id", str(it["item_id"])).execute()
            new_it = self._get_item(sb, gid, str(it["item_id"])) or it

            embed = discord.Embed(title="✅ Inventory Item Updated", color=discord.Color.blurple())
            embed.add_field(name="Name", value=f"**{new_it.get('name','(unknown)')}**", inline=False)
            embed.add_field(name="Class", value=f"`{new_it.get('item_class','—')}`", inline=True)
            embed.add_field(name="WU", value=f"`{int(new_it.get('wu') or 0)}`", inline=True)
            if new_it.get("sheet_url"):
                embed.add_field(name="Sheet", value=str(new_it["sheet_url"]), inline=False)
            if new_it.get("notes"):
                embed.add_field(name="Notes", value=str(new_it["notes"])[:900], inline=False)
            embed.add_field(name="Active", value="✅" if new_it.get("is_active", True) else "🗑️", inline=True)
            embed.set_footer(text=f"Item ID: {str(new_it['item_id'])[:8]}")
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception as e:
            print(f"[item edit] error: {e}")
            traceback.print_exc()
            return await self._public(interaction, content="Server error editing item.", ephemeral=False)

    @app_commands.command(name="info", description="Show quick info for an inventory item")
    @app_commands.autocomplete(item=_item_autocomplete)
    async def info(self, interaction: discord.Interaction, item: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        gid = int(interaction.guild.id)

        try:
            it = self._get_item(sb, gid, item)
            if not it:
                return await self._public(interaction, content="Item not found.", ephemeral=False)

            embed = discord.Embed(title="📦 Item Info", color=discord.Color.dark_teal())
            embed.add_field(name="Name", value=f"**{it.get('name','(unknown)')}**", inline=False)
            embed.add_field(name="Class", value=f"`{it.get('item_class','—')}`", inline=True)
            embed.add_field(name="WU", value=f"`{int(it.get('wu') or 0)}`", inline=True)
            if it.get("sheet_url"):
                embed.add_field(name="Sheet", value=str(it["sheet_url"]), inline=False)
            if it.get("notes"):
                embed.add_field(name="Notes", value=str(it["notes"])[:900], inline=False)
            embed.add_field(name="Active", value="✅" if it.get("is_active", True) else "🗑️", inline=True)
            embed.set_footer(text=f"Item ID: {str(it['item_id'])[:8]}")
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error fetching item.", ephemeral=False)

    @app_commands.command(name="list", description="List inventory items (active by default)")
    @app_commands.describe(show_inactive="Include inactive items")
    async def list(self, interaction: discord.Interaction, show_inactive: bool = False):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        gid = int(interaction.guild.id)

        try:
            items = self._list_items(sb, gid, active_only=not show_inactive, limit=25)
            if not items:
                return await self._public(interaction, content="No items found.", ephemeral=False)

            embed = discord.Embed(title="📦 Inventory Items", color=discord.Color.blurple())
            embed.description = "Use `/item info` for details."
            for it in items[:20]:
                tag = "✅" if it.get("is_active", True) else "🗑️"
                name = str(it.get("name") or "Item")
                ic = str(it.get("item_class") or "—")
                wu = it.get("wu")
                wu_txt = f"WU {int(wu)}" if wu is not None else "WU —"
                embed.add_field(
                    name=f"{tag} {name}",
                    value=f"`{ic}` • `{wu_txt}` • id `{str(it['item_id'])[:8]}`",
                    inline=False,
                )
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error listing items.", ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(ItemCog(bot))