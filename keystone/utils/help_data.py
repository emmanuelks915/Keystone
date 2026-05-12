# utils/help_data.py — Keystone Codex help registry
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class HelpEntry:
    command: str
    category: str
    description: str
    usage: str
    examples: tuple[str, ...] = field(default_factory=tuple)
    tips: tuple[str, ...] = field(default_factory=tuple)
    staff_only: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


# This registry is intentionally small-but-useful at first.
# The help cog will ALSO auto-scan live slash commands, so missing entries still show up.
# Add richer notes here over time for your biggest systems.
HELP_ENTRIES: dict[str, HelpEntry] = {
    # Player core
    "oc create": HelpEntry(
        command="oc create",
        category="Character & OC",
        description="Create a new OC profile in Keystone.",
        usage="/oc create",
        examples=("/oc create",),
        tips=("After creating an OC, use /oc select so Keystone knows which OC is active.",),
    ),
    "oc list": HelpEntry(
        command="oc list",
        category="Character & OC",
        description="View your registered OCs.",
        usage="/oc list",
        examples=("/oc list",),
    ),
    "oc select": HelpEntry(
        command="oc select",
        category="Character & OC",
        description="Choose which OC Keystone should use for OC-based commands.",
        usage="/oc select",
        examples=("/oc select",),
        tips=("If a command says it cannot find your active OC, use this first.",),
    ),
    "xp balance": HelpEntry(
        command="xp balance",
        category="XP & Stats",
        description="View XP for your active OC.",
        usage="/xp balance",
        examples=("/xp balance",),
    ),
    "xp history": HelpEntry(
        command="xp history",
        category="XP & Stats",
        description="View recent XP transactions for your OC.",
        usage="/xp history",
        examples=("/xp history",),
    ),
    "xp buy_stat": HelpEntry(
        command="xp buy_stat",
        category="XP & Stats",
        description="Spend XP to increase a stat.",
        usage="/xp buy_stat",
        examples=("/xp buy_stat",),
        tips=("If stats do not look updated, check that you are viewing the correct active OC.",),
    ),
    "stats view": HelpEntry(
        command="stats view",
        category="XP & Stats",
        description="View an OC's current stats and derived values.",
        usage="/stats view",
        examples=("/stats view",),
    ),
    "roll": HelpEntry(
        command="roll",
        category="Dice & Combat",
        description="Roll standard dice notation.",
        usage="/roll dice:<dice> mode:<normal/adv/dis> hidden:<true/false>",
        examples=("/roll dice:1d20+5", "/roll dice:2d6 mode:adv", "/roll dice:1d100 hidden:true"),
        tips=("Staff/GM hidden rolls can keep results private when needed.",),
    ),
    "contest": HelpEntry(
        command="contest",
        category="Dice & Combat",
        description="Roll a contested dice check between two sides.",
        usage="/contest",
        examples=("/contest",),
    ),
    "stat": HelpEntry(
        command="stat",
        category="Dice & Combat",
        description="Roll using an OC stat modifier.",
        usage="/stat",
        examples=("/stat",),
    ),
    "inventory view": HelpEntry(
        command="inventory view",
        category="Inventory & Items",
        description="View your active OC inventory.",
        usage="/inventory view",
        examples=("/inventory view",),
    ),
    "inventory item": HelpEntry(
        command="inventory item",
        category="Inventory & Items",
        description="Look up item details like weight, class, or sheet link.",
        usage="/inventory item",
        examples=("/inventory item",),
    ),
    "wallet": HelpEntry(
        command="wallet",
        category="Economy",
        description="View your wallet balance.",
        usage="/wallet",
        examples=("/wallet",),
    ),
    "pay": HelpEntry(
        command="pay",
        category="Economy",
        description="Pay currency to another player or OC.",
        usage="/pay",
        examples=("/pay",),
        tips=("Make sure the recipient and amount are correct before confirming.",),
    ),
    "shop browse": HelpEntry(
        command="shop browse",
        category="Shops & Commerce",
        description="Browse available shop items.",
        usage="/shop browse",
        examples=("/shop browse",),
    ),
    "shop buy": HelpEntry(
        command="shop buy",
        category="Shops & Commerce",
        description="Buy an item from a shop.",
        usage="/shop buy",
        examples=("/shop buy",),
    ),
    "giveaway entries": HelpEntry(
        command="giveaway entries",
        category="Giveaways",
        description="View entries for a giveaway.",
        usage="/giveaway entries",
        examples=("/giveaway entries",),
    ),
    "giveaway status": HelpEntry(
        command="giveaway status",
        category="Giveaways",
        description="View giveaway winners, claims, and fulfillment status.",
        usage="/giveaway status",
        examples=("/giveaway status",),
    ),
    "travel quote": HelpEntry(
        command="travel quote",
        category="Travel",
        description="Preview travel details before starting a trip.",
        usage="/travel quote",
        examples=("/travel quote",),
    ),
    "travel board": HelpEntry(
        command="travel board",
        category="Travel",
        description="View the travel departure board.",
        usage="/travel board",
        examples=("/travel board",),
    ),
    "rp open": HelpEntry(
        command="rp open",
        category="RP Tracker",
        description="Open/start an RP tracker entry.",
        usage="/rp open",
        examples=("/rp open",),
    ),
    "rp me": HelpEntry(
        command="rp me",
        category="RP Tracker",
        description="View your RP tracker status.",
        usage="/rp me",
        examples=("/rp me",),
    ),

    # Staff/Admin
    "xp award": HelpEntry(
        command="xp award",
        category="Staff: XP & Stats",
        description="Award XP to an OC.",
        usage="/xp award",
        examples=("/xp award",),
        staff_only=True,
    ),
    "stats add": HelpEntry(
        command="stats add",
        category="Staff: XP & Stats",
        description="Staff command to add to an OC stat.",
        usage="/stats add",
        examples=("/stats add",),
        staff_only=True,
    ),
    "stats set": HelpEntry(
        command="stats set",
        category="Staff: XP & Stats",
        description="Staff command to set an OC stat directly.",
        usage="/stats set",
        examples=("/stats set",),
        staff_only=True,
    ),
    "inventory grant": HelpEntry(
        command="inventory grant",
        category="Staff: Inventory",
        description="Grant an item to an OC.",
        usage="/inventory grant",
        examples=("/inventory grant",),
        staff_only=True,
    ),
    "inventory take": HelpEntry(
        command="inventory take",
        category="Staff: Inventory",
        description="Remove an item from an OC.",
        usage="/inventory take",
        examples=("/inventory take",),
        staff_only=True,
    ),
    "giveaway start": HelpEntry(
        command="giveaway start",
        category="Staff: Giveaways",
        description="Start a giveaway with prizes, duration, entry rules, and winners.",
        usage="/giveaway start",
        examples=("/giveaway start",),
        staff_only=True,
    ),
    "giveaway stop": HelpEntry(
        command="giveaway stop",
        category="Staff: Giveaways",
        description="End a giveaway immediately and draw winners.",
        usage="/giveaway stop",
        examples=("/giveaway stop",),
        staff_only=True,
    ),
    "giveaway reroll": HelpEntry(
        command="giveaway reroll",
        category="Staff: Giveaways",
        description="Reroll a giveaway and reverse prizes when needed.",
        usage="/giveaway reroll",
        examples=("/giveaway reroll",),
        staff_only=True,
    ),
    "shop approve": HelpEntry(
        command="shop approve",
        category="Staff: Shops & Commerce",
        description="Approve a pending shop item/order depending on workflow.",
        usage="/shop approve",
        examples=("/shop approve",),
        staff_only=True,
    ),
    "shop deny": HelpEntry(
        command="shop deny",
        category="Staff: Shops & Commerce",
        description="Deny a pending shop item/order depending on workflow.",
        usage="/shop deny",
        examples=("/shop deny",),
        staff_only=True,
    ),
    "mint": HelpEntry(
        command="mint",
        category="Staff: Economy Admin",
        description="Mint currency into a wallet.",
        usage="/mint",
        examples=("/mint",),
        staff_only=True,
    ),
    "burn": HelpEntry(
        command="burn",
        category="Staff: Economy Admin",
        description="Burn/remove currency from a wallet.",
        usage="/burn",
        examples=("/burn",),
        staff_only=True,
    ),
    "setbalance": HelpEntry(
        command="setbalance",
        category="Staff: Economy Admin",
        description="Set a wallet balance directly.",
        usage="/setbalance",
        examples=("/setbalance",),
        staff_only=True,
    ),
    "currency create": HelpEntry(
        command="currency create",
        category="Staff: Configuration",
        description="Create a server currency.",
        usage="/currency create",
        examples=("/currency create",),
        staff_only=True,
    ),
    "currency set primary": HelpEntry(
        command="currency set primary",
        category="Staff: Configuration",
        description="Set the server's primary currency.",
        usage="/currency set primary",
        examples=("/currency set primary",),
        staff_only=True,
    ),
    "sync": HelpEntry(
        command="sync",
        category="Staff: Debugging",
        description="Dev/Admin command to sync slash commands.",
        usage="/sync",
        examples=("/sync",),
        staff_only=True,
    ),
    "reload": HelpEntry(
        command="reload",
        category="Staff: Debugging",
        description="Dev/Admin command to reload a cog.",
        usage="/reload cog:<name>",
        examples=("/reload cog:help", "/reload cog:cogs.help"),
        staff_only=True,
    ),
}

