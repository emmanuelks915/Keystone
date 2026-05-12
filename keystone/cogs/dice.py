# cogs/dice.py
from __future__ import annotations

import inspect
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.stat_calculator import calculate_derived_stats
from services.stats_service import StatsService
from services.traits_service import TraitsService
from services.trait_modifier_service import apply_trait_modifiers


"""
Keystone Universal Dice Roller
------------------------------
Commands:
    /dice roll
    /dice contest
    /dice table
    /dice stat

Supports:
    1d20
    d100
    4d6+2
    4d6kh3
    4d6kl3
    4d6dh1
    4d6dl1
    2d20adv+5
    2d20dis+5
    10d6>=5
    1d10!+1d6r1-2

Notes:
    - Public by default.
    - hidden:true is staff/GM only.
    - Hidden uses Discord ephemeral messages.
    - /dice stat pulls character stats using the same StatsService flow as cogs/stats.py.
"""


# ---------------------------
# CONFIG
# ---------------------------

KEYSTONE_GUILD_ID = 1374730886234374235

MAX_TERMS = 50
MAX_DICE_PER_TERM = 100
MAX_TOTAL_DICE = 200
MAX_SIDES = 1000
MAX_EXPLOSIONS_PER_DIE = 25


CORE_STAT_ORDER = [
    "strength",
    "dexterity",
    "stamina",
    "magic_affinity",
    "mana",
]

CORE_STAT_LABELS = {
    "strength": "Strength",
    "dexterity": "Dexterity",
    "stamina": "Stamina",
    "magic_affinity": "Magic Affinity",
    "mana": "Mana",
}

DERIVED_STAT_LABELS = {
    "reaction_score": "Reaction Score",
    "fortitude": "Fortitude",
    "safe_output": "Safe Output",
    "magic_safe_output": "Magic Safe Output",
    "ap": "Action Points",
    "carry_capacity": "Carry Capacity",
}

STAT_ALIASES = {
    "str": "strength",
    "strength": "strength",
    "dex": "dexterity",
    "dexterity": "dexterity",
    "sta": "stamina",
    "stamina": "stamina",
    "mag": "magic_affinity",
    "affinity": "magic_affinity",
    "magic": "magic_affinity",
    "magic_affinity": "magic_affinity",
    "mana": "mana",
    "reaction": "reaction_score",
    "reaction_score": "reaction_score",
    "fortitude": "fortitude",
    "safe_output": "safe_output",
    "physical_output": "safe_output",
    "magic_safe_output": "magic_safe_output",
    "magic_output": "magic_safe_output",
    "ap": "ap",
    "action_points": "ap",
    "carry": "carry_capacity",
    "carry_capacity": "carry_capacity",
}


# ---------------------------
# Built-in roll tables
# ---------------------------

ROLL_TABLES: dict[str, list[str]] = {
    "travel": [
        "Smooth travel. No delay.",
        "Minor rail delay. Add 1 hour.",
        "Mechanical inspection. Add 2 hours.",
        "Heavy foot traffic at the station. Add 30 minutes.",
        "A strange passenger asks too many questions.",
        "Cargo issue. Add 1 hour unless handled by staff/RP.",
        "Bad weather slows the route. Add 2 hours.",
        "Rare quiet ride. Characters may recover or RP freely.",
    ],
    "weather": [
        "Clear skies.",
        "Light rain.",
        "Heavy rain.",
        "Thick fog.",
        "Strong winds.",
        "Cold snap.",
        "Unseasonably warm.",
        "Storm rolling in.",
    ],
    "encounter": [
        "Peaceful travelers.",
        "Suspicious merchant.",
        "Pickpocket attempt.",
        "Broken rail signal.",
        "Hostile wildlife nearby.",
        "Local patrol asks questions.",
        "Lost courier needs help.",
        "Something watches from a distance.",
    ],
    "loot": [
        "Nothing useful.",
        "Small coins or basic supplies.",
        "Common crafting material.",
        "Useful tool or trinket.",
        "Uncommon resource.",
        "Damaged but valuable item.",
        "Rare clue or document.",
        "GM special reward.",
    ],
    "injury": [
        "No lasting injury.",
        "Minor bruise or scrape.",
        "Sprain or strain.",
        "Deep cut.",
        "Concussion risk.",
        "Temporary limp or weakness.",
        "Damaged gear or armor.",
        "GM chooses a serious complication.",
    ],
    "shop_event": [
        "Normal business day.",
        "A customer asks for a discount.",
        "A supplier is late.",
        "A rare customer arrives.",
        "Minor theft attempt.",
        "High demand. Sales are up today.",
        "Inspection or paperwork issue.",
        "Special commission opportunity.",
    ],
}


