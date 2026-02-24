# cogs/monthly_jobs.py
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG ----------------
GUILD_ID = int(os.getenv("GUILD_ID", "1374730886234374235"))
SKYFALL_GUILD = discord.Object(id=GUILD_ID)

APPROVED_ROLE_ID = int(os.getenv("APPROVED_ROLE_ID", "1374730886356144193"))

BOARD_CHANNEL_ID = int(os.getenv("JOBS_BOARD_CHANNEL_ID", "1382470739340300480"))
STAFF_REVIEW_CHANNEL_ID = int(os.getenv("JOBS_STAFF_REVIEW_CHANNEL_ID", "1374730887547191442"))

# If a job template doesn't specify rp_channel_id, this is used
DEFAULT_JOB_RP_CHANNEL_ID = int(os.getenv("DEFAULT_JOB_RP_CHANNEL_ID", "0"))  # set this

TIMEZONE = os.getenv("JOBS_TIMEZONE", "America/New_York")
POST_DAY = int(os.getenv("JOBS_POST_DAY", "1"))  # 1st of the month
POST_HOUR = int(os.getenv("JOBS_POST_HOUR", "12"))  # noon local
POST_MINUTE = int(os.getenv("JOBS_POST_MINUTE", "0"))

OPENINGS_RATIO = float(os.getenv("JOBS_OPENINGS_RATIO", "0.75"))
MIN_OPENINGS = int(os.getenv("JOBS_MIN_OPENINGS", "6"))
MAX_OPENINGS = int(os.getenv("JOBS_MAX_OPENINGS", "30"))

# Default: 1 job per player per month
ONE_JOB_PER_PLAYER = os.getenv("JOBS_ONE_PER_PLAYER", "true").lower().strip() == "true"

# No-show timeout (days) optional
NOSHOW_DAYS = int(os.getenv("JOBS_NOSHOW_DAYS", "0"))  # 0 disables