FAQS: dict[str, dict[str, str]] = {
    "getting_started": {
        "title": "Getting Started",
        "body": "Start with `/oc create`, then `/oc select`. After that, most OC-based systems know who you are playing.",
    },
    "active_oc": {
        "title": "Why does Keystone say I have no active OC?",
        "body": "Most OC commands use your selected OC. Run `/oc list`, then `/oc select` to choose the OC Keystone should use.",
    },
    "xp": {
        "title": "How do I check or spend XP?",
        "body": "Use `/xp balance` to check XP, `/xp history` to audit gains/spending, and `/xp buy_stat` to spend XP on stats.",
    },
    "dice": {
        "title": "How do dice work?",
        "body": "Use `/roll` for standard dice notation like `1d20+5`. Use `/stat` for OC-stat-based rolls and `/contest` for opposed checks.",
    },
    "shops": {
        "title": "How do shops work?",
        "body": "Use `/shop browse` to view listings and `/shop buy` to purchase. Some purchases or player shop items may require staff approval.",
    },
    "giveaways": {
        "title": "How do giveaways work?",
        "body": "Giveaways are usually started by staff. Players can enter through the giveaway message/buttons, and `/giveaway status` can show winners or fulfillment status.",
    },
    "rp_tracker": {
        "title": "What does the RP tracker do?",
        "body": "The RP tracker helps track active RP participation and can help staff see who has or has not posted yet.",
    },
    "staff_approval": {
        "title": "Where do staff approvals happen?",
        "body": "Approvals are split by system. Shops, giveaways, XP/stat changes, and RP tools each have staff-facing commands or approval cards.",
    },
}