# ---------------------------
# Data structures
# ---------------------------

@dataclass
class DiceTermResult:
    term: str
    rolls: list[int] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)
    exploded: list[list[int]] = field(default_factory=list)
    rerolled_from: list[tuple[int, int]] = field(default_factory=list)
    successes: Optional[int] = None
    subtotal: int = 0


@dataclass
class RollResult:
    expression: str
    total: int
    terms: list[DiceTermResult]
    modifier_total: int
    detail: str


# ---------------------------
# Regex
# ---------------------------

DICE_RE = re.compile(
    r"\s*"
    r"(?P<sign>[+-])?"
    r"(?:(?P<count>\d*)d(?P<sides>\d+)(?P<body>[^+\-]*)|(?P<const>\d+))",
    re.IGNORECASE,
)

COMP_RE = re.compile(r"(?P<op>>=|<=|=|>|<)\s*(?P<thresh>\d+)")
KEEP_DROP_RE = re.compile(r"(k|d)(h|l)(\d+)", re.IGNORECASE)
REROLL_RE = re.compile(r"r(\d+)", re.IGNORECASE)
ADV_RE = re.compile(r"\b(adv|dis)\b", re.IGNORECASE)


# ---------------------------
# Core Roller
# ---------------------------

class DiceRoller:
    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.SystemRandom()

    def roll(self, expression: str) -> RollResult:
        expression = expression.strip()

        if not expression:
            raise ValueError("Give me something to roll, like `1d20`, `2d6+3`, or `4d6kh3`.")

        terms: list[DiceTermResult] = []
        modifier_total = 0

        pos = 0
        term_count = 0
        total_dice = 0

        while pos < len(expression):
            m = DICE_RE.match(expression, pos)

            if not m:
                if expression[pos].isspace():
                    pos += 1
                    continue
                raise ValueError(f"Could not parse near `{expression[pos:pos + 10]}`.")

            term_count += 1
            if term_count > MAX_TERMS:
                raise ValueError(f"Expression has too many terms. Max is `{MAX_TERMS}`.")

            sign = -1 if m.group("sign") == "-" else 1

            if m.group("const"):
                const_val = int(m.group("const")) * sign
                modifier_total += const_val
                terms.append(DiceTermResult(term=f"{const_val:+d}", subtotal=const_val))
                pos = m.end()
                continue

            count = int(m.group("count") or "1")
            sides = int(m.group("sides"))
            body = m.group("body") or ""

            if count <= 0:
                raise ValueError("Dice count must be at least `1`.")
            if sides < 2:
                raise ValueError("Dice need at least `2` sides.")
            if count > MAX_DICE_PER_TERM:
                raise ValueError(f"You can roll at most `{MAX_DICE_PER_TERM}` dice per term.")
            if sides > MAX_SIDES:
                raise ValueError(f"Dice can have at most `{MAX_SIDES}` sides.")

            total_dice += count
            if total_dice > MAX_TOTAL_DICE:
                raise ValueError(f"This expression rolls too many dice. Max total dice is `{MAX_TOTAL_DICE}`.")

            adv_mode: Optional[str] = None
            adv_match = ADV_RE.search(body)
            if adv_match:
                adv_mode = adv_match.group(1).lower()
                body = ADV_RE.sub("", body)

            explode = "!" in body
            body = body.replace("!", "")

            keep: Optional[tuple[str, int]] = None
            kd = KEEP_DROP_RE.search(body)
            if kd:
                keep = ((kd.group(1) + kd.group(2)).lower(), int(kd.group(3)))
                body = KEEP_DROP_RE.sub("", body)

            reroll_face: Optional[int] = None
            rr = REROLL_RE.search(body)
            if rr:
                reroll_face = int(rr.group(1))
                body = REROLL_RE.sub("", body)
                if reroll_face < 1 or reroll_face > sides:
                    raise ValueError(f"Reroll face must be between `1` and `{sides}`.")

            comparator: Optional[tuple[str, int]] = None
            comp = COMP_RE.search(body)
            if comp:
                comparator = (comp.group("op"), int(comp.group("thresh")))
                body = COMP_RE.sub("", body)

            if body.strip():
                raise ValueError(f"Unrecognized dice modifier: `{body.strip()}`.")

            dres = self._roll_dice_term(
                count=count,
                sides=sides,
                explode=explode,
                reroll_face=reroll_face,
                keep=keep,
                comparator=comparator,
                adv_mode=adv_mode,
            )

            dres.term = f"{'-' if sign < 0 else ''}{count}d{sides}"
            if adv_mode:
                dres.term += adv_mode
            if explode:
                dres.term += "!"
            if reroll_face is not None:
                dres.term += f"r{reroll_face}"
            if keep:
                dres.term += f"{keep[0]}{keep[1]}"
            if comparator:
                dres.term += f"{comparator[0]}{comparator[1]}"

            dres.subtotal *= sign
            terms.append(dres)
            pos = m.end()

        total = modifier_total + sum(t.subtotal for t in terms if t.rolls)
        detail = self._format_detail(expression, terms, modifier_total, total)

        return RollResult(
            expression=expression,
            total=total,
            terms=terms,
            modifier_total=modifier_total,
            detail=detail,
        )

    def _roll_once(self, sides: int) -> int:
        return self.rng.randint(1, sides)

    def _roll_adv_pair(self, sides: int, mode: str) -> tuple[int, int, int]:
        a = self._roll_once(sides)
        b = self._roll_once(sides)
        kept = max(a, b) if mode == "adv" else min(a, b)
        return a, b, kept

    def _cmp(self, x: int, op: str, y: int) -> bool:
        if op == ">=":
            return x >= y
        if op == "<=":
            return x <= y
        if op == ">":
            return x > y
        if op == "<":
            return x < y
        return x == y

    def _roll_dice_term(
        self,
        *,
        count: int,
        sides: int,
        explode: bool,
        reroll_face: Optional[int],
        keep: Optional[tuple[str, int]],
        comparator: Optional[tuple[str, int]],
        adv_mode: Optional[str],
    ) -> DiceTermResult:
        rolls: list[int] = []
        exploded: list[list[int]] = []
        rerolled_from: list[tuple[int, int]] = []

        for _ in range(count):
            if adv_mode and sides == 20:
                a, b, _ = self._roll_adv_pair(sides, adv_mode)
                rolls.extend([a, b])
                continue

            val = self._roll_once(sides)

            if reroll_face is not None and val == reroll_face:
                new_val = self._roll_once(sides)
                rerolled_from.append((val, new_val))
                val = new_val

            chain = [val]
            if explode:
                explosion_count = 0
                while chain[-1] == sides:
                    explosion_count += 1
                    if explosion_count > MAX_EXPLOSIONS_PER_DIE:
                        raise ValueError(
                            f"Explosion limit hit. Max is `{MAX_EXPLOSIONS_PER_DIE}` explosions per die."
                        )
                    chain.append(self._roll_once(sides))

            if len(chain) > 1:
                exploded.append(chain)

            rolls.append(sum(chain) if explode else chain[-1])

        if adv_mode and sides == 20:
            kept = []
            for i in range(0, len(rolls), 2):
                pair = rolls[i:i + 2]
                if len(pair) < 2:
                    continue
                kept.append(max(pair) if adv_mode == "adv" else min(pair))
        else:
            kept = list(rolls)

        dropped: list[int] = []

        if keep is not None:
            mode, n = keep
            n = max(0, min(n, len(kept)))

            sort_high = mode[1] == "h"
            sorted_idx = sorted(range(len(kept)), key=lambda i: kept[i], reverse=sort_high)

            if mode[0] == "k":
                keep_idx = set(sorted_idx[:n])
            else:
                keep_idx = set(sorted_idx[n:])

            new_kept = []
            for i, v in enumerate(kept):
                if i in keep_idx:
                    new_kept.append(v)
                else:
                    dropped.append(v)
            kept = new_kept

        if comparator is not None:
            op, threshold = comparator
            successes = sum(1 for v in kept if self._cmp(v, op, threshold))
            subtotal = successes
        else:
            successes = None
            subtotal = sum(kept)

        return DiceTermResult(
            term="",
            rolls=rolls,
            kept=kept,
            dropped=dropped,
            exploded=exploded,
            rerolled_from=rerolled_from,
            successes=successes,
            subtotal=subtotal,
        )

    def _format_detail(
        self,
        expression: str,
        terms: list[DiceTermResult],
        modifier_total: int,
        total: int,
    ) -> str:
        lines = [f"Expression: {expression}"]

        for t in terms:
            if not t.rolls:
                continue

            parts = [f"rolls={t.rolls}"]

            if t.kept and t.kept != t.rolls:
                parts.append(f"kept={t.kept}")
            if t.dropped:
                parts.append(f"dropped={t.dropped}")
            if t.rerolled_from:
                parts.append(f"rerolls={t.rerolled_from}")
            if t.exploded:
                parts.append(f"exploded={t.exploded}")
            if t.successes is not None:
                parts.append(f"successes={t.successes}")

            parts.append(f"subtotal={t.subtotal}")
            lines.append(f"- {t.term}: " + ", ".join(parts))

        if modifier_total:
            lines.append(f"Modifiers total: {modifier_total:+d}")

        lines.append(f"TOTAL = {total}")
        return "\n".join(lines)


