# cogs/give_item.py
from __future__ import annotations

import json
import discord
from discord import app_commands
from discord.ext import commands

from config.skyfall import SKYFALL_GUILD
from services.db import get_supabase_client


# ---------------- OC + Item autocomplete ----------------
async def oc_name_autocomplete_all(interaction: discord.Interaction, current: str):
    """Show ALL OC names (server-wide)."""
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("ocs").select("oc_name")
        if cur:
            q = q.ilike("oc_name", f"%{cur}%")
        res = q.order("oc_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["oc_name"], value=r["oc_name"]) for r in rows if r.get("oc_name")]
    except Exception:
        return []


async def item_name_autocomplete_all(interaction: discord.Interaction, current: str):
    """Show ALL item names (catalog-wide)."""
    sb = get_supabase_client()
    cur = (current or "").strip()
    try:
        q = sb.table("item_catalog").select("item_name")
        if cur:
            q = q.ilike("item_name", f"%{cur}%")
        res = q.order("item_name").limit(25).execute()
        rows = getattr(res, "data", None) or []
        return [app_commands.Choice(name=r["item_name"], value=r["item_name"]) for r in rows if r.get("item_name")]
    except Exception:
        return []


class GiveItem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ----------------------
    # /give_item
    # ----------------------
    @app_commands.command(
        name="give_item",
        description="GM: create/update an item and grant it to an OC",
    )
    @app_commands.describe(
        player="Discord user who owns the OC",
        oc_name="OC name",
        item_name="Name of the item",
        quantity="How many to grant (+) or remove (-). Default 1.",
        effect="What it does (optional)",
        duration="How long it lasts (optional)",
        active="Is this item active/available? Default true.",
        doc_url="Google Doc link for the item (optional)",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_all, item_name=item_name_autocomplete_all)
    async def give_item(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        oc_name: str,
        item_name: str,
        quantity: int = 1,
        effect: str | None = None,
        duration: str | None = None,
        active: bool = True,
        doc_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)

        oc_name = (oc_name or "").strip()
        item_name = (item_name or "").strip()
        if not oc_name or not item_name:
            return await interaction.followup.send("Please provide both `oc_name` and `item_name`.", ephemeral=False)
        if quantity == 0:
            return await interaction.followup.send("Quantity is 0 — nothing to do.", ephemeral=False)

        sb = get_supabase_client()

        payload = {
            "p_owner_discord_id": str(player.id),
            "p_oc_name": oc_name,
            "p_item_name": item_name,
            "p_effect": effect,
            "p_duration": duration,
            "p_active": active,
            "p_doc_url": doc_url,
            "p_quantity": int(quantity),
            "p_reason": "discord /give_item",
            "p_ctx": {"by": str(interaction.user.id), "interaction_id": str(interaction.id)},
        }
        print(f"[give_item] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = sb.rpc("upsert_item_and_grant_to_oc", payload).execute()

            # postgrest-py sometimes surfaces errors as exception; sometimes as res.error
            err = getattr(res, "error", None)
            if err:
                print(f"[give_item] RPC error: {err}")
                return await interaction.followup.send(f"RPC error: {err}", ephemeral=False)

        except Exception as e:
            print(f"[give_item] RPC exception: {repr(e)}")
            return await interaction.followup.send(f"RPC exception: {e}", ephemeral=False)

        data = getattr(res, "data", None) or []
        if not data:
            print("[give_item] RPC returned no data")
            return await interaction.followup.send(
                "RPC returned no data. This usually means the OC wasn't found for that player, or the function returned nothing.",
                ephemeral=False,
            )

        row = data[0]
        print(f"[give_item] RPC result: {row}")

        color = discord.Color.green() if quantity > 0 else discord.Color.red()
        action = "Granted" if quantity > 0 else "Removed"

        embed = discord.Embed(
            title=f"{action} {abs(quantity)} × {row.get('item_name', item_name)}",
            description=f"**OC:** {row.get('oc_name', oc_name)} • **Owner:** <@{player.id}>",
            color=color,
        )
        if row.get("effect"):
            embed.add_field(name="Effect", value=str(row["effect"])[:1024], inline=False)
        if row.get("duration"):
            embed.add_field(name="Duration", value=str(row["duration"])[:1024], inline=True)
        if "new_quantity" in row:
            embed.add_field(name="New Quantity", value=str(row["new_quantity"]), inline=True)

        view = None
        if row.get("doc_url"):
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="View Item Doc", url=row["doc_url"]))

        await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    # ----------------------
    # /take_item
    # ----------------------
    @app_commands.command(
        name="take_item",
        description="GM: remove items from an OC (wrapper around give with negative quantity)",
    )
    @app_commands.describe(
        player="Discord user who owns the OC",
        oc_name="OC name",
        item_name="Item name",
        quantity="How many to remove (positive number). Default 1.",
    )
    @app_commands.autocomplete(oc_name=oc_name_autocomplete_all, item_name=item_name_autocomplete_all)
    async def take_item(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        oc_name: str,
        item_name: str,
        quantity: int = 1,
    ):
        await interaction.response.defer(ephemeral=False)

        oc_name = (oc_name or "").strip()
        item_name = (item_name or "").strip()
        if not oc_name or not item_name:
            return await interaction.followup.send("Please provide both `oc_name` and `item_name`.", ephemeral=False)

        negative_quantity = -(abs(int(quantity)))
        if negative_quantity == 0:
            return await interaction.followup.send("Quantity is 0 — nothing to do.", ephemeral=False)

        sb = get_supabase_client()

        payload = {
            "p_owner_discord_id": str(player.id),
            "p_oc_name": oc_name,
            "p_item_name": item_name,
            "p_effect": None,
            "p_duration": None,
            "p_active": True,
            "p_doc_url": None,
            "p_quantity": negative_quantity,
            "p_reason": "discord /take_item",
            "p_ctx": {"by": str(interaction.user.id), "interaction_id": str(interaction.id)},
        }
        print(f"[take_item] payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            res = sb.rpc("upsert_item_and_grant_to_oc", payload).execute()
            err = getattr(res, "error", None)
            if err:
                print(f"[take_item] RPC error: {err}")
                return await interaction.followup.send(f"RPC error: {err}", ephemeral=False)
        except Exception as e:
            print(f"[take_item] RPC exception: {repr(e)}")
            return await interaction.followup.send(f"RPC exception: {e}", ephemeral=False)

        data = getattr(res, "data", None) or []
        if not data:
            print("[take_item] RPC returned no data")
            return await interaction.followup.send(
                "RPC returned no data. This usually means the OC wasn't found for that player, or the function returned nothing.",
                ephemeral=False,
            )

        row = data[0]
        print(f"[take_item] RPC result: {row}")

        embed = discord.Embed(
            title=f"Removed {abs(negative_quantity)} × {row.get('item_name', item_name)}",
            description=f"**OC:** {row.get('oc_name', oc_name)} • **Owner:** <@{player.id}>",
            color=discord.Color.red(),
        )
        if row.get("effect"):
            embed.add_field(name="Effect", value=str(row["effect"])[:1024], inline=False)
        if row.get("duration"):
            embed.add_field(name="Duration", value=str(row["duration"])[:1024], inline=True)
        if "new_quantity" in row:
            embed.add_field(name="New Quantity", value=str(row["new_quantity"]), inline=True)

        view = None
        if row.get("doc_url"):
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="View Item Doc", url=row["doc_url"]))

        await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    # ----------------------
    # /item_update
    # ----------------------
    @app_commands.command(
        name="item_update",
        description="GM: update an item in the catalog",
    )
    @app_commands.describe(
        item_name="Existing item name to update",
        new_item_name="New name (optional)",
        effect="New effect (optional)",
        duration="New duration (optional)",
        active="Set active true/false (optional)",
        doc_url="New Google Doc URL (optional)",
    )
    @app_commands.autocomplete(item_name=item_name_autocomplete_all)
    async def item_update(
        self,
        interaction: discord.Interaction,
        item_name: str,
        new_item_name: str | None = None,
        effect: str | None = None,
        duration: str | None = None,
        active: bool | None = None,
        doc_url: str | None = None,
    ):
        await interaction.response.defer(ephemeral=False)

        item_name = (item_name or "").strip()
        if not item_name:
            return await interaction.followup.send("Provide the current `item_name` to update.", ephemeral=False)

        updates: dict[str, object] = {}
        if new_item_name is not None and new_item_name.strip():
            updates["item_name"] = new_item_name.strip()
        if effect is not None:
            updates["effect"] = effect
        if duration is not None:
            updates["duration"] = duration
        if active is not None:
            updates["active"] = active
        if doc_url is not None:
            updates["doc_url"] = doc_url

        if not updates:
            return await interaction.followup.send("No fields to update were provided.", ephemeral=False)

        sb = get_supabase_client()

        try:
            res = (
                sb.table("item_catalog")
                .update(updates)
                .eq("item_name", item_name)
                .select("*")
                .limit(1)
                .execute()
            )
        except Exception as e:
            print(f"[item_update] exception: {repr(e)}")
            return await interaction.followup.send(f"Could not update item (exception): {e}", ephemeral=False)

        rows = getattr(res, "data", None) or []
        if not rows:
            return await interaction.followup.send("Item not found or no changes applied.", ephemeral=False)

        data = rows[0]
        embed = discord.Embed(
            title=f"Item Updated: {data.get('item_name', new_item_name or item_name)}",
            color=discord.Color.blurple(),
        )
        for k in ("effect", "duration", "active", "doc_url"):
            if k in data and data[k] is not None:
                embed.add_field(name=k.capitalize(), value=str(data[k])[:1024], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ----------------------
    # /item_deactivate
    # ----------------------
    @app_commands.command(
        name="item_deactivate",
        description="GM: mark an item inactive (soft delete)",
    )
    @app_commands.describe(item_name="Item name to deactivate")
    @app_commands.autocomplete(item_name=item_name_autocomplete_all)
    async def item_deactivate(self, interaction: discord.Interaction, item_name: str):
        await interaction.response.defer(ephemeral=False)

        item_name = (item_name or "").strip()
        if not item_name:
            return await interaction.followup.send("Provide an `item_name`.", ephemeral=False)

        sb = get_supabase_client()

        try:
            res = (
                sb.table("item_catalog")
                .update({"active": False})
                .eq("item_name", item_name)
                .select("item_id, item_name, active")
                .limit(1)
                .execute()
            )
        except Exception as e:
            print(f"[item_deactivate] exception: {repr(e)}")
            return await interaction.followup.send(f"Could not deactivate item (exception): {e}", ephemeral=False)

        rows = getattr(res, "data", None) or []
        if not rows:
            return await interaction.followup.send("Item not found.", ephemeral=False)

        row = rows[0]
        embed = discord.Embed(
            title=f"Item Deactivated: {row.get('item_name', item_name)}",
            description=f"`active` → **{row.get('active')}**",
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveItem(bot))