PLAYER_CATEGORY_ORDER = (
    "Getting Started",
    "Character & OC",
    "XP & Stats",
    "Dice & Combat",
    "Inventory & Items",
    "Economy",
    "Shops & Commerce",
    "Giveaways",
    "RP Tracker",
    "Travel",
)

STAFF_CATEGORY_ORDER = (
    "Staff: XP & Stats",
    "Staff: Inventory",
    "Staff: Economy Admin",
    "Staff: Shops & Commerce",
    "Staff: Giveaways",
    "Staff: RP Management",
    "Staff: Configuration",
    "Staff: Debugging",
)

STAFF_HINT_WORDS = (
    "staff",
    "admin",
    "dev",
    "gm",
    "moderation",
    "approve",
    "deny",
    "grant",
    "remove",
    "set",
    "mint",
    "burn",
    "sync",
    "reload",
    "wipe",
    "debug",
    "schedule",
    "run_now",
)


def normalize_command_name(value: str) -> str:
    return " ".join(value.strip().lower().replace("/", "").split())


def get_entry(command_name: str) -> HelpEntry | None:
    return HELP_ENTRIES.get(normalize_command_name(command_name))


def iter_entries(*, staff: bool | None = None) -> Iterable[HelpEntry]:
    for entry in HELP_ENTRIES.values():
        if staff is None or entry.staff_only is staff:
            yield entry