def roll(expression: str, *, rng: Optional[random.Random] = None) -> RollResult:
    return DiceRoller(rng=rng).roll(expression)


# ---------------------------
# Presentation Helpers
# ---------------------------

def normalize_mode(mode: str) -> str:
    mode = (mode or "normal").lower().strip()
    if mode in ("adv", "advantage"):
        return "advantage"
    if mode in ("dis", "disadvantage"):
        return "disadvantage"
    return "normal"


def apply_mode_to_expression(dice_expr: str, mode: str) -> str:
    dice_expr = dice_expr.strip()
    mode = normalize_mode(mode)

    if mode == "advantage":
        if re.fullmatch(r"\s*(?:1)?d20\s*(?:[+-]\s*\d+)?\s*", dice_expr, re.IGNORECASE):
            return re.sub(r"d20", "2d20adv", dice_expr, count=1, flags=re.IGNORECASE)
    elif mode == "disadvantage":
        if re.fullmatch(r"\s*(?:1)?d20\s*(?:[+-]\s*\d+)?\s*", dice_expr, re.IGNORECASE):
            return re.sub(r"d20", "2d20dis", dice_expr, count=1, flags=re.IGNORECASE)

    return dice_expr


def add_modifier_to_expression(dice_expr: str, modifier: int) -> str:
    dice_expr = dice_expr.strip()
    if modifier == 0:
        return dice_expr
    if modifier > 0:
        return f"{dice_expr}+{modifier}"
    return f"{dice_expr}{modifier}"


