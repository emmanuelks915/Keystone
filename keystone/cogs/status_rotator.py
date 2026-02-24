# cogs/status_rotator.py
import os
import json
import random
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

STATUS_JSON_FILENAME = "status_lines.json"

# ---------- Guild lock ----------
SKYFALL_GUILD_ID = 1374730886234374235
SKYFALL_GUILD = discord.Object(id=SKYFALL_GUILD_ID)


class StatusRotator(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Track last VIP index so we don't repeat the same VIP twice in a row
        self.last_vip_index: Optional[int] = None

        # Current "event mode" status (set by staff command)
        self.current_event_status: Optional[str] = None

        # ------------ Built-in hardcoded lines ------------

        # Tangerine Facts (49)
        self.tangerine_facts = [
            "🍊 Did you know? Tangerines are named after Tangier, Morocco.",
            "🍊 Tangerines are a type of mandarin orange, but usually sweeter.",
            "🍊 Tangerines have a looser peel than oranges, which makes them easier to peel.",
            "🍊 Tangerines are naturally rich in vitamin C.",
            "🍊 Tangerines also contain vitamin A, which supports eye health.",
            "🍊 China is the world’s largest producer of tangerines.",
            "🍊 Tangerines are technically a kind of berry called a hesperidium.",
            "🍊 Tangerine peel oil is used in perfumes and aromatherapy.",
            "🍊 Tangerine zest can be used to flavor desserts and drinks.",
            "🍊 Tangerines often have more seeds than clementines.",
            "🍊 In many cultures, tangerines symbolize luck and prosperity.",
            "🍊 Tangerines are popular gifts during Lunar New Year celebrations.",
            "🍊 Some tangerine varieties are seedless thanks to selective breeding.",
            "🍊 Tangerines are usually in season during winter months.",
            "🍊 A medium tangerine typically has around 50 calories.",
            "🍊 Tangerines are naturally fat-free and cholesterol-free.",
            "🍊 Tangerine segments are held together by thin membranes called carpels.",
            "🍊 The bright orange color of tangerines comes from carotenoids.",
            "🍊 Tangerines can be used in both sweet and savory dishes.",
            "🍊 Fresh tangerine juice can be used as a base for marinades.",
            "🍊 Dried tangerine peel is used in some traditional medicines.",
            "🍊 Tangerines were once considered a luxury fruit in Europe.",
            "🍊 Tangerine trees can live for decades with proper care.",
            "🍊 Bees love tangerine blossoms, which help with pollination.",
            "🍊 Tangerine trees prefer warm, subtropical climates.",
            "🍊 Some tangerines are naturally more reddish in color than others.",
            "🍊 Tangerines are related to grapefruits, lemons, and limes.",
            "🍊 Tangerine segments are perfect for bento boxes and lunchboxes.",
            "🍊 Tangerines can be stored at room temperature for several days.",
            "🍊 Refrigerating tangerines helps them stay fresh longer.",
            "🍊 Tangerines can be candied for a sweet citrus treat.",
            "🍊 Tangerine peel can be steeped to make citrus tea.",
            "🍊 Some people call tangerines “Christmas oranges” because of their season.",
            "🍊 Tangerine essential oil is sometimes used to create a calming scent.",
            "🍊 The scientific name for mandarin-type citrus is Citrus reticulata.",
            "🍊 Tangerines are often used to add a pop of color to salads.",
            "🍊 Tangerines can be crossbred with other citrus to create new varieties.",
            "🍊 Many sports drinks try to mimic natural tangerine flavor.",
            "🍊 Tangerine segments separate more easily than those of many oranges.",
            "🍊 Tangerines were introduced to the United States in the 19th century.",
            "🍊 Some tangerines have a bumpy, textured peel; others are very smooth.",
            "🍊 Tangerine trees can be grown in large pots indoors in sunny spots.",
            "🍊 Tangerines are sometimes used as natural table decorations.",
            "🍊 Tangerine-colored flowers and fabrics are associated with warmth and joy.",
            "🍊 The word “tangerine” first appeared in English in the 1800s.",
            "🍊 Tangerines pair well with chocolate in desserts.",
            "🍊 A bowl of tangerines on the table is basically a built-in snack bar.",
            "🍊 Tangerines can be dehydrated into chewy, tart slices.",
            "🍊 One tangerine can provide a big chunk of your daily vitamin C needs.",
        ]

        # Fun / Silly / Personality Lines (40)
        self.fun_silly = [
            "🍊 Peeling away your problems since 2024.",
            "🍊 Providing 100% of your daily RP vitamin C.",
            "🍊 Warning: do not squeeze the bot.",
            "🍊 Crunching numbers, not peels.",
            "🍊 Sweeter than your favorite OC.",
            "🍊 Low acidity, high productivity.",
            "🍊 Officially not compatible with scurvy.",
            "🍊 Peeling back server drama one layer at a time.",
            "🍊 Now with 200% more zest.",
            "🍊 May cause sudden urges to visit the shop channel.",
            "🍊 Press here to extract fresh Skyfall juice.",
            "🍊 Quietly judging outdated bots.",
            "🍊 Freshly deployed from Railway at 3 AM.",
            "🍊 Contains pulp and opinions.",
            "🍊 Powered by sunlight and Supabase queries.",
            "🍊 A balanced server includes one Tangerine Bot daily.",
            "🍊 Do not compare me to an orange. I’m superior.",
            "🍊 Emotionally supported by Glass and caffeine.",
            "🍊 Auto-sorting your chaos into neat citrus slices.",
            "🍊 If this status is showing, the bot is probably behaving. Probably.",
            "🍊 Currently pretending to be a serious infrastructure service.",
            "🍊 Squeezing bugs into juice.",
            "🍊 Logging your shenanigans in HD.",
            "🍊 Making spreadsheets jealous since launch day.",
            "🍊 Secretly knows who hasn’t used /oc_register yet.",
            "🍊 Watching missions like a citrus-flavored CCTV.",
            "🍊 Token transactions? I prefer to call it pulp management.",
            "🍊 If it breaks, blame latency, not the fruit.",
            "🍊 Half bot, half citrus, all business.",
            "🍊 Sweet on the outside, strict about logging on the inside.",
            "🍊 Please stand by while I peel another feature.",
            "🍊 Currently arguing with the RNG gods.",
            "🍊 Seen more economy resets than most nobles.",
            "🍊 Living, laughing, logging.",
            "🍊 This presence message counts as enrichment for the bot.",
            "🍊 Rip to Currencies Bot, but I’m different.",
            "🍊 Calibrating juice levels…",
            "🍊 Storing your data in a climate-controlled citrus grove.",
            "🍊 Hi dad. – love, Tangerine Bot 💾🍊",
            "🍊 Strong opinions on proper inventory management.",
        ]

        # VIP Shoutouts (12)
        self.vip_lines = [
            "🍊 VIP Spotlight: Sarah — keeping things softer than citrus pith.",
            "🍊 VIP Spotlight: Phoenix — hotter than a forge, sweeter than a tangerine.",
            "🍊 VIP Spotlight: Hamster_Rey — rolling faster than my dice cog.",
            "🍊 VIP Spotlight: Limelite — proof that lime and tangerine can coexist.",
            "🍊 VIP Spotlight: Autumn — cozy vibes and falling citrus leaves.",
            "🍊 VIP Spotlight: Eph — quietly holding the server together like peel to pulp.",
            "🍊 VIP Spotlight: Nadim — theorycrafting harder than my CPU.",
            "🍊 VIP Spotlight: Pearl — shinier than any legendary drop.",
            "🍊 VIP Spotlight: Ramón — certified chaos with a citrus aftertaste.",
            "🍊 VIP Spotlight: Serena — smoother than fresh-squeezed juice.",
            "🍊 VIP Spotlight: SVX — driving the hype like a turbocharged tangerine.",
            "🍊 VIP Spotlight: Kristian — sweeter than citrus, deadlier than RNG.",
        ]

        # Seasonal / Holiday Status Lines
        self.seasonal_status = {
            "winter": [
                "🍊 Winter mode: serving cozy citrus and cold economy data.",
                "🍊 Keep warm, drink tea, add tangerine.",
                "🍊 Snow outside, citrus inside.",
            ],
            "spring": [
                "🍊 Spring cleaning your logs and ledgers.",
                "🍊 New blooms, new builds, same citrus.",
                "🍊 Spring showers, tangerine flowers.",
            ],
            "summer": [
                "🍊 Summer mode: chilled juice, hot missions.",
                "🍊 Too warm for drama, perfect for citrus.",
                "🍊 Sunshine, sand, and stats.",
            ],
            "autumn": [
                "🍊 Autumn leaves, tangerine sleeves.",
                "🍊 Fall cozy check: blanket, tea, bot online.",
                "🍊 Harvesting data like it’s grain.",
            ],
            "halloween": [
                "🎃🍊 Spooky citrus: now with extra pulp.",
                "🎃🍊 Trick-or-treat: I only serve treats and logs.",
                "🎃🍊 Ghost-checked your transactions twice.",
            ],
            "holiday": [
                "🎄🍊 Happy holidays from your favorite citrus bot.",
                "🎄🍊 May your rolls be kind and your citrus sweet.",
                "🎄🍊 Logging cheer across NA and EU.",
            ],
            "boxing_day": [
                "🎁🍊 Happy Boxing Day to our EU/UK crew.",
                "🎁🍊 Post-holiday chill with extra citrus.",
            ],
            "new_year": [
                "🎆🍊 Happy New Year — fresh logs, fresh juice.",
                "🎆🍊 New year, same citrus, upgraded features.",
            ],
            "valentines": [
                "💘🍊 Happy Valentine’s — I ship you with good rolls.",
                "💘🍊 Roses are red, citrus is bright, may your missions go well tonight.",
            ],
            # Simple generic Easter window (date moves yearly, so we just cover a spring window)
            "easter": [
                "🐣🍊 Happy Easter / spring weekend to all timezones.",
                "🐣🍊 Hiding eggs, logging everything.",
            ],
        }

        # ------------ Custom, staff-editable lines ------------
        # Loaded from JSON so staff can add/remove via slash commands.
        self.custom_lines = {
            "fact": [],
            "fun": [],
            "vip": [],
            "seasonal": [],
        }
        self._load_custom_lines()

        # Start background loops
        self.change_status.start()
        self.daily_vip_shoutout.start()

    # =========================
    #  JSON persistence helpers
    # =========================

    @property
    def _status_json_path(self) -> str:
        # Store JSON alongside this cog file
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, STATUS_JSON_FILENAME)

    def _load_custom_lines(self) -> None:
        try:
            with open(self._status_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Guard against missing keys
            for key in self.custom_lines.keys():
                if key in data and isinstance(data[key], list):
                    self.custom_lines[key] = data[key]
        except FileNotFoundError:
            # First run, no file yet: ignore
            pass
        except Exception as e:
            print(f"[StatusRotator] Failed to load custom status lines: {e}")

    def _save_custom_lines(self) -> None:
        try:
            with open(self._status_json_path, "w", encoding="utf-8") as f:
                json.dump(self.custom_lines, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[StatusRotator] Failed to save custom status lines: {e}")

    # =========================
    #  Seasonal / date helpers
    # =========================

    def get_seasonal_pool(self, now: datetime) -> list[str]:
        month = now.month
        day = now.day
        pool: list[str] = []

        # Seasons (meteorological)
        if month in (12, 1, 2):
            pool += self.seasonal_status["winter"]
        elif month in (3, 4, 5):
            pool += self.seasonal_status["spring"]
        elif month in (6, 7, 8):
            pool += self.seasonal_status["summer"]
        elif month in (9, 10, 11):
            pool += self.seasonal_status["autumn"]

        # Halloween: Oct 25–31
        if month == 10 and 25 <= day <= 31:
            pool += self.seasonal_status["halloween"]

        # Holiday season: Dec 20 – Jan 6
        if (month == 12 and day >= 20) or (month == 1 and day <= 6):
            pool += self.seasonal_status["holiday"]

        # Boxing Day: Dec 26
        if month == 12 and day == 26:
            pool += self.seasonal_status["boxing_day"]

        # New Year: Dec 31 – Jan 2
        if (month == 12 and day == 31) or (month == 1 and day <= 2):
            pool += self.seasonal_status["new_year"]

        # Valentines: Feb 10–15
        if month == 2 and 10 <= day <= 15:
            pool += self.seasonal_status["valentines"]

        # Easter-ish: Apr 1–15
        if month == 4 and 1 <= day <= 15:
            pool += self.seasonal_status["easter"]

        # Include any *custom* seasonal lines staff added
        pool += self.custom_lines.get("seasonal", [])

        return pool

    # =========================
    #  Dynamic stats helpers
    # =========================

    async def fetch_oc_and_mission_counts(self) -> tuple[Optional[int], Optional[int]]:
        """
        Placeholder for Supabase-backed counts.

        TODO: Replace with your real Supabase queries.
        For now this safely returns (None, None) so it won't break anything.
        """
        return None, None

    async def build_dynamic_statuses(self) -> list[str]:
        """
        Build dynamic lines like member count, OC count, active missions.
        If a value can't be fetched, that line is simply skipped.
        """
        lines: list[str] = []

        # Use the "main" guild (first one) as reference
        guild: Optional[discord.Guild] = self.bot.guilds[0] if self.bot.guilds else None

        if guild:
            lines.append(f"🍊 Watching over {guild.member_count} members.")
            # Simple milestone-flavored line
            if guild.member_count >= 500:
                lines.append(f"🍊 Celebrating {guild.member_count}+ members!")

        # Supabase-backed counts (if implemented)
        oc_count, mission_count = await self.fetch_oc_and_mission_counts()
        if oc_count is not None:
            lines.append(f"🍊 Tracking {oc_count} registered OCs.")
        if mission_count is not None:
            lines.append(f"🍊 Watching {mission_count} active missions.")

        return lines

    # =========================
    #  Background loops
    # =========================

    def cog_unload(self):
        self.change_status.cancel()
        self.daily_vip_shoutout.cancel()

    # Main rotation: every 3 hours
    @tasks.loop(hours=3)
    async def change_status(self):
        await self.bot.wait_until_ready()

        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz=tz)

        seasonal_pool = self.get_seasonal_pool(now)
        dynamic_pool = await self.build_dynamic_statuses()

        # Base pool: built-in lines + custom lines + dynamic lines
        pool: list[str] = []
        pool += self.tangerine_facts
        pool += self.fun_silly
        pool += self.vip_lines
        pool += self.custom_lines.get("fact", [])
        pool += self.custom_lines.get("fun", [])
        pool += self.custom_lines.get("vip", [])
        pool += dynamic_pool

        # Seasonal lines get extra weight during their window
        if seasonal_pool:
            pool += seasonal_pool * 2  # appears more often

        # If there is an active event status, give it a strong chance to show
        if self.current_event_status and random.random() < 0.5:
            line = self.current_event_status
        else:
            line = random.choice(pool)

        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=line,
        )
        await self.bot.change_presence(activity=activity)

    @change_status.before_loop
    async def before_change_status(self):
        await self.bot.wait_until_ready()

    # Daily VIP shoutout at 2PM EST (overrides whatever was there)
    @tasks.loop(hours=24)
    async def daily_vip_shoutout(self):
        await self.bot.wait_until_ready()

        indices = list(range(len(self.vip_lines)))
        if self.last_vip_index is not None and len(indices) > 1:
            # Avoid same VIP two days in a row
            indices.remove(self.last_vip_index)

        vip_index = random.choice(indices)
        self.last_vip_index = vip_index

        line = self.vip_lines[vip_index]

        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=line,
        )
        await self.bot.change_presence(activity=activity)

    @daily_vip_shoutout.before_loop
    async def before_daily_vip_shoutout(self):
        """Wait until the next 2:00 PM America/New_York, then start loop."""
        await self.bot.wait_until_ready()

        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz=tz)

        target = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

    # =========================
    #  Slash commands (staff tools)
    # =========================

    def _require_staff(self, interaction: discord.Interaction) -> bool:
        # Simple check: manage_guild perms. Adjust if you use specific roles.
        return interaction.user.guild_permissions.manage_guild

    @app_commands.command(
        name="status_add", description="Add a custom Tangerine status line."
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def status_add(
        self,
        interaction: discord.Interaction,
        category: Literal["fact", "fun", "vip", "seasonal"],
        text: str,
    ):
        if not self._require_staff(interaction):
            return await interaction.response.send_message(
                "You don’t have permission to edit status lines.",
                ephemeral=True,
            )

        if len(text) > 128:
            return await interaction.response.send_message(
                "That line is too long for Discord status (max 128 characters).",
                ephemeral=True,
            )

        self.custom_lines[category].append(text)
        self._save_custom_lines()

        await interaction.response.send_message(
            f"Added new `{category}` status line:\n> {text}",
            ephemeral=True,
        )

    @app_commands.command(
        name="status_remove",
        description="Remove a custom Tangerine status line (exact text match).",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def status_remove(
        self,
        interaction: discord.Interaction,
        category: Literal["fact", "fun", "vip", "seasonal"],
        text: str,
    ):
        if not self._require_staff(interaction):
            return await interaction.response.send_message(
                "You don’t have permission to edit status lines.",
                ephemeral=True,
            )

        lines = self.custom_lines.get(category, [])
        try:
            lines.remove(text)
        except ValueError:
            return await interaction.response.send_message(
                "That line wasn’t found in the custom list for that category.",
                ephemeral=True,
            )

        self.custom_lines[category] = lines
        self._save_custom_lines()

        await interaction.response.send_message(
            f"Removed `{category}` status line:\n> {text}",
            ephemeral=True,
        )

    @app_commands.command(
        name="status_list", description="List custom Tangerine status lines."
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def status_list(
        self,
        interaction: discord.Interaction,
        category: Literal["fact", "fun", "vip", "seasonal"],
    ):
        if not self._require_staff(interaction):
            return await interaction.response.send_message(
                "You don’t have permission to view/manage custom status lines.",
                ephemeral=True,
            )

        lines = self.custom_lines.get(category, [])
        if not lines:
            return await interaction.response.send_message(
                f"No custom lines found for `{category}`.",
                ephemeral=True,
            )

        description = "\n".join(f"{i+1}. {line}" for i, line in enumerate(lines))

        await interaction.response.send_message(
            f"Custom `{category}` status lines:\n{description}",
            ephemeral=True,
        )

    @app_commands.command(
        name="status_event_set", description="Set Tangerine’s current event status."
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def status_event_set(
        self,
        interaction: discord.Interaction,
        event_name: str,
    ):
        if not self._require_staff(interaction):
            return await interaction.response.send_message(
                "You don’t have permission to set event status.",
                ephemeral=True,
            )

        line = f"🍊 Event: {event_name}"
        if len(line) > 128:
            return await interaction.response.send_message(
                "Event name is too long once formatted into a status.",
                ephemeral=True,
            )

        self.current_event_status = line

        # Immediately apply it so staff can see it right away
        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=line,
        )
        await self.bot.change_presence(activity=activity)

        await interaction.response.send_message(
            f"Set event status to:\n> {line}",
            ephemeral=True,
        )

    @app_commands.command(
        name="status_event_clear",
        description="Clear Tangerine’s current event status.",
    )
    @app_commands.guilds(SKYFALL_GUILD)
    async def status_event_clear(self, interaction: discord.Interaction):
        if not self._require_staff(interaction):
            return await interaction.response.send_message(
                "You don’t have permission to clear event status.",
                ephemeral=True,
            )

        self.current_event_status = None
        await interaction.response.send_message(
            "Cleared current event status. Rotation will continue normally.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusRotator(bot))
