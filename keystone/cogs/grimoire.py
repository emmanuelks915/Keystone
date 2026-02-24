# cogs/grimoire.py
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

# ── Grimoire configuration ─────────────────────────────────────────────────────
GRIMOIRE_TYPES = [
    {"name": "Simple",        "probability": 80, "emoji": "📘", "color": 0x3498db},
    {"name": "Uncommon",      "probability": 10, "emoji": "📗", "color": 0x2ecc71},
    {"name": "Coverless",     "probability": 2,  "emoji": "📓", "color": 0x95a5a6},
    {"name": "Ancestral",     "probability": 2,  "emoji": "📙", "color": 0xe67e22},
    {"name": "Possessed",     "probability": 2,  "emoji": "📕", "color": 0xe74c3c},
    {"name": "Reincarnated",  "probability": 2,  "emoji": "📖", "color": 0x9b59b6},
    {"name": "Dual",          "probability": 2,  "emoji": "📚", "color": 0xf1c40f},
]

def determine_grimoire_type(roll: int) -> Dict:
    cumulative = 0
    for g in GRIMOIRE_TYPES:
        cumulative += g["probability"]
        if roll <= cumulative:
            return g
    return GRIMOIRE_TYPES[0]

# 🔹 Rules (Skyfall)
# Anything not Simple triggers the 3-day OC sheet deadline once CLAIMED.
NON_SIMPLE_GRIMOIRE_TYPES = {"Uncommon", "Coverless", "Ancestral", "Possessed", "Reincarnated", "Dual"}
OC_SHEET_DEADLINE_DAYS = 3

# 🔹 Guild-scope: Skyfall RP only
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)

# ── Role IDs (Skyfall) ────────────────────────────────────────────────────────
BOOSTER_ROLE_ID = 1376241549960020189
SUPPORTER_ROLE_ID = 1377741787891761262
POWER_SUPPORTER_ROLE_ID = 1396236912951562270

# ── Staff alert channel ────────────────────────────────────────────────────────
STAFF_ALERT_CHANNEL_ID = 1456440906633707520

# ── Supabase helper ────────────────────────────────────────────────────────────
def get_supabase_client():
    """Lazy-init Supabase so missing env/packages don't crash cog load."""
    try:
        from supabase import create_client
    except Exception as e:
        raise RuntimeError(f"Supabase import failed: {e}")

    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set.")
    return create_client(url, key)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# ── Views ─────────────────────────────────────────────────────────────────────
class OCSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.oc_slot: Optional[str] = None

    @discord.ui.select(
        placeholder="Select which OC is rolling",
        options=[
            discord.SelectOption(label="OC Slot 1", value="1"),
            discord.SelectOption(label="OC Slot 2", value="2"),
        ],
    )
    async def select_oc(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.oc_slot = select.values[0]
        await interaction.response.defer(ephemeral=True)
        self.stop()

class RerollConfirmView(discord.ui.View):
    def __init__(self, original_view: discord.ui.View):
        super().__init__(timeout=30)
        self.original_view = original_view

    @discord.ui.button(label="Yes, use Minor Trait", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ✅ No inventory blocking. Allow the extra roll and log for staff to deduct manually.
        new_roll = random.randint(1, 100)
        grimoire = determine_grimoire_type(new_roll)
        self.original_view.results.append({"roll": new_roll, "grimoire": grimoire, "is_reroll": True})

        embed = self.original_view.message.embeds[0]
        embed.add_field(
            name="Extra Roll (Minor Trait Used)",
            value=f"{grimoire['emoji']} **{grimoire['name']}** (Roll: {new_roll})",
            inline=True,
        )

        for child in self.children:
            child.disabled = True

        # Disable confirm view buttons
        await interaction.message.edit(view=self)
        # Update the original roll message embed
        await self.original_view.message.edit(embed=embed)

        # Audit log (Supabase audit_log table)
        try:
            cog = getattr(self.original_view, "cog", None)
            if cog and hasattr(cog, "log_audit"):
                await cog.log_audit(
                    user_id=interaction.user.id,
                    action_type="grimoire_extra_roll_minor_trait",
                    details={
                        "note": "Player used Minor Trait for extra roll. Deduct manually.",
                        "roll": new_roll,
                        "result_grimoire": grimoire["name"],
                        "oc_slot": getattr(self.original_view, "oc_slot", None),
                        "message_url": interaction.message.jump_url if interaction.message else None,
                    },
                    guild_id=interaction.guild.id if interaction.guild else None,
                    channel_id=interaction.channel.id if interaction.channel else None,
                )
        except Exception as e:
            print(f"[grimoire] audit log failed (extra roll): {e}")

        await interaction.response.send_message(
            "✅ Extra roll granted! **Reminder:** 1 Minor Trait should be deducted manually by staff.",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Cancelled extra roll.", ephemeral=True)

class GrimoireSelectButton(discord.ui.Button):
    def __init__(self, index: int, grimoire: Dict):
        super().__init__(
            label=f"Roll {index + 1}: {grimoire['name']}",
            style=discord.ButtonStyle.primary,
            emoji=grimoire["emoji"],
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: "GrimoireRollView" = self.view  # type: ignore
        selected = view.results[self.index]
        grimoire_type = selected["grimoire"]["name"]

        # ✅ Log claim into your actual grimoire_claims schema
        claim_id = await view.cog.log_grimoire_claim(
            discord_id=str(interaction.user.id),
            username=interaction.user.display_name,
            selected_grimoire=grimoire_type,
            rolled_options=view.rolled_options_blob,
            guild_id=interaction.guild.id if interaction.guild else None,
            channel_id=interaction.channel.id if interaction.channel else None,
            oc_slot=view.oc_slot,
        )

        # ✅ Staff+ Phase 2 hook:
        # Non-simple CLAIMED => 3-day OC sheet deadline + staff queue item
        try:
            if grimoire_type in NON_SIMPLE_GRIMOIRE_TYPES and claim_id:
                phase2 = interaction.client.get_cog("StaffPlusPhase2")
                if phase2 and hasattr(phase2, "hook_grimoire_claim_deadline"):
                    await phase2.hook_grimoire_claim_deadline(
                        interaction=interaction,
                        claim_id=str(claim_id),
                        grimoire_type=grimoire_type,
                        oc_slot=view.oc_slot,
                        due_days=OC_SHEET_DEADLINE_DAYS,
                    )
        except Exception as e:
            print(f"[grimoire] Staff+ hook failed: {type(e).__name__}: {e}")

        # ✅ Disable buttons after selection
        for child in view.children:
            child.disabled = True

        # ✅ Make the original roll message clearly show it was claimed
        try:
            original_embed = view.message.embeds[0] if getattr(view, "message", None) and view.message.embeds else None
            if original_embed:
                original_embed.title = "✅ Grimoire Claimed"
                original_embed.add_field(
                    name="Selected",
                    value=f"{selected['grimoire']['emoji']} **{grimoire_type}** (Roll: {selected['roll']})",
                    inline=False,
                )
                original_embed.set_footer(text=f"Claimed by {interaction.user.display_name} • OC Slot {view.oc_slot}")
                await view.message.edit(embed=original_embed, view=view)
            else:
                await interaction.message.edit(view=view)
        except Exception as e:
            print(f"[grimoire] Failed to update claim message: {e}")

        # ✅ Ping staff + send a staff-facing summary embed to the chosen channel
        try:
            staff_channel = interaction.client.get_channel(STAFF_ALERT_CHANNEL_ID)
            if staff_channel is None and interaction.guild:
                staff_channel = await interaction.guild.fetch_channel(STAFF_ALERT_CHANNEL_ID)

            if staff_channel:
                due_ts = int((_utcnow() + timedelta(days=OC_SHEET_DEADLINE_DAYS)).timestamp())
                due_str = f"<t:{due_ts}:R>" if grimoire_type in NON_SIMPLE_GRIMOIRE_TYPES else "N/A (Simple grimoire)"

                staff_embed = discord.Embed(
                    title="📚 Grimoire Claimed (Staff Alert)",
                    color=selected["grimoire"]["color"],
                )
                staff_embed.add_field(name="Player", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
                staff_embed.add_field(name="OC Slot", value=str(view.oc_slot), inline=True)
                staff_embed.add_field(name="Selected", value=f"{selected['grimoire']['emoji']} **{grimoire_type}** (Roll: {selected['roll']})", inline=True)
                staff_embed.add_field(name="All Rolls", value=view.rolled_options_blob, inline=False)
                staff_embed.add_field(name="OC Sheet Due", value=due_str, inline=False)
                staff_embed.add_field(name="Jump to Roll Message", value=view.message.jump_url if getattr(view, "message", None) else "N/A", inline=False)

                await staff_channel.send(content="@here", embed=staff_embed)
        except Exception as e:
            print(f"[grimoire] Staff alert send failed: {e}")

        # ✅ Acknowledge interaction first, then send confirmation via followup (more reliable)
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        # Confirmation embed (ephemeral)
        confirm = discord.Embed(
            title="Grimoire Claimed!",
            description=f"{selected['grimoire']['emoji']} **{grimoire_type}** (OC Slot {view.oc_slot})",
            color=selected["grimoire"]["color"],
        )

        if grimoire_type in NON_SIMPLE_GRIMOIRE_TYPES:
            confirm.add_field(
                name="OC Sheet Due",
                value=f"<t:{int((_utcnow() + timedelta(days=OC_SHEET_DEADLINE_DAYS)).timestamp())}:R>",
                inline=False,
            )
        else:
            confirm.add_field(name="OC Sheet Due", value="N/A (Simple grimoire)", inline=False)

        await interaction.followup.send(embed=confirm, ephemeral=True)

class RerollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Use Minor Trait for Extra Roll", style=discord.ButtonStyle.secondary, emoji="🎲")

    async def callback(self, interaction: discord.Interaction):
        view: "GrimoireRollView" = self.view  # type: ignore
        confirm_view = RerollConfirmView(view)
        embed = discord.Embed(
            title="⚠️ Confirm Extra Roll",
            description="This will require 1 Minor Trait to be deducted manually by staff.",
            color=0xf1c40f,
        )
        await interaction.response.send_message(embed=embed, view=confirm_view, ephemeral=True)

class GrimoireRollView(discord.ui.View):
    def __init__(self, cog, results: List[Dict], is_vip: bool, oc_slot: int, rolled_options_blob: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.results = results
        self.is_vip = is_vip
        self.oc_slot = oc_slot
        self.rolled_options_blob = rolled_options_blob

        for i, result in enumerate(results):
            self.add_item(GrimoireSelectButton(i, result["grimoire"]))

        # ✅ Only show the Minor Trait extra-roll button if NOT VIP and NOT Booster
        if not is_vip and not getattr(self, "is_booster", False):
            self.add_item(RerollButton())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass

# ── Cog ───────────────────────────────────────────────────────────────────────
class Grimoire(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.supabase = None
        try:
            self.supabase = get_supabase_client()
            print("✅ Supabase connection established (grimoire)")
        except Exception as e:
            print(f"❌ Supabase connection failed (grimoire): {e}")

    def _ensure_sb(self):
        if self.supabase is None:
            self.supabase = get_supabase_client()
        return self.supabase

    async def log_grimoire_claim(
        self,
        discord_id: str,
        username: str,
        selected_grimoire: str,
        rolled_options: str,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        oc_slot: int = 1,
    ) -> Optional[str]:
        """
        Inserts into your real schema:
        - discord_id (text) [required]
        - selected_grimoire (text)
        - rolled_options (text)
        - claim_date (timestamptz default now())
        - channel (text)
        - username (text)
        - oc_slot (int)
        """
        try:
            sb = self._ensure_sb()
        except Exception as e:
            print(f"[grimoire] Supabase not ready: {e}")
            return None

        data = {
            "discord_id": discord_id,
            "username": username,
            "oc_slot": oc_slot,
            "selected_grimoire": selected_grimoire,
            "rolled_options": rolled_options,
            "channel": str(channel_id) if channel_id else None,
            # claim_date is defaulted in DB, but we can also send it:
            "claim_date": _utcnow().isoformat(),
        }

        try:
            res = sb.table("grimoire_claims").insert(data).execute()
            if getattr(res, "data", None) and isinstance(res.data, list) and res.data:
                return res.data[0].get("id")
            return None
        except Exception as e:
            print(f"[grimoire] Insert exception: {e}")
            return None

    async def log_audit(
        self,
        user_id: int,
        action_type: str,
        details: Optional[dict] = None,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ):
        try:
            sb = self._ensure_sb()
        except Exception as e:
            print(f"[grimoire] Supabase not ready (audit): {e}")
            return

        data = {
            "user_id": str(user_id),
            "action_type": action_type,
            "details": details or {},
            "guild_id": str(guild_id) if guild_id else None,
            "channel_id": str(channel_id) if channel_id else None,
        }
        try:
            sb.table("audit_log").insert(data).execute()
        except Exception as e:
            print(f"[grimoire] Audit insert exception: {e}")

    @app_commands.command(
        name="roll-grimoire",
        description="Roll for a random grimoire (Supporters get +2 rolls, Boosters get +1 roll)",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def roll_grimoire(self, interaction: discord.Interaction):
        member_roles = {r.id for r in getattr(interaction.user, "roles", [])}

        is_vip = (SUPPORTER_ROLE_ID in member_roles) or (POWER_SUPPORTER_ROLE_ID in member_roles)
        is_booster = BOOSTER_ROLE_ID in member_roles

        # Base + bonuses
        base_rolls = 2
        vip_bonus = 2 if is_vip else 0
        booster_bonus = 1 if is_booster else 0
        total_rolls = base_rolls + vip_bonus + booster_bonus

        oc_view = OCSelectView()
        await interaction.response.send_message(
            "Which OC slot is rolling for a grimoire?",
            view=oc_view,
            ephemeral=True,
        )
        if await oc_view.wait():
            return await interaction.followup.send("Timed out waiting for OC selection.", ephemeral=True)

        oc_slot = int(oc_view.oc_slot)

        rolls = [random.randint(1, 100) for _ in range(total_rolls)]
        results = [{"roll": r, "grimoire": determine_grimoire_type(r)} for r in rolls]

        # store rolled options as a text blob for staff transparency
        rolled_blob = " | ".join([f"{determine_grimoire_type(r)['name']}({r})" for r in rolls])

        embed = discord.Embed(
            title=f"🎲 Grimoire Rolls (OC Slot {oc_slot})",
            color=0x2ecc71,
            description=f"{interaction.user.display_name}'s rolls:",
        )
        for i, result in enumerate(results):
            embed.add_field(
                name=f"Roll {i+1}",
                value=f"{result['grimoire']['emoji']} **{result['grimoire']['name']}** (Roll: {result['roll']})",
                inline=True,
            )

        bonuses = []
        if is_vip:
            bonuses.append("VIP +2")
        if is_booster:
            bonuses.append("Server Boost +1")

        if bonuses:
            embed.set_footer(text=f"Bonuses: {', '.join(bonuses)} • Total rolls: {total_rolls}")

        view = GrimoireRollView(self, results, is_vip, oc_slot, rolled_blob)
        # stash booster flag so the view can hide Minor Trait reroll for boosters too
        view.is_booster = is_booster  # type: ignore[attr-defined]

        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message

    @app_commands.command(
        name="my-grimoire",
        description="Check your currently claimed grimoire",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def my_grimoire(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            sb = self._ensure_sb()
        except Exception as e:
            print(f"[grimoire] Supabase not ready (my_grimoire): {e}")
            return await interaction.followup.send("Server configuration error. Please notify an admin.", ephemeral=True)

        try:
            result = (
                sb.table("grimoire_claims")
                .select("*")
                .eq("discord_id", str(interaction.user.id))
                .order("claim_date", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            print(f"[grimoire] Select exception (my_grimoire): {e}")
            return await interaction.followup.send("❌ Could not fetch your grimoire data.", ephemeral=True)

        if not getattr(result, "data", None):
            return await interaction.followup.send(
                "You don't have an active grimoire. Use `/roll-grimoire` to get one!",
                ephemeral=True,
            )

        g = result.data[0]
        claim_date = datetime.fromisoformat(g["claim_date"].replace("Z", "+00:00"))

        embed = discord.Embed(
            title=f"Your Claimed Grimoire: {g.get('selected_grimoire', 'Unknown')} (Slot {g.get('oc_slot', 1)})",
            color=0x3498db,
        )
        embed.add_field(name="Claimed", value=f"<t:{int(claim_date.timestamp())}:R>", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Grimoire(bot))