def trim_list(values: list[int], limit: int = 60) -> str:
    if not values:
        return "—"

    if len(values) <= limit:
        return ", ".join(str(v) for v in values)

    visible = ", ".join(str(v) for v in values[:limit])
    remaining = len(values) - limit
    return f"{visible}, ... and {remaining} more"


def has_single_d20_crit(res: RollResult) -> Optional[int]:
    if len(res.terms) != 1:
        return None

    term = res.terms[0]
    if "d20" not in term.term.lower():
        return None
    if len(term.kept) != 1:
        return None
    if term.successes is not None:
        return None

    return term.kept[0]


def build_roll_embed(
    interaction: discord.Interaction,
    res: RollResult,
    *,
    hidden: bool,
    title_override: str | None = None,
    description: str | None = None,
) -> discord.Embed:
    title = title_override or "🎲 Dice Roll"
    color = discord.Color.gold()

    if title_override is None:
        crit = has_single_d20_crit(res)
        if crit == 20:
            title = "🎯 Critical Success!"
            color = discord.Color.green()
        elif crit == 1:
            title = "💀 Critical Failure"
            color = discord.Color.red()

    if hidden:
        title += " — Hidden"

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    embed.add_field(name="Expression", value=f"`{res.expression}`", inline=True)
    embed.add_field(name="Total", value=f"**{res.total}**", inline=True)

    if res.modifier_total:
        embed.add_field(name="Flat Modifiers", value=f"`{res.modifier_total:+d}`", inline=True)

    for index, term in enumerate(res.terms, start=1):
        if not term.rolls:
            continue

        lines = [f"**Rolls:** `{trim_list(term.rolls)}`"]

        if term.kept and term.kept != term.rolls:
            lines.append(f"**Kept:** `{trim_list(term.kept)}`")
        if term.dropped:
            lines.append(f"**Dropped:** `{trim_list(term.dropped)}`")
        if term.rerolled_from:
            rerolls = ", ".join(f"{old}→{new}" for old, new in term.rerolled_from[:30])
            if len(term.rerolled_from) > 30:
                rerolls += f", ... and {len(term.rerolled_from) - 30} more"
            lines.append(f"**Rerolls:** `{rerolls}`")
        if term.exploded:
            chains = ", ".join(str(chain) for chain in term.exploded[:15])
            if len(term.exploded) > 15:
                chains += f", ... and {len(term.exploded) - 15} more"
            lines.append(f"**Exploded:** `{chains}`")
        if term.successes is not None:
            lines.append(f"**Successes:** `{term.successes}`")

        lines.append(f"**Subtotal:** `{term.subtotal}`")

        embed.add_field(
            name=f"Term {index}: `{term.term}`",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"Rolled by {interaction.user.display_name}")
    return embed


