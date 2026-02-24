# cogs/dice.py
from __future__ import annotations

import re
import random
import inspect
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

"""
Universal Dice Roller
---------------------
Supports complex expressions (e.g., 4d6kh3+2, 2d20adv+5, 10d6>=5, 1d10!+1d6r1-2)
Modifiers: +/-, keep/drop (kh/kl/dh/dl), explode (!), reroll (rN), adv/dis (d20),
success counting (>=, >, <=, <, =).
"""

# ---------------------------
# CONFIG
# ---------------------------

SKYFALL_GUILD_ID = 1374730886234374235

# ---------------------------
# Data structures
# ---------------------------

@dataclass
class DiceTermResult:
    term: str
    rolls: List[int] = field(default_factory=list)
    kept: List[int] = field(default_factory=list)
    dropped: List[int] = field(default_factory=list)
    exploded: List[List[int]] = field(default_factory=list)  # per die chain
    rerolled_from: List[Tuple[int, int]] = field(default_factory=list)  # (from, to)
    successes: Optional[int] = None  # when using comparators
    subtotal: int = 0

@dataclass
class RollResult:
    expression: str
    total: int
    terms: List[DiceTermResult]
    modifier_total: int
    detail: str

# ---------------------------
# Core Roller
# ---------------------------

DICE_RE = re.compile(
    r"\s*"
    r"(?P<sign>[+-])?"
    r"(?:(?P<count>\d*)d(?P<sides>\d+)(?P<body>[^+\-]*)|(?P<const>\d+))",
    re.IGNORECASE
)

COMP_RE = re.compile(r"(?P<op>>=|<=|=|>|<)\s*(?P<thresh>\d+)")
KEEP_DROP_RE = re.compile(r"(k|d)(h|l)(\d+)")
REROLL_RE = re.compile(r"r(\d+)")
ADV_RE = re.compile(r"\b(adv|dis)\b", re.IGNORECASE)

