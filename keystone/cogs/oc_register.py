# cogs/oc_register.py
import os
import re
import socket
import traceback
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

MAX_OCS_PER_USER = 2
NAME_RE = re.compile(r"^[A-Za-z0-9 _'\-]{1,64}$")

STATS_STAFF_ROLE_ID = 1374730886490357822

# ---- stats keys (must match oc_stats columns) ----
STAT_FIELDS = [
    ("dexterity", "Dexterity"),
    ("reflexes", "Reflexes"),
    ("strength", "Strength"),
    ("durability", "Durability"),
    ("mana", "Mana"),
    ("magic_output", "Magic Output"),
    ("magic_control", "Magic Control"),
]

def get_supabase_client():
    try:
        from supabase import create_client
    except Exception as e:
        raise RuntimeError(f"Supabase client import failed: {e}")

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set.")

    print(f"[supabase] URL seen by bot: {url!r}")
    if not url.startswith("https://"):
        raise RuntimeError("SUPABASE_URL must start with https://")
    host = urlparse(url).netloc
    try:
        ip = socket.gethostbyname(host)
        print(f"[supabase] DNS OK: {host} -> {ip}")
    except Exception as e:
        raise RuntimeError(f"Cannot resolve {host}. Check SUPABASE_URL. ({e})")

    return create_client(url, key)

def _has_stats_staff_role(member: discord.Member) -> bool:
    return any(r.id == STATS_STAFF_ROLE_ID for r in getattr(member, "roles", []))

class AllocateStatsModal(discord.ui.Modal, title="Allocate Starting Stats"):
    def __init__(self, supabase, oc_id: str, oc_name: str, owner_discord_id: str):
        super().__init__(timeout=300)
        self.supabase = supabase
        self.oc_id = oc_id
        self.oc_name = oc_name
        self.owner_discord_id = owner_discord_id

        # 7 required fields
        self.dexterity = discord.ui.TextInput(label="Dexterity", placeholder=">= 20", required=True, max_length=4)
        self.reflexes = discord.ui.TextInput(label="Reflexes", placeholder=">= 20", required=True, max_length=4)
        self.strength = discord.ui.TextInput(label="Strength", placeholder=">= 20", required=True, max_length=4)
        self.durability = discord.ui.TextInput(label="Durability", placeholder=">= 20", required=True, max_length=4)
        self.mana = discord.ui.TextInput(label="Mana", placeholder=">= 20", required=True, max_length=4)
        self.magic_output = discord.ui.TextInput(label="Magic Output", placeholder=">= 20", required=True, max_length=4)
        self.magic_control = discord.ui.TextInput(label="Magic Control", placeholder=">= 20", required=True, max_length=4)

        for item in (
            self.dexterity, self.reflexes, self.strength, self.durability,
            self.mana, self.magic_output, self.magic_control
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        # Permission: owner OR stats staff
        if str(interaction.user.id) != self.owner_discord_id:
            if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _has_stats_staff_role(interaction.user):
                return await interaction.response.send_message("You can only allocate stats for your own OC.", ephemeral=True)

        # Parse ints
        try:
            values = {
                "dexterity": int(self.dexterity.value.strip()),
                "reflexes": int(self.reflexes.value.strip()),
                "strength": int(self.strength.value.strip()),
                "durability": int(self.durability.value.strip()),
                "mana": int(self.mana.value.strip()),
                "magic_output": int(self.magic_output.value.strip()),
                "magic_control": int(self.magic_control.value.strip()),
            }
        except ValueError:
            return await interaction.response.send_message("All stats must be whole numbers.", ephemeral=True)

        # Validate minimums only (your chosen rule)
        below = [label for key, label in STAT_FIELDS if values.get(key, 0) < 20]
        if below:
            return await interaction.response.send_message(
                "These stats must be **at least 20**:\n- " + "\n- ".join(below),
                ephemeral=True
            )

        actor_id = str(interaction.user.id)

        # Fetch current stats to compute deltas for logs (handles re-submits too)
        try:
            cur_res = self.supabase.table("oc_stats").select("*").eq("oc_id", self.oc_id).limit(1).execute()
            cur_row = (getattr(cur_res, "data", None) or [])
            cur = cur_row[0] if cur_row else {"oc_id": self.oc_id}
        except Exception:
            cur = {"oc_id": self.oc_id}

        # Upsert snapshot
        try:
            payload = {"oc_id": self.oc_id, **values}
            up = self.supabase.table("oc_stats").upsert(payload).execute()
            if hasattr(up, "error") and up.error:
                print(f"[stats_modal] upsert error: {up.error}")
                return await interaction.response.send_message("DB error saving stats.", ephemeral=True)
        except Exception as e:
            print(f"[stats_modal] upsert exception: {e}")
            traceback.print_exc()
            return await interaction.response.send_message("Server error saving stats.", ephemeral=True)

        # Log each stat change
        try:
            logs = []
            for key, _label in STAT_FIELDS:
                old_val = int(cur.get(key, 0) or 0)
                new_val = int(values[key])
                delta = new_val - old_val
                if delta == 0:
                    continue
                logs.append({
                    "oc_id": self.oc_id,
                    "stat_key": key,
                    "delta": delta,
                    "old_value": old_val,
                    "new_value": new_val,
                    "actor_discord_id": actor_id,
                    "reason": "initial allocation" if old_val == 0 else "re-allocation",
                })
            if logs:
                self.supabase.table("oc_stat_logs").insert(logs).execute()
        except Exception as e:
            print(f"[stats_modal] log insert exception: {e}")
            traceback.print_exc()
            # Don't block success if logs fail

        # Mark status pending review
        try:
            self.supabase.table("ocs").update({"stats_status": "pending_review"}).eq("oc_id", self.oc_id).execute()
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ Stats saved for **{self.oc_name}** and sent for staff review.",
            ephemeral=True
        )

