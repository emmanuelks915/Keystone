# cogs/inventory.py — Keystone Inventory (items + character inventory + loadouts)
# PUBLIC BY DEFAULT (no ephemeral unless needed).
#
# Features:
# - Staff: define items (/inv define_item)
# - Staff: grant/take inventory (/inv grant, /inv take)
# - Player: view inventory (/inv view)
# - Player: loadouts: save/print/list/delete/add/remove/equip/active
#
# Requires DB objects:
# - public.items
# - public.inventory_entries
# - public.inventory_logs
# - public.inventory_loadouts
# - public.apply_inventory_delta RPC
# - public.characters.active_loadout_name (text)

import os
import traceback
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands

from services.oc_service import get_active_character
from services import inventory_service


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


class InventoryCog(commands.GroupCog, group_name="inv", group_description="Inventory tools"):
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
        kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed

        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _fmt_item_line(self, it: dict) -> str:
        name = str(it.get("name") or "Item")
        ic = str(it.get("item_class") or "misc")
        wu = it.get("wu")
        wu_txt = f"WU `{int(wu)}`" if wu is not None else "WU `—`"
        return f"**{name}** • `{ic}` • {wu_txt}"

    def _chunk_lines(self, lines: list[str], max_chars: int = 900) -> list[str]:
        chunks: list[str] = []
        buf = ""
        for ln in lines:
            add = (ln + "\n")
            if len(buf) + len(add) > max_chars and buf:
                chunks.append(buf.rstrip())
                buf = ""
            buf += add
        if buf.strip():
            chunks.append(buf.rstrip())
        return chunks

    def _get_character_row(self, sb, character_id: str) -> Optional[dict]:
        try:
            res = (
                sb.table("characters")
                .select("character_id,name,active_loadout_name")
                .eq("character_id", str(character_id))
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _get_active_loadout_name(self, sb, character_id: str) -> Optional[str]:
        row = self._get_character_row(sb, character_id)
        if not row:
            return None
        raw = row.get("active_loadout_name")
        if not raw:
            return None
        txt = str(raw).strip()
        return txt or None

    def _set_active_loadout_name(self, sb, character_id: str, loadout_name: Optional[str]) -> bool:
        try:
            payload = {"active_loadout_name": loadout_name}
            sb.table("characters").update(payload).eq("character_id", str(character_id)).execute()
            return True
        except Exception:
            traceback.print_exc()
            return False

    # ── Autocomplete: items ────────────────────────────────────────────────────
    async def _item_autocomplete(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        sb = self.sb()
        guild_id = int(interaction.guild.id)
        q = (current or "").strip().lower()

        try:
            res = (
                sb.table("items")
                .select("item_id,name,is_active")
                .eq("guild_id", guild_id)
                .order("name", desc=False)
                .limit(50)
                .execute()
            )
            rows = getattr(res, "data", None) or []
        except Exception:
            rows = []

        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            if q and q not in name.lower():
                continue
            tag = "✅" if r.get("is_active", True) else "🗑️"
            out.append(app_commands.Choice(name=f"{tag} {name[:90]}", value=str(r["item_id"])))
        return out[:25]

    # ── Get active character ───────────────────────────────────────────────────
    def _get_active_character_id(self, sb, user_id: int) -> Optional[str]:
        oc = get_active_character(sb, int(user_id))
        if not oc:
            return None
        cid = oc.get("character_id")
        return str(cid) if cid else None

    # ───────────────────────────────────────────────────────────────────────────
    # Player: View inventory
    # ───────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="view", description="View your active OC inventory")
    async def view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            ent = (
                sb.table("inventory_entries")
                .select("item_id,qty")
                .eq("guild_id", guild_id)
                .eq("character_id", char_id)
                .order("updated_at", desc=True)
                .limit(200)
                .execute()
            )
            entries = getattr(ent, "data", None) or []
            if not entries:
                return await self._public(interaction, content="🎒 Inventory is empty.", ephemeral=False)

            item_ids = [str(e["item_id"]) for e in entries]
            items_res = (
                sb.table("items")
                .select("item_id,name,item_class,wu,sheet_url,is_active")
                .eq("guild_id", guild_id)
                .in_("item_id", item_ids)
                .execute()
            )
            items = getattr(items_res, "data", None) or []
            by_id = {str(i["item_id"]): i for i in items}

            lines: list[str] = []
            for e in entries:
                iid = str(e["item_id"])
                qty = int(e.get("qty") or 0)
                it = by_id.get(iid, {"name": "Unknown Item", "item_class": "?", "wu": None, "sheet_url": None})
                name = str(it.get("name") or "Item")
                ic = str(it.get("item_class") or "misc")
                wu = it.get("wu")
                wu_txt = f"{int(wu)}" if wu is not None else "—"
                lines.append(f"• **{name}** x`{qty}`  _(class `{ic}`, WU `{wu_txt}`)_")

            embed = discord.Embed(title="🎒 Inventory", color=discord.Color.dark_teal())
            chunks = self._chunk_lines(lines, max_chars=950)
            for idx, ch in enumerate(chunks[:5], start=1):
                embed.add_field(name=f"Items ({idx}/{len(chunks)})", value=ch, inline=False)
            embed.set_footer(text="Tip: use /inv item <item> to view full item details.")
            embed.timestamp = discord.utils.utcnow()

            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error loading inventory.", ephemeral=False)

    # ───────────────────────────────────────────────────────────────────────────
    # Item definitions
    # ───────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="item", description="View an item definition (WU, class, sheet link, etc.)")
    @app_commands.autocomplete(item_id=_item_autocomplete)
    @app_commands.describe(item_id="Item")
    async def item(self, interaction: discord.Interaction, item_id: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        it = inventory_service.get_item(sb, guild_id=guild_id, item_id=str(item_id))
        if not it:
            return await self._public(interaction, content="Item not found.", ephemeral=False)

        name = str(it.get("name") or "Item")
        ic = str(it.get("item_class") or "misc")
        wu = it.get("wu")
        url = (it.get("sheet_url") or "").strip() or None
        notes = (it.get("notes") or "").strip() or "—"
        active = bool(it.get("is_active", True))

        embed = discord.Embed(title=f"📦 {name}", color=discord.Color.blurple())
        embed.add_field(name="Class", value=f"`{ic}`", inline=True)
        embed.add_field(name="WU", value=f"`{int(wu)}`" if wu is not None else "`—`", inline=True)
        embed.add_field(name="Active", value="✅ Yes" if active else "🗑️ No", inline=True)
        if url:
            embed.add_field(name="Sheet", value=url, inline=False)
        embed.add_field(name="Notes", value=(notes[:900] + "…") if len(notes) > 900 else notes, inline=False)
        embed.set_footer(text=f"Item ID: {str(it['item_id'])}")
        embed.timestamp = discord.utils.utcnow()
        return await self._public(interaction, embed=embed, ephemeral=False)

    @app_commands.command(name="define_item", description="Staff: Create a new item definition (prevents duplicate names)")
    @app_commands.describe(
        name="Item name (unique per guild, case-insensitive)",
        item_class="e.g. weapon, armor, consumable, misc",
        wu="Weight units (optional)",
        sheet_url="Link to item sheet (optional)",
        notes="Extra notes (optional)",
    )
    async def define_item(
        self,
        interaction: discord.Interaction,
        name: str,
        item_class: str = "misc",
        wu: Optional[int] = None,
        sheet_url: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        try:
            it = inventory_service.upsert_item(
                sb,
                guild_id=guild_id,
                name=name,
                item_class=item_class,
                wu=wu,
                sheet_url=sheet_url,
                notes=notes,
            )
            return await self._public(
                interaction,
                content=f"✅ Item ready: {self._fmt_item_line(it)}\nID: `{str(it['item_id'])[:8]}`",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error defining item.", ephemeral=False)

    # ───────────────────────────────────────────────────────────────────────────
    # Staff: grant / take inventory
    # ───────────────────────────────────────────────────────────────────────────
    @app_commands.command(name="grant", description="Staff: Grant items to a character (your active OC by default)")
    @app_commands.autocomplete(item_id=_item_autocomplete)
    @app_commands.describe(
        item_id="Item to grant",
        qty="How many to add",
        target_discord_id="Optional: grant to another player's active OC (Discord ID)",
        note="Optional note for the log",
    )
    async def grant(
        self,
        interaction: discord.Interaction,
        item_id: str,
        qty: int,
        target_discord_id: Optional[str] = None,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if qty <= 0:
            return await self._public(interaction, content="qty must be > 0.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        target_id = interaction.user.id
        if target_discord_id:
            if not str(target_discord_id).isdigit():
                return await self._public(interaction, content="target_discord_id must be a numeric Discord ID.", ephemeral=False)
            target_id = int(target_discord_id)

        char_id = self._get_active_character_id(sb, target_id)
        if not char_id:
            who = f"<@{target_id}>" if target_id != interaction.user.id else "you"
            return await self._public(interaction, content=f"No active OC set for {who}.", ephemeral=False)

        it = inventory_service.get_item(sb, guild_id=guild_id, item_id=str(item_id))
        if not it:
            return await self._public(interaction, content="Item not found.", ephemeral=False)

        try:
            out = inventory_service.apply_delta(
                sb,
                guild_id=guild_id,
                character_id=char_id,
                item_id=str(item_id),
                delta=int(qty),
                actor_discord_id=int(interaction.user.id),
                context="STAFF_GRANT",
                note=note,
            )
            new_qty = out.get("qty")
            return await self._public(
                interaction,
                content=f"✅ Granted **{it.get('name','Item')}** x`{qty}` to <@{target_id}>. New qty: `{new_qty}`",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error granting item.", ephemeral=False)

    @app_commands.command(name="take", description="Staff: Remove items from a character (your active OC by default)")
    @app_commands.autocomplete(item_id=_item_autocomplete)
    @app_commands.describe(
        item_id="Item to remove",
        qty="How many to remove",
        target_discord_id="Optional: take from another player's active OC (Discord ID)",
        note="Optional note for the log",
    )
    async def take(
        self,
        interaction: discord.Interaction,
        item_id: str,
        qty: int,
        target_discord_id: Optional[str] = None,
        note: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=False)

        if not self._staff_ok(interaction):
            return await self._private(interaction, "❌ Staff only.")
        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        if qty <= 0:
            return await self._public(interaction, content="qty must be > 0.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)

        target_id = interaction.user.id
        if target_discord_id:
            if not str(target_discord_id).isdigit():
                return await self._public(interaction, content="target_discord_id must be a numeric Discord ID.", ephemeral=False)
            target_id = int(target_discord_id)

        char_id = self._get_active_character_id(sb, target_id)
        if not char_id:
            who = f"<@{target_id}>" if target_id != interaction.user.id else "you"
            return await self._public(interaction, content=f"No active OC set for {who}.", ephemeral=False)

        it = inventory_service.get_item(sb, guild_id=guild_id, item_id=str(item_id))
        if not it:
            return await self._public(interaction, content="Item not found.", ephemeral=False)

        try:
            out = inventory_service.apply_delta(
                sb,
                guild_id=guild_id,
                character_id=char_id,
                item_id=str(item_id),
                delta=-int(qty),
                actor_discord_id=int(interaction.user.id),
                context="STAFF_TAKE",
                note=note,
            )
            new_qty = out.get("qty")
            return await self._public(
                interaction,
                content=f"✅ Removed **{it.get('name','Item')}** x`{qty}` from <@{target_id}>. New qty: `{new_qty}`",
                ephemeral=False,
            )
        except RuntimeError as ex:
            if str(ex) == "INSUFFICIENT_QTY":
                return await self._public(interaction, content="❌ Not enough quantity to remove.", ephemeral=False)
            raise
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error removing item.", ephemeral=False)

    # ───────────────────────────────────────────────────────────────────────────
    # Loadouts
    # ───────────────────────────────────────────────────────────────────────────
    def _loadout_fetch(self, sb, guild_id: int, character_id: str, loadout_name: str) -> Optional[dict]:
        res = (
            sb.table("inventory_loadouts")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("loadout_name", str(loadout_name))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    @app_commands.command(name="loadout_list", description="List your saved loadouts")
    async def loadout_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        sb = self.sb()
        guild_id = int(interaction.guild.id)

        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            active_loadout = self._get_active_loadout_name(sb, char_id)

            res = (
                sb.table("inventory_loadouts")
                .select("loadout_name,updated_at")
                .eq("guild_id", guild_id)
                .eq("character_id", char_id)
                .order("updated_at", desc=True)
                .limit(50)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                return await self._public(interaction, content="You have no loadouts yet. Use `/inv loadout_save`.", ephemeral=False)

            embed = discord.Embed(title="🧰 Loadouts", color=discord.Color.gold())
            lines = []
            for r in rows:
                loadout_name = str(r["loadout_name"])
                marker = "⭐ " if active_loadout and loadout_name == active_loadout else ""
                lines.append(
                    f"• {marker}**{loadout_name}**  _(updated `{str(r.get('updated_at',''))[:19]}`)_"
                )

            embed.description = "\n".join(lines[:30])
            embed.set_footer(text="⭐ = active equipped loadout")
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=False)
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error listing loadouts.", ephemeral=False)

    @app_commands.command(name="loadout_save", description="Save/overwrite a loadout from your current inventory")
    @app_commands.describe(name="Loadout name (e.g., 'Forest Run', 'PvP Kit')")
    async def loadout_save(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            inv = (
                sb.table("inventory_entries")
                .select("item_id,qty")
                .eq("guild_id", guild_id)
                .eq("character_id", char_id)
                .execute()
            )
            entries = getattr(inv, "data", None) or []
            items_map = {str(e["item_id"]): int(e.get("qty") or 0) for e in entries if int(e.get("qty") or 0) > 0}

            existing = self._loadout_fetch(sb, guild_id, char_id, name)
            if existing:
                sb.table("inventory_loadouts").update({"items": items_map}).eq("guild_id", guild_id).eq("character_id", char_id).eq("loadout_name", name).execute()
            else:
                sb.table("inventory_loadouts").insert(
                    {"guild_id": guild_id, "character_id": char_id, "loadout_name": name, "items": items_map}
                ).execute()

            return await self._public(interaction, content=f"✅ Saved loadout **{name}** ({len(items_map)} item types).", ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error saving loadout.", ephemeral=False)

    @app_commands.command(name="loadout_print", description="Print a saved loadout (item list) for your active OC")
    @app_commands.describe(name="Loadout name")
    async def loadout_print(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            active_loadout = self._get_active_loadout_name(sb, char_id)
            lo = self._loadout_fetch(sb, guild_id, char_id, name)
            if not lo:
                return await self._public(interaction, content="Loadout not found. Use `/inv loadout_list`.", ephemeral=False)

            items_map = lo.get("items") or {}
            if not isinstance(items_map, dict) or not items_map:
                return await self._public(interaction, content=f"Loadout **{name}** is empty.", ephemeral=False)

            item_ids = list(items_map.keys())
            items_res = (
                sb.table("items")
                .select("item_id,name,item_class,wu,sheet_url")
                .eq("guild_id", guild_id)
                .in_("item_id", item_ids)
                .execute()
            )
            items = getattr(items_res, "data", None) or []
            by_id = {str(i["item_id"]): i for i in items}

            lines: list[str] = []
            for iid, qty in items_map.items():
                it = by_id.get(str(iid), {"name": "Unknown Item", "item_class": "?", "wu": None, "sheet_url": None})
                nm = str(it.get("name") or "Item")
                ic = str(it.get("item_class") or "misc")
                wu = it.get("wu")
                wu_txt = f"{int(wu)}" if wu is not None else "—"
                url = (it.get("sheet_url") or "").strip()
                url_txt = f" • {url}" if url else ""
                lines.append(f"• **{nm}** x`{int(qty)}`  _(class `{ic}`, WU `{wu_txt}`)_{url_txt}")

            title_prefix = "⭐ " if active_loadout and active_loadout == name else ""
            embed = discord.Embed(title=f"{title_prefix}🧰 Loadout: {name}", color=discord.Color.gold())
            chunks = self._chunk_lines(lines, max_chars=950)
            for idx, ch in enumerate(chunks[:5], start=1):
                embed.add_field(name=f"Items ({idx}/{len(chunks)})", value=ch, inline=False)
            embed.set_footer(text="⭐ = currently equipped loadout")
            embed.timestamp = discord.utils.utcnow()
            return await self._public(interaction, embed=embed, ephemeral=False)

        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error printing loadout.", ephemeral=False)

    @app_commands.command(name="loadout_delete", description="Delete a saved loadout")
    @app_commands.describe(name="Loadout name")
    async def loadout_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            active_loadout = self._get_active_loadout_name(sb, char_id)
            sb.table("inventory_loadouts").delete().eq("guild_id", guild_id).eq("character_id", char_id).eq("loadout_name", name).execute()

            if active_loadout and active_loadout == name:
                self._set_active_loadout_name(sb, char_id, None)

            return await self._public(interaction, content=f"🗑️ Deleted loadout **{name}**.", ephemeral=False)
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error deleting loadout.", ephemeral=False)

    @app_commands.command(name="loadout_add", description="Add an item to a loadout (does NOT change your inventory)")
    @app_commands.autocomplete(item_id=_item_autocomplete)
    @app_commands.describe(name="Loadout name", item_id="Item", qty="Quantity to set/add (default 1)")
    async def loadout_add(self, interaction: discord.Interaction, name: str, item_id: str, qty: int = 1):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)
        if qty <= 0:
            return await self._public(interaction, content="qty must be > 0.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            lo = self._loadout_fetch(sb, guild_id, char_id, name)
            items_map = {}
            if lo and isinstance(lo.get("items"), dict):
                items_map = dict(lo["items"])

            items_map[str(item_id)] = int(qty)

            if lo:
                sb.table("inventory_loadouts").update({"items": items_map}).eq("guild_id", guild_id).eq("character_id", char_id).eq("loadout_name", name).execute()
            else:
                sb.table("inventory_loadouts").insert(
                    {"guild_id": guild_id, "character_id": char_id, "loadout_name": name, "items": items_map}
                ).execute()

            it = inventory_service.get_item(sb, guild_id=guild_id, item_id=str(item_id)) or {}
            return await self._public(
                interaction,
                content=f"✅ Loadout **{name}** now includes **{it.get('name','Item')}** x`{qty}`.",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error updating loadout.", ephemeral=False)

    @app_commands.command(name="loadout_remove", description="Remove an item from a loadout (does NOT change your inventory)")
    @app_commands.autocomplete(item_id=_item_autocomplete)
    @app_commands.describe(name="Loadout name", item_id="Item")
    async def loadout_remove(self, interaction: discord.Interaction, name: str, item_id: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            lo = self._loadout_fetch(sb, guild_id, char_id, name)
            if not lo or not isinstance(lo.get("items"), dict):
                return await self._public(interaction, content="Loadout not found.", ephemeral=False)

            items_map = dict(lo["items"])
            if str(item_id) not in items_map:
                return await self._public(interaction, content="That item isn’t in this loadout.", ephemeral=False)

            items_map.pop(str(item_id), None)

            sb.table("inventory_loadouts").update({"items": items_map}).eq("guild_id", guild_id).eq("character_id", char_id).eq("loadout_name", name).execute()

            it = inventory_service.get_item(sb, guild_id=guild_id, item_id=str(item_id)) or {}
            return await self._public(
                interaction,
                content=f"✅ Removed **{it.get('name','Item')}** from loadout **{name}**.",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error updating loadout.", ephemeral=False)

    @app_commands.command(name="loadout_equip", description="Set one of your saved loadouts as active/equipped")
    @app_commands.describe(name="Loadout name")
    async def loadout_equip(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")
        name = (name or "").strip()
        if not name:
            return await self._public(interaction, content="Name required.", ephemeral=False)

        sb = self.sb()
        guild_id = int(interaction.guild.id)
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            lo = self._loadout_fetch(sb, guild_id, char_id, name)
            if not lo:
                return await self._public(interaction, content="Loadout not found. Use `/inv loadout_list`.", ephemeral=False)

            ok = self._set_active_loadout_name(sb, char_id, name)
            if not ok:
                return await self._public(interaction, content="Failed to equip loadout.", ephemeral=False)

            return await self._public(
                interaction,
                content=f"⭐ Equipped loadout **{name}**.",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error equipping loadout.", ephemeral=False)

    @app_commands.command(name="loadout_active", description="Show your currently equipped loadout")
    async def loadout_active(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not interaction.guild:
            return await self._private(interaction, "Use this in a server, not DMs.")

        sb = self.sb()
        char_id = self._get_active_character_id(sb, interaction.user.id)
        if not char_id:
            return await self._public(interaction, content="No active OC set. Use `/oc select <name>`.", ephemeral=False)

        try:
            active_loadout = self._get_active_loadout_name(sb, char_id)
            if not active_loadout:
                return await self._public(
                    interaction,
                    content="No active loadout equipped.",
                    ephemeral=False,
                )

            return await self._public(
                interaction,
                content=f"⭐ Current equipped loadout: **{active_loadout}**",
                ephemeral=False,
            )
        except Exception:
            traceback.print_exc()
            return await self._public(interaction, content="Server error reading active loadout.", ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(InventoryCog(bot))