class DiceRoller:
    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.SystemRandom()

    def roll(self, expression: str, *, max_terms: int = 100, max_dice: int = 5000) -> RollResult:
        terms: List[DiceTermResult] = []
        modifier_total = 0

        pos = 0
        term_count = 0
        while pos < len(expression):
            m = DICE_RE.match(expression, pos)
            if not m:
                if expression[pos].isspace():
                    pos += 1
                    continue
                raise ValueError(f"Could not parse at position {pos}: ...{expression[pos:pos+10]!r}")

            term_count += 1
            if term_count > max_terms:
                raise ValueError("Expression has too many terms.")

            sign = -1 if (m.group('sign') == '-') else 1
            if m.group('const'):
                const_val = int(m.group('const')) * sign
                modifier_total += const_val
                terms.append(DiceTermResult(term=f"{('' if sign>0 else '-')}{const_val if sign>0 else -const_val}"))
                pos = m.end()
                continue

            count = int(m.group('count') or '1')
            sides = int(m.group('sides'))
            body = m.group('body') or ''
            if count <= 0 or sides <= 0:
                raise ValueError("Dice count and sides must be positive.")
            if count > max_dice:
                raise ValueError("Dice count exceeds limit.")

            adv_mode = None
            match = ADV_RE.search(body)
            if match:
                adv_mode = match.group(1).lower()
                body = ADV_RE.sub('', body)

            explode = '!' in body
            body = body.replace('!', '')

            keep: Optional[Tuple[str, int]] = None
            kd = KEEP_DROP_RE.search(body)
            if kd:
                keep = (kd.group(1)+kd.group(2), int(kd.group(3)))
                body = KEEP_DROP_RE.sub('', body)

            reroll_face: Optional[int] = None
            rr = REROLL_RE.search(body)
            if rr:
                reroll_face = int(rr.group(1))
                body = REROLL_RE.sub('', body)

            comp = COMP_RE.search(body)
            comparator: Optional[Tuple[str, int]] = None
            if comp:
                comparator = (comp.group('op'), int(comp.group('thresh')))
                body = COMP_RE.sub('', body)

            if body.strip():
                raise ValueError(f"Unrecognized modifier segment: {body!r}")

            dres = self._roll_dice_term(count, sides, explode, reroll_face, keep, comparator, adv_mode)
            dres.term = f"{('-' if sign<0 else '')}{count}d{sides}"
            if adv_mode:
                dres.term += adv_mode
            if explode:
                dres.term += '!'
            if reroll_face is not None:
                dres.term += f"r{reroll_face}"
            if keep:
                dres.term += f"{keep[0]}{keep[1]}"
            if comparator:
                dres.term += f"{comparator[0]}{comparator[1]}"

            dres.subtotal *= sign
            terms.append(dres)
            pos = m.end()

        total = modifier_total + sum(t.subtotal for t in terms)
        detail = self._format_detail(expression, terms, modifier_total, total)
        return RollResult(expression=expression, total=total, terms=terms, modifier_total=modifier_total, detail=detail)

    # ---------------------------
    # Internals
    # ---------------------------

    def _roll_once(self, sides: int) -> int:
        return self.rng.randint(1, sides)

    def _roll_adv_pair(self, sides: int, mode: str) -> int:
        a = self._roll_once(sides)
        b = self._roll_once(sides)
        return max(a, b) if mode == 'adv' else min(a, b)

    def _cmp(self, x: int, op: str, y: int) -> bool:
        if op == '>=': return x >= y
        if op == '<=': return x <= y
        if op == '>':  return x > y
        if op == '<':  return x < y
        return x == y

    def _roll_dice_term(
        self,
        count: int,
        sides: int,
        explode: bool,
        reroll_face: Optional[int],
        keep: Optional[Tuple[str, int]],
        comparator: Optional[Tuple[str, int]],
        adv_mode: Optional[str],
    ) -> DiceTermResult:
        rolls: List[int] = []
        exploded: List[List[int]] = []
        rerolled_from: List[Tuple[int, int]] = []

        for _ in range(count):
            if adv_mode and sides == 20:
                val = self._roll_adv_pair(sides, adv_mode)
            else:
                val = self._roll_once(sides)

            if reroll_face is not None and val == reroll_face:
                new_val = self._roll_once(sides)
                rerolled_from.append((val, new_val))
                val = new_val

            chain = [val]
            if explode:
                while chain[-1] == sides:
                    nxt = self._roll_once(sides)
                    chain.append(nxt)
            if len(chain) > 1:
                exploded.append(chain)
            rolls.append(chain[-1])

        kept = list(rolls)
        dropped: List[int] = []

        if keep is not None:
            mode, n = keep
            n = max(0, min(n, len(rolls)))
            sorted_idx = sorted(range(len(rolls)), key=lambda i: rolls[i], reverse=(mode[1] == 'h'))
            if mode[0] == 'k':
                keep_idx = set(sorted_idx[:n])
            else:
                keep_idx = set(sorted_idx[n:])
            new_kept = []
            for i, v in enumerate(rolls):
                if i in keep_idx:
                    new_kept.append(v)
                else:
                    dropped.append(v)
            kept = new_kept

        if comparator is not None:
            op, thr = comparator
            successes = sum(1 for v in kept if self._cmp(v, op, thr))
            subtotal = successes
        else:
            subtotal = sum(kept)

        term_res = DiceTermResult(
            term="",
            rolls=rolls,
            kept=kept,
            dropped=dropped,
            exploded=exploded,
            rerolled_from=rerolled_from,
            successes=(subtotal if comparator is not None else None),
            subtotal=subtotal,
        )
        return term_res

    def _format_detail(self, expr: str, terms: List[DiceTermResult], mod_total: int, total: int) -> str:
        lines = [f"Expression: {expr}"]
        for t in terms:
            parts = []
            if t.rolls:         parts.append(f"rolls={t.rolls}")
            if t.dropped:       parts.append(f"dropped={t.dropped}")
            if t.rerolled_from: parts.append(f"rerolls={t.rerolled_from}")
            if t.exploded:      parts.append(f"exploded={t.exploded}")
            if t.successes is not None:
                parts.append(f"successes={t.successes}")
            parts.append(f"subtotal={t.subtotal}")
            lines.append(f"- {t.term}: " + ", ".join(parts))
        if mod_total:
            lines.append(f"Modifiers total: {mod_total}")
        lines.append(f"TOTAL = {total}")
        return "\n".join(lines)