class OCRegisterView(discord.ui.View):
    def __init__(self, supabase, oc_id: str, oc_name: str, owner_discord_id: str):
        super().__init__(timeout=600)
        self.supabase = supabase
        self.oc_id = oc_id
        self.oc_name = oc_name
        self.owner_discord_id = owner_discord_id

    @discord.ui.button(label="Allocate Stats", style=discord.ButtonStyle.primary)
    async def allocate(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only owner or stats staff can open
        if str(interaction.user.id) != self.owner_discord_id:
            if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _has_stats_staff_role(interaction.user):
                return await interaction.response.send_message("Only the OC owner (or stats staff) can do this.", ephemeral=True)

        modal = AllocateStatsModal(self.supabase, self.oc_id, self.oc_name, self.owner_discord_id)
        await interaction.response.send_modal(modal)


class OCRegister(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="oc_register", description="Register a new OC for yourself")
    @app_commands.guilds(SKYFALL_GUILD)
    @app_commands.describe(name="Your character's name (max 64 characters)")
    async def oc_register(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)

        raw = (name or "").strip()
        if not raw:
            return await interaction.followup.send("Please provide a valid OC name.")
        if len(raw) > 64:
            return await interaction.followup.send("OC name is too long (max **64** chars).")
        if not NAME_RE.match(raw):
            return await interaction.followup.send(
                "OC name has invalid characters. Allowed: letters, numbers, space, `- _ '`. "
                "Example: `Edwyn Faerber` or `Mirabel_Silva`."
            )

        owner_discord_id = str(interaction.user.id)

        try:
            supabase = get_supabase_client()
        except Exception as e:
            print(f"[oc_register] Supabase init error: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server configuration error. Please notify an admin.")

        # Fetch existing OCs
        try:
            res_existing = (
                supabase.table("ocs")
                .select("oc_id, oc_name")
                .eq("owner_discord_id", owner_discord_id)
                .execute()
            )
        except Exception as e:
            print(f"[oc_register] Supabase SELECT failed: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Server error fetching your OCs. Please try again.")

        existing = (getattr(res_existing, "data", None) or [])
        for row in existing:
            if row["oc_name"].casefold() == raw.casefold():
                return await interaction.followup.send(f"OC **{row['oc_name']}** is already registered. (`{row['oc_id']}`)")

        if MAX_OCS_PER_USER > 0 and len(existing) >= MAX_OCS_PER_USER:
            return await interaction.followup.send(
                f"You've reached the limit of **{MAX_OCS_PER_USER}** OCs. Ask staff if you need more."
            )

        # Insert OC (keeps semantics clean)
        try:
            insert_payload = {
                "owner_discord_id": owner_discord_id,
                "oc_name": raw,
                "stats_status": "unallocated",
            }
            ins = supabase.table("ocs").insert(insert_payload).execute()
            if hasattr(ins, "error") and ins.error:
                print(f"[oc_register] INSERT error payload: {ins.error}")
                return await interaction.followup.send("Could not register OC (DB policy error).")

            # Grab created row (supabase sometimes returns it in ins.data)
            data = (getattr(ins, "data", None) or [])
            oc_row = data[0] if data else None

            if not oc_row:
                # fallback select
                res_row = (
                    supabase.table("ocs")
                    .select("oc_id, owner_discord_id, oc_name, stats_status")
                    .eq("owner_discord_id", owner_discord_id)
                    .eq("oc_name", raw)
                    .single()
                    .execute()
                )
                if hasattr(res_row, "error") and res_row.error:
                    print(f"[oc_register] SELECT-after-insert error payload: {res_row.error}")
                    return await interaction.followup.send("Could not register OC (DB policy error).")
                oc_row = getattr(res_row, "data", None)

        except Exception as e:
            print(f"[oc_register] Supabase INSERT/SELECT failed: {e}")
            traceback.print_exc()
            return await interaction.followup.send("Could not register OC (server error).")

        if not oc_row:
            return await interaction.followup.send("Could not register OC. Please try again later.")

        oc_id = oc_row["oc_id"]

        # Ensure stats row exists (zeros)
        try:
            supabase.table("oc_stats").upsert({"oc_id": oc_id}).execute()
        except Exception as e:
            print(f"[oc_register] oc_stats upsert failed: {e}")

        embed = discord.Embed(
            title="OC Registered",
            description=(
                f"✅ OC **{raw}** registered!\n"
                f"Now allocate your starting stats.\n\n"
                f"**Status:** `unallocated`"
            ),
            color=discord.Color.orange()
        )
        embed.add_field(name="OC ID", value=f"`{oc_id}`", inline=False)

        view = OCRegisterView(supabase, oc_id, raw, owner_discord_id)
        return await interaction.followup.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(OCRegister(bot))