def can_hide_roll(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False

    perms = interaction.user.guild_permissions
    if perms.manage_guild or perms.administrator:
        return True

    staff_role_ids = getattr(bot, "staff_role_ids", set()) or set()
    return any(role.id in staff_role_ids for role in interaction.user.roles)


def is_staff(bot: commands.Bot, interaction: discord.Interaction) -> bool:
    return can_hide_roll(bot, interaction)


# ---------------------------
# Autocomplete
# ---------------------------

async def mode_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name="normal", value="normal"),
        app_commands.Choice(name="advantage", value="advantage"),
        app_commands.Choice(name="disadvantage", value="disadvantage"),
    ]

    current = current.lower().strip()
    return [choice for choice in choices if current in choice.name.lower()][:25]


async def table_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower().strip()
    choices = [
        app_commands.Choice(name=key, value=key)
        for key in sorted(ROLL_TABLES)
        if not current or current in key.lower()
    ]
    return choices[:25]


async def stat_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    labels = {
        **CORE_STAT_LABELS,
        **DERIVED_STAT_LABELS,
    }

    current_lower = current.strip().lower()
    choices: list[app_commands.Choice[str]] = []

    for key, label in labels.items():
        aliases = [k for k, v in STAT_ALIASES.items() if v == key]
        search_blob = " ".join([key, label, *aliases]).lower()
        if not current_lower or current_lower in search_blob:
            choices.append(app_commands.Choice(name=label, value=key))

    return choices[:25]


# ---------------------------
# Discord Cog
# ---------------------------