# ---------------------------
# Convenience function
# ---------------------------

def roll(expression: str, *, rng: Optional[random.Random] = None) -> RollResult:
    return DiceRoller(rng=rng).roll(expression)

# ---------------------------
# Compact presentation helpers
# ---------------------------

def _looks_plain_multi(t: DiceTermResult, res: RollResult) -> bool:
    """True when we can show a simple table:
       - multiple dice (len(rolls) > 1)
       - no success mode, no explode/reroll/keep/drop modifiers
       - no global modifier_total
    """
    return (
        len(t.rolls) > 1
        and t.successes is None
        and not t.dropped
        and not t.exploded
        and not t.rerolled_from
        and res.modifier_total == 0
    )

def build_compact_table(term_label: str, rolls: List[int], subtotal: int) -> str:
    """Render a small monospace table with rolls and sum."""
    left_h, right_h = "rolls", "sum"
    left_v = " ".join(str(x) for x in rolls)
    right_v = f"[{subtotal}]"

    w0 = len(term_label)
    w1 = max(len(left_h), len(left_v))
    w2 = max(len(right_h), len(right_v))

    def row(a, b, c):
        return f"│ {a:{w0}} │ {b:{w1}} │ {c:{w2}} │"

    top    = f"┌{'─'*(w0+2)}┬{'─'*(w1+2)}┬{'─'*(w2+2)}┐"
    mid    = f"├{'─'*(w0+2)}┼{'─'*(w1+2)}┼{'─'*(w2+2)}┤"
    bot    = f"└{'─'*(w0+2)}┴{'─'*(w1+2)}┴{'─'*(w2+2)}┘"

    lines = [
        top,
        row(term_label, left_h, right_h),
        mid,
        row("", left_v, right_v),
        bot
    ]
    return "```\n" + "\n".join(lines) + "\n```"

# ---------------------------
# Discord Cog
# ---------------------------

class Dice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.guilds(discord.Object(id=SKYFALL_GUILD_ID))
    @app_commands.command(
        name="roll",
        description="Roll a dice! (e.g., 1d20, 4d6+2, 2d20adv+5, 10d6>=5)"
    )
    @app_commands.describe(
        dice="What to roll, e.g. 1d20 or 4d6+2"
    )
    async def roll_cmd(self, interaction: discord.Interaction, dice: str):
        try:
            res = roll(dice)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        # If it's a single plain multi-dice term like 2d20, show a compact table with the individual dice.
        if len(res.terms) == 1 and _looks_plain_multi(res.terms[0], res):
            t = res.terms[0]
            table = build_compact_table(term_label=t.term, rolls=t.rolls, subtotal=t.subtotal)
            await interaction.response.send_message(table)
            return

        # Otherwise, send the simple embed (with crit flair for single d20)
        title = "🎲 Dice Roll"
        color = 0x5865F2
        if (
            len(res.terms) == 1
            and ("d20" in res.terms[0].term)
            and len(res.terms[0].kept) == 1
            and res.terms[0].successes is None
        ):
            v = res.terms[0].kept[0]
            if v == 20:
                title = "🎯 Critical Success!"
                color = 0x43B581
            elif v == 1:
                title = "💀 Critical Failure"
                color = 0xF04747

        embed = discord.Embed(
            title=title,
            description=f"**Result:** {res.total}\nRolled `{dice}`",
            color=color
        )
        await interaction.response.send_message(embed=embed)

# ---------------------------
# Extension entry point (async)
# ---------------------------

async def setup(bot: commands.Bot):
    """Async setup for environments where `load_extension` awaits setup."""
    if inspect.iscoroutinefunction(bot.add_cog):
        await bot.add_cog(Dice(bot))
    else:
        bot.add_cog(Dice(bot))
    print("[cogs/dice] Loaded Dice cog and registered /roll")