# ---------------- HELPERS ----------------
def month_key(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def parse_dice(expr: str) -> tuple[int, int]:
    # "1d20" => (1,20)
    expr = (expr or "").lower().strip()
    if "d" not in expr:
        raise ValueError("Invalid dice expression")
    a, b = expr.split("d", 1)
    return int(a), int(b)

def roll_dice(expr: str, rng: random.Random) -> tuple[int, str]:
    n, sides = parse_dice(expr)
    rolls = [rng.randint(1, sides) for _ in range(n)]
    total = sum(rolls)
    detail = f"{expr}=" + ("+".join(map(str, rolls)) if n > 1 else str(total))
    return total, detail

def season_tags_for_month(m: int) -> List[str]:
    tags = []
    if m in (12, 1, 2):
        tags.append("winter")
    if m in (3, 4, 5):
        tags.append("spring")
    if m in (6, 7, 8):
        tags.append("summer")
    if m in (9, 10, 11):
        tags.append("autumn")
    if m == 12:
        tags.append("holiday")
    if m == 10:
        tags.append("spooky")
    return tags

@dataclass
class CatalogJob:
    id: str
    title: str
    description: str
    max_workers: int
    pay_min: int
    pay_max: int
    bonus_rule: Optional[str]
    bonus_dice: Optional[str]
    season_tags: List[str]
    weight: int
    rp_channel_id: Optional[int]

# ---------------- UI VIEWS ----------------
class SignupView(discord.ui.View):
    def __init__(self, cog: "MonthlyJobsCog", month_key: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.month_key = month_key

    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.success, custom_id="jobs:signup")
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_signup(interaction, self.month_key)

class StaffReviewView(discord.ui.View):
    def __init__(self, cog: "MonthlyJobsCog", signup_id: str, staff_block_self_user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.signup_id = signup_id
        self.staff_block_self_user_id = staff_block_self_user_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="jobs:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_staff_approve(interaction, self.signup_id)

    @discord.ui.button(label="Edit Pay", style=discord.ButtonStyle.primary, custom_id="jobs:edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_staff_edit(interaction, self.signup_id)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="jobs:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_staff_deny(interaction, self.signup_id)

class EditPayModal(discord.ui.Modal, title="Edit Payout"):
    base_pay = discord.ui.TextInput(label="Base Pay", placeholder="e.g. 70", required=True)
    bonus_pay = discord.ui.TextInput(label="Bonus Pay", placeholder="e.g. 13", required=True)
    notes = discord.ui.TextInput(label="Notes (required for overrides)", style=discord.TextStyle.paragraph, required=True)

    def __init__(self, cog: "MonthlyJobsCog", signup_id: str):
        super().__init__()
        self.cog = cog
        self.signup_id = signup_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.apply_staff_override(
            interaction,
            self.signup_id,
            int(str(self.base_pay.value).strip()),
            int(str(self.bonus_pay.value).strip()),
            str(self.notes.value).strip(),
        )

class QualifiesBonusView(discord.ui.View):
    def __init__(self, cog: "MonthlyJobsCog", signup_id: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.signup_id = signup_id
        self.result: Optional[bool] = None

    @discord.ui.button(label="Qualifies", style=discord.ButtonStyle.success)
    async def qualifies(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.stop()
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="No Bonus", style=discord.ButtonStyle.secondary)
    async def no_bonus(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.stop()
        await interaction.response.defer(ephemeral=True)

# ---------------- COG ----------------
class MonthlyJobsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tz = ZoneInfo(TIMEZONE)
        self.monthly_post_loop.start()

    def cog_unload(self):
        self.monthly_post_loop.cancel()

    # ---- Scheduler: checks every 15 minutes and posts when needed ----
    @tasks.loop(minutes=15)
    async def monthly_post_loop(self):
        if not self.bot.is_ready():
            return
        now = datetime.now(self.tz)

        # Only attempt around the scheduled time window
        if now.day != POST_DAY:
            return
        if now.hour != POST_HOUR:
            return
        if not (POST_MINUTE <= now.minute <= POST_MINUTE + 14):
            return

        mk = month_key(now)
        sb = getattr(self.bot, "supabase", None)
        if sb is None:
            return

        # already posted?
        existing = sb.table("monthly_job_posts").select("id").eq("month_key", mk).execute()
        if existing.data:
            return

        # Generate + post
        guild = self.bot.get_guild(GUILD_ID)
        if guild is None:
            return
        await self.generate_and_post_board(guild, mk, now)

    @monthly_post_loop.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

    # ---- Generation ----
    async def generate_and_post_board(self, guild: discord.Guild, mk: str, now: datetime):
        sb = self.bot.supabase
        board_channel = guild.get_channel(BOARD_CHANNEL_ID)
        if board_channel is None:
            return

        # Count approved players
        role = guild.get_role(APPROVED_ROLE_ID)
        approved_members = [m for m in (role.members if role else []) if not m.bot]
        active_count = len(approved_members)

        openings = clamp(int(round(active_count * OPENINGS_RATIO)), MIN_OPENINGS, MAX_OPENINGS)

        # Pull catalog
        res = sb.table("job_catalog").select("*").eq("enabled", True).execute()
        catalog_rows = res.data or []

        if not catalog_rows:
            await board_channel.send("⚠️ No jobs in job_catalog yet.")
            return

        tags = season_tags_for_month(now.month)
        rng_seed = random.randint(1, 2_000_000_000)
        rng = random.Random(rng_seed)

        catalog: List[CatalogJob] = []
        for r in catalog_rows:
            catalog.append(
                CatalogJob(
                    id=str(r["id"]),
                    title=r["title"],
                    description=r["description"],
                    max_workers=int(r["max_workers"]),
                    pay_min=int(r["pay_min"]),
                    pay_max=int(r["pay_max"]),
                    bonus_rule=r.get("bonus_rule"),
                    bonus_dice=r.get("bonus_dice"),
                    season_tags=list(r.get("season_tags") or []),
                    weight=int(r.get("weight") or 0),
                    rp_channel_id=r.get("rp_channel_id"),
                )
            )

        # Weighted selection with season boost
        weighted: List[tuple[CatalogJob, int]] = []
        for j in catalog:
            w = max(0, j.weight)
            if w == 0:
                continue
            # season boost
            if any(t in (j.season_tags or []) for t in tags):
                w *= 3
            if ("holiday" in tags) and ("holiday" in (j.season_tags or [])):
                w *= 4
            if ("spooky" in tags) and ("spooky" in (j.season_tags or [])):
                w *= 4
            weighted.append((j, w))

        if not weighted:
            await board_channel.send("⚠️ All jobs have weight 0 or no enabled jobs.")
            return

        # Choose unique job types target
        unique_target = clamp(int(round(openings * 0.6)), 6, 10)
        unique_target = min(unique_target, len(weighted))

        chosen: List[CatalogJob] = []
        pool = weighted[:]
        for _ in range(unique_target):
            total_w = sum(w for _, w in pool)
            pick = rng.randint(1, total_w)
            running = 0
            chosen_idx = 0
            for idx, (job, w) in enumerate(pool):
                running += w
                if pick <= running:
                    chosen_idx = idx
                    chosen.append(job)
                    break
            pool.pop(chosen_idx)

        # Allocate openings (slots) across chosen jobs
        remaining = openings
        allocations: Dict[str, int] = {j.id: 0 for j in chosen}

        # First pass: give each job 1 slot
        for j in chosen:
            if remaining <= 0:
                break
            allocations[j.id] += 1
            remaining -= 1

        # Then distribute remaining with caps
        while remaining > 0:
            j = rng.choice(chosen)
            if allocations[j.id] < j.max_workers:
                allocations[j.id] += 1
                remaining -= 1
            else:
                # if all capped, break
                if all(allocations[x.id] >= x.max_workers for x in chosen):
                    break

        # Create monthly_jobs snapshots
        monthly_jobs_rows: List[Dict[str, Any]] = []
        for j in chosen:
            slots = allocations[j.id]
            # if the allocated slots are 0 somehow, skip
            if slots <= 0:
                continue
            pay_base = rng.randint(j.pay_min, j.pay_max)
            rp_chan = j.rp_channel_id or (DEFAULT_JOB_RP_CHANNEL_ID if DEFAULT_JOB_RP_CHANNEL_ID != 0 else None)
            monthly_jobs_rows.append(
                dict(
                    month_key=mk,
                    job_catalog_id=j.id,
                    title=j.title,
                    description=j.description,
                    max_workers=slots,  # snapshot slots for month
                    pay_base=pay_base,
                    bonus_rule=j.bonus_rule,
                    bonus_dice=j.bonus_dice,
                    rp_channel_id=rp_chan,
                    is_open=True,
                )
            )

        insert_jobs = sb.table("monthly_jobs").insert(monthly_jobs_rows).execute()
        created_jobs = insert_jobs.data or []

        # Build message embed
        month_name = now.strftime("%B").upper()
        embed = discord.Embed(
            title=f"HELP WANTED | {month_name}",
            description=(
                "Down the street from the Imperial Army’s headquarters and the residential squad towers stands a very popular tavern...\n\n"
                "**Eligibility:** Anyone (not a mission)\n"
                "**What Is This?** Side-gigs for extra thral once a month. Self-GM’d tasks.\n"
                "**How Does This Work?** Sign up below. Once accepted, open a thread in the RP channel indicated, post 4 paragraphs (100 words each), then use `/job complete` in your thread.\n\n"
                f"**Openings this month:** {openings}\n"
            ),
        )

        # Add job fields
        for j in created_jobs:
            title = f"🪶 {j['title']} | 0/{j['max_workers']} OPEN"
            bonus_line = ""
            if j.get("bonus_rule") and j.get("bonus_dice"):
                bonus_line = f"\n**Bonus Pay:** {j['bonus_rule']} ({j['bonus_dice']})"
            pay_line = f"**Pay:** {j['pay_base']} thral"
            embed.add_field(
                name=title,
                value=f"{j['description']}\n{pay_line}{bonus_line}",
                inline=False,
            )

        msg = await board_channel.send(embed=embed, view=SignupView(self, mk))

        # record monthly post
        sb.table("monthly_job_posts").insert(
            dict(
                month_key=mk,
                channel_id=BOARD_CHANNEL_ID,
                message_id=msg.id,
                openings=openings,
                seed=rng_seed,
                status="active",
            )
        ).execute()

    # ---- Signup handling ----
    async def handle_signup(self, interaction: discord.Interaction, mk: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return

        member: discord.Member = interaction.user  # type: ignore
        role = guild.get_role(APPROVED_ROLE_ID)
        if role is None or role not in member.roles:
            await interaction.followup.send("You are not eligible for these jobs (approved role required).", ephemeral=True)
            return

        sb = self.bot.supabase

        # 1 job per player per month
        if ONE_JOB_PER_PLAYER:
            existing = sb.table("job_signups").select("id,status").eq("month_key", mk).eq("user_id", member.id).execute()
            if existing.data:
                # allow if cancelled/denied only
                if any(r["status"] in ("signed_up", "completed", "paid") for r in existing.data):
                    await interaction.followup.send("You already have a job signup for this month.", ephemeral=True)
                    return

        # Get open jobs + current signup counts
        jobs = sb.table("monthly_jobs").select("id,title,max_workers,pay_base,bonus_rule,bonus_dice,is_open").eq("month_key", mk).eq("is_open", True).execute().data or []
        if not jobs:
            await interaction.followup.send("No jobs are available right now.", ephemeral=True)
            return

        # Build a select menu dynamically
        options = []
        for j in jobs:
            count = sb.table("job_signups").select("id").eq("monthly_job_id", j["id"]).in_("status", ["signed_up", "completed", "paid"]).execute()
            filled = len(count.data or [])
            if filled >= int(j["max_workers"]):
                continue
            label = j["title"][:100]
            desc = f"{filled}/{j['max_workers']} slots | Pay {j['pay_base']}"
            options.append(discord.SelectOption(label=label, description=desc[:100], value=str(j["id"])))

        if not options:
            await interaction.followup.send("All jobs are full right now.", ephemeral=True)
            return

        class JobSelect(discord.ui.Select):
            def __init__(self, cog: MonthlyJobsCog):
                super().__init__(placeholder="Pick a job…", options=options)
                self.cog = cog

            async def callback(self, i: discord.Interaction):
                await i.response.defer(ephemeral=True)
                job_id = self.values[0]

                # Re-check capacity
                job_row = sb.table("monthly_jobs").select("*").eq("id", job_id).single().execute().data
                if not job_row or not job_row.get("is_open", False):
                    await i.followup.send("That job is no longer available.", ephemeral=True)
                    return

                count = sb.table("job_signups").select("id").eq("monthly_job_id", job_id).in_("status", ["signed_up", "completed", "paid"]).execute()
                filled = len(count.data or [])
                if filled >= int(job_row["max_workers"]):
                    await i.followup.send("That job just filled up.", ephemeral=True)
                    return

                # Insert signup
                sb.table("job_signups").insert(
                    dict(month_key=mk, monthly_job_id=job_id, user_id=i.user.id, status="signed_up")
                ).execute()

                rp_hint = "Check the job post for instructions."
                await i.followup.send(f"✅ Signed up for **{job_row['title']}**.\nWhen finished, use `/job complete` in your job thread.\n{rp_hint}", ephemeral=True)

        class JobPickView(discord.ui.View):
            def __init__(self, cog: MonthlyJobsCog):
                super().__init__(timeout=60)
                self.add_item(JobSelect(cog))

        await interaction.followup.send("Choose a job:", view=JobPickView(self), ephemeral=True)

    # ---- /job complete ----
    @app_commands.command(name="job_complete", description="Mark your monthly job as complete (run this inside your job thread).")
    @app_commands.guilds(SKYFALL_GUILD)
    async def job_complete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return
        channel = interaction.channel
        if channel is None:
            return

        # must be thread or text channel; store thread id if thread
        thread_id = channel.id

        mk = month_key(datetime.now(self.tz))
        sb = self.bot.supabase

        # Find active signup
        signups = sb.table("job_signups").select("*").eq("month_key", mk).eq("user_id", interaction.user.id).execute().data or []
        if not signups:
            await interaction.followup.send("You don’t have a signup for this month.", ephemeral=True)
            return

        signup = None
        for s in signups:
            if s["status"] in ("signed_up",):
                signup = s
                break
        if signup is None:
            await interaction.followup.send("You don’t have an active signup to complete.", ephemeral=True)
            return

        # mark completed
        sb.table("job_signups").update(dict(status="completed", thread_id=thread_id, completed_at="now()")).eq("id", signup["id"]).execute()

        # pull job details for staff
        job = sb.table("monthly_jobs").select("*").eq("id", signup["monthly_job_id"]).single().execute().data
        if not job:
            await interaction.followup.send("Completed, but I couldn't load the job details. Staff has been notified.", ephemeral=True)
            job_title = "Unknown Job"
        else:
            job_title = job["title"]

        staff_channel = guild.get_channel(STAFF_REVIEW_CHANNEL_ID)
        if staff_channel:
            embed = discord.Embed(title="Job Completion Submitted", description=f"**Player:** <@{interaction.user.id}>\n**Job:** {job_title}")
            embed.add_field(name="Base Pay", value=str(job.get("pay_base", "0")), inline=True)
            if job.get("bonus_rule") and job.get("bonus_dice"):
                embed.add_field(name="Bonus Rule", value=f"{job['bonus_rule']} ({job['bonus_dice']})", inline=False)
            embed.add_field(name="Thread", value=f"<#{thread_id}>", inline=False)

            view = StaffReviewView(self, signup_id=str(signup["id"]), staff_block_self_user_id=interaction.user.id)
            await staff_channel.send(embed=embed, view=view)

        await interaction.followup.send("✅ Marked complete! Staff has been notified for payout review.", ephemeral=True)

    # ---- Staff actions ----
    def _is_staff(self, member: discord.Member) -> bool:
        # You can tighten this later: check for specific staff roles.
        # For now: Manage Guild or Administrator.
        perms = member.guild_permissions
        return perms.manage_guild or perms.administrator

    async def handle_staff_approve(self, interaction: discord.Interaction, signup_id: str):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        staff: discord.Member = interaction.user

        if not self._is_staff(staff):
            await interaction.followup.send("You do not have permission to approve payouts.", ephemeral=True)
            return

        sb = self.bot.supabase
        signup = sb.table("job_signups").select("*").eq("id", signup_id).single().execute().data
        if not signup:
            await interaction.followup.send("Signup not found.", ephemeral=True)
            return

        # Prevent self-approval
        if int(signup["user_id"]) == staff.id:
            await interaction.followup.send("You cannot approve your own payout.", ephemeral=True)
            return

        if signup["status"] == "paid":
            await interaction.followup.send("This signup is already paid.", ephemeral=True)
            return

        job = sb.table("monthly_jobs").select("*").eq("id", signup["monthly_job_id"]).single().execute().data
        if not job:
            await interaction.followup.send("Job record missing.", ephemeral=True)
            return

        base_pay = int(job.get("pay_base", 0))
        bonus_pay = 0
        qualifies = False
        bonus_roll_detail = None

        # Ask qualifies?
        if job.get("bonus_dice"):
            qv = QualifiesBonusView(self, signup_id)
            await interaction.followup.send("Does the player qualify for the bonus?", view=qv, ephemeral=True)
            await qv.wait()
            qualifies = bool(qv.result)

            if qualifies:
                rng = random.Random()  # approval-time roll
                bonus_pay, bonus_roll_detail = roll_dice(str(job["bonus_dice"]), rng)

        total = base_pay + bonus_pay

        # Log payout
        sb.table("job_payouts").insert(
            dict(
                signup_id=signup_id,
                staff_id=staff.id,
                base_pay=base_pay,
                bonus_pay=bonus_pay,
                total_pay=total,
                qualifies_bonus=qualifies,
                bonus_roll=bonus_roll_detail,
                notes=None,
            )
        ).execute()

        # Mark signup paid
        sb.table("job_signups").update(dict(status="paid")).eq("id", signup_id).execute()

        await interaction.followup.send(f"✅ Approved. Paid **{total} thral** (Base {base_pay} + Bonus {bonus_pay}).", ephemeral=True)

    async def handle_staff_edit(self, interaction: discord.Interaction, signup_id: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        staff: discord.Member = interaction.user
        if not self._is_staff(staff):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.send_modal(EditPayModal(self, signup_id))

    async def apply_staff_override(self, interaction: discord.Interaction, signup_id: str, base_pay: int, bonus_pay: int, notes: str):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        staff: discord.Member = interaction.user
        if not self._is_staff(staff):
            await interaction.followup.send("No permission.", ephemeral=True)
            return
        if not notes:
            await interaction.followup.send("Notes are required for overrides.", ephemeral=True)
            return

        sb = self.bot.supabase
        signup = sb.table("job_signups").select("*").eq("id", signup_id).single().execute().data
        if not signup:
            await interaction.followup.send("Signup not found.", ephemeral=True)
            return

        if int(signup["user_id"]) == staff.id:
            await interaction.followup.send("You cannot approve your own payout.", ephemeral=True)
            return

        total = base_pay + bonus_pay

        sb.table("job_payouts").insert(
            dict(
                signup_id=signup_id,
                staff_id=staff.id,
                base_pay=base_pay,
                bonus_pay=bonus_pay,
                total_pay=total,
                qualifies_bonus=(bonus_pay > 0),
                bonus_roll=None,
                notes=notes,
            )
        ).execute()

        sb.table("job_signups").update(dict(status="paid")).eq("id", signup_id).execute()

        await interaction.followup.send(f"✅ Override payout approved: **{total} thral**. Logged with notes.", ephemeral=True)

    async def handle_staff_deny(self, interaction: discord.Interaction, signup_id: str):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return
        staff: discord.Member = interaction.user
        if not self._is_staff(staff):
            await interaction.followup.send("No permission.", ephemeral=True)
            return

        sb = self.bot.supabase
        signup = sb.table("job_signups").select("*").eq("id", signup_id).single().execute().data
        if not signup:
            await interaction.followup.send("Signup not found.", ephemeral=True)
            return

        if int(signup["user_id"]) == staff.id:
            await interaction.followup.send("You cannot deny your own payout.", ephemeral=True)
            return

        sb.table("job_signups").update(dict(status="denied")).eq("id", signup_id).execute()
        await interaction.followup.send("❌ Denied and logged.", ephemeral=True)

# ---------------- COG DISABLED ----------------
# This cog is intentionally disabled for the first implementation.
# The full logic is preserved for later activation.

async def setup(bot: commands.Bot):
    return