class Dice(commands.GroupCog, group_name="dice", group_description="Roll dice"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sb = getattr(bot, "supabase", None)
        self.stats = StatsService(self.sb) if self.sb is not None else None
        self.traits = TraitsService(self.sb) if self.sb is not None else None
        super().__init__()

    async def character_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        if self.sb is None:
            return []

        try:
            user_id = interaction.user.id
            staff = is_staff(self.bot, interaction)

            query = (
                self.sb.table("characters")
                .select("character_id,name,user_id,is_active")
                .order("name")
                .limit(100)
            )

            if not staff:
                query = query.eq("user_id", str(user_id))

            res = query.execute()
            rows = res.data or []

            current_lower = current.strip().lower()
            if current_lower:
                rows = [
                    row for row in rows
                    if current_lower in str(row.get("name", "")).lower()
                    or current_lower in str(row.get("character_id", "")).lower()
                    or current_lower in str(row.get("user_id", "")).lower()
                ]

            choices: list[app_commands.Choice[str]] = []
            for row in rows[:25]:
                name = str(row.get("name") or "Unnamed Character")
                char_id = str(row.get("character_id"))
                active = bool(row.get("is_active", False))
                status = "active" if active else "inactive"

                if staff:
                    owner = str(row.get("user_id", "unknown"))
                    label = f"{name} • {status} • {owner}"
                else:
                    label = f"{name} • {status}"

                choices.append(app_commands.Choice(name=label[:100], value=char_id))

            return choices
        except Exception:
            return []

    def _get_character_row(self, character_id: str) -> dict[str, Any] | None:
        if self.sb is None:
            return None

        try:
            res = (
                self.sb.table("characters")
                .select("*")
                .eq("character_id", character_id)
                .limit(1)
                .execute()
            )
            rows = res.data or []
            return rows[0] if rows else None
        except Exception:
            return None

    def _build_core_stat_map(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        core_map: dict[str, int] = {}

        for row in rows:
            stat_def = row["definition"]
            stat = row["stat"]

            raw_key = str(stat_def.get("key") or stat_def.get("stat_key") or "").strip().lower()
            value = int(stat.get("value") or 0)

            if raw_key == "strength":
                core_map["strength"] = value
            elif raw_key == "dexterity":
                core_map["dexterity"] = value
            elif raw_key == "stamina":
                core_map["stamina"] = value
            elif raw_key in ("affinity", "magic_affinity"):
                core_map["magic_affinity"] = value
            elif raw_key == "mana":
                core_map["mana"] = value

        for key in CORE_STAT_ORDER:
            core_map.setdefault(key, 0)

        return core_map

    def _load_character_stats(
        self,
        *,
        guild_id: int,
        character_id: str,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
        if self.sb is None or self.stats is None or self.traits is None:
            raise RuntimeError("Supabase is not configured on the bot.")

        character_row = self._get_character_row(character_id)
        if not character_row:
            raise ValueError("That character could not be found.")

        rows = self.stats.get_all_character_stats(
            guild_id=guild_id,
            character_id=character_id,
            include_hidden=False,
        )

        if not rows:
            raise ValueError("No stat definitions exist for this server yet.")

        base_core_stats = self._build_core_stat_map(rows)

        trait_bundles = self.traits.get_character_traits(
            guild_id=guild_id,
            character_id=character_id,
        )

        trait_result = apply_trait_modifiers(
            core_stats=base_core_stats,
            trait_bundles=trait_bundles,
        )

        core_stats = trait_result["core_stats"]
        trait_extras = trait_result.get("extras", {})

        derived_stats = calculate_derived_stats(core_stats)

        # Match the stats cog's carry capacity trait behavior.
        if "carry_capacity" in derived_stats:
            derived_stats["carry_capacity"] = int(derived_stats["carry_capacity"]) + int(
                trait_extras.get("carry_capacity_bonus", 0)
            )

        return character_row, core_stats, derived_stats

    def _resolve_stat_value(
        self,
        *,
        stat: str,
        core_stats: dict[str, int],
        derived_stats: dict[str, int],
    ) -> tuple[str, int]:
        normalized = STAT_ALIASES.get(stat.lower().strip(), stat.lower().strip())

        if normalized in core_stats:
            return CORE_STAT_LABELS.get(normalized, normalized.title()), int(core_stats[normalized])

        if normalized in derived_stats:
            return DERIVED_STAT_LABELS.get(normalized, normalized.title()), int(derived_stats[normalized])

        raise ValueError("Unknown stat. Use autocomplete or try `strength`, `dexterity`, `stamina`, `affinity`, or `mana`.")

    @app_commands.guilds(discord.Object(id=KEYSTONE_GUILD_ID))
    @app_commands.command(
        name="roll",
        description="Roll dice. Examples: 1d20, 4d6+2, 4d6kh3, 2d20adv+5, 10d6>=5",
    )
    @app_commands.describe(
        dice="What to roll, like 1d20, 2d6+3, 4d6kh3, 2d20adv+5, or 10d6>=5",
        mode="Optional helper: normal, advantage, or disadvantage. Adv/dis only affects plain d20 rolls.",
        hidden="Staff/GM only: hide the roll from public chat",
    )
    @app_commands.autocomplete(mode=mode_autocomplete)
    async def roll_cmd(
        self,
        interaction: discord.Interaction,
        dice: str,
        mode: str = "normal",
        hidden: bool = False,
    ):
        mode = normalize_mode(mode)

        if mode not in ("normal", "advantage", "disadvantage"):
            await interaction.response.send_message(
                "❌ Mode must be `normal`, `advantage`, or `disadvantage`.",
                ephemeral=True,
            )
            return

        if hidden and not can_hide_roll(self.bot, interaction):
            await interaction.response.send_message(
                "❌ Only staff/GMs can use hidden rolls.",
                ephemeral=True,
            )
            return

        dice_expr = apply_mode_to_expression(dice, mode)

        try:
            res = roll(dice_expr)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        embed = build_roll_embed(interaction, res, hidden=hidden)
        await interaction.response.send_message(embed=embed, ephemeral=hidden)

    @app_commands.guilds(discord.Object(id=KEYSTONE_GUILD_ID))
    @app_commands.command(
        name="contest",
        description="Roll two sides against each other and declare the winner.",
    )
    @app_commands.describe(
        actor="First side, character, or player name",
        opponent="Second side, character, or player name",
        actor_dice="Dice for the first side, like 1d20+3",
        opponent_dice="Dice for the second side. Defaults to actor_dice if blank.",
        hidden="Staff/GM only: hide the contest from public chat",
    )
    async def contest_cmd(
        self,
        interaction: discord.Interaction,
        actor: str,
        opponent: str,
        actor_dice: str = "1d20",
        opponent_dice: str | None = None,
        hidden: bool = False,
    ):
        if hidden and not can_hide_roll(self.bot, interaction):
            await interaction.response.send_message(
                "❌ Only staff/GMs can use hidden rolls.",
                ephemeral=True,
            )
            return

        opponent_dice = opponent_dice or actor_dice

        try:
            actor_res = roll(actor_dice)
            opponent_res = roll(opponent_dice)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        if actor_res.total > opponent_res.total:
            outcome = f"🏆 **{actor} wins!**"
        elif opponent_res.total > actor_res.total:
            outcome = f"🏆 **{opponent} wins!**"
        else:
            outcome = "🤝 **Tie!**"

        title = "⚔️ Dice Contest"
        if hidden:
            title += " — Hidden"

        embed = discord.Embed(
            title=title,
            description=outcome,
            color=discord.Color.orange(),
        )

        embed.add_field(
            name=f"{actor}",
            value=(
                f"Expression: `{actor_res.expression}`\n"
                f"Total: **{actor_res.total}**\n"
                f"Detail: `{actor_res.detail[:850]}`"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"{opponent}",
            value=(
                f"Expression: `{opponent_res.expression}`\n"
                f"Total: **{opponent_res.total}**\n"
                f"Detail: `{opponent_res.detail[:850]}`"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Rolled by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed, ephemeral=hidden)

    @app_commands.guilds(discord.Object(id=KEYSTONE_GUILD_ID))
    @app_commands.command(
        name="table",
        description="Roll on a built-in table like travel, weather, encounter, loot, injury, or shop_event.",
    )
    @app_commands.describe(
        table="Which table to roll on",
        hidden="Staff/GM only: hide the table result from public chat",
    )
    @app_commands.autocomplete(table=table_autocomplete)
    async def table_cmd(
        self,
        interaction: discord.Interaction,
        table: str,
        hidden: bool = False,
    ):
        if hidden and not can_hide_roll(self.bot, interaction):
            await interaction.response.send_message(
                "❌ Only staff/GMs can use hidden table rolls.",
                ephemeral=True,
            )
            return

        table_key = table.lower().strip()
        entries = ROLL_TABLES.get(table_key)

        if not entries:
            valid = ", ".join(f"`{key}`" for key in sorted(ROLL_TABLES))
            await interaction.response.send_message(
                f"❌ Unknown table. Try one of: {valid}",
                ephemeral=True,
            )
            return

        roll_value = random.SystemRandom().randint(1, len(entries))
        result = entries[roll_value - 1]

        title = f"🎲 Table Roll: {table_key}"
        if hidden:
            title += " — Hidden"

        embed = discord.Embed(
            title=title,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Roll", value=f"`{roll_value}` / `{len(entries)}`", inline=True)
        embed.add_field(name="Result", value=f"**{result}**", inline=False)
        embed.set_footer(text=f"Rolled by {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed, ephemeral=hidden)

    @app_commands.guilds(discord.Object(id=KEYSTONE_GUILD_ID))
    @app_commands.command(
        name="stat",
        description="Roll dice using a character stat as the modifier.",
    )
    @app_commands.describe(
        character="Select a character",
        stat="Which stat to add as the modifier",
        dice="Base dice to roll. Defaults to 1d20.",
        mode="Optional helper: normal, advantage, or disadvantage. Adv/dis only affects plain d20 rolls.",
        hidden="Staff/GM only: hide the roll from public chat",
    )
    @app_commands.autocomplete(character=character_autocomplete, stat=stat_autocomplete, mode=mode_autocomplete)
    async def stat_cmd(
        self,
        interaction: discord.Interaction,
        character: str,
        stat: str,
        dice: str = "1d20",
        mode: str = "normal",
        hidden: bool = False,
    ):
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if hidden and not can_hide_roll(self.bot, interaction):
            await interaction.response.send_message(
                "❌ Only staff/GMs can use hidden stat rolls.",
                ephemeral=True,
            )
            return

        try:
            character_row, core_stats, derived_stats = self._load_character_stats(
                guild_id=interaction.guild_id,
                character_id=character,
            )
            stat_label, stat_value = self._resolve_stat_value(
                stat=stat,
                core_stats=core_stats,
                derived_stats=derived_stats,
            )
            dice_expr = apply_mode_to_expression(dice, normalize_mode(mode))
            dice_expr = add_modifier_to_expression(dice_expr, stat_value)
            res = roll(dice_expr)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        character_name = str(character_row.get("name") or "Unnamed Character")
        description = (
            f"**Character:** {character_name}\n"
            f"**Stat:** {stat_label} `{stat_value:+d}`"
        )

        embed = build_roll_embed(
            interaction,
            res,
            hidden=hidden,
            title_override="🎲 Stat Roll",
            description=description,
        )

        await interaction.response.send_message(embed=embed, ephemeral=hidden)


# ---------------------------
# Extension entry point
# ---------------------------

async def setup(bot: commands.Bot):
    if inspect.iscoroutinefunction(bot.add_cog):
        await bot.add_cog(Dice(bot))
    else:
        bot.add_cog(Dice(bot))

    print("[cogs/dice] Loaded Dice cog and registered /dice roll, /dice contest, /dice table, /dice stat")
