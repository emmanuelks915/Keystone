from __future__ import annotations

from typing import Any


CORE_STAT_KEYS = {
    "strength",
    "dexterity",
    "stamina",
    "magic_affinity",
    "mana",
}

DERIVED_STAT_KEYS = {
    "reaction_score",
    "fortitude",
    "safe_output",
    "magic_safe_output",
    "ap",
    "carry_capacity",
    "dodge",
}

FLAT_BONUS_KEYS = {
    "luck",
    "carry_capacity",
}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def apply_trait_modifiers(
    *,
    core_stats: dict[str, int],
    trait_bundles: list[dict[str, Any]],
) -> dict[str, Any]:
    stats = {
        "strength": int(core_stats.get("strength", 0)),
        "dexterity": int(core_stats.get("dexterity", 0)),
        "stamina": int(core_stats.get("stamina", 0)),
        "magic_affinity": int(core_stats.get("magic_affinity", 0)),
        "mana": int(core_stats.get("mana", 0)),
    }

    extras = {
        "luck_bonus": 0,
        "carry_capacity_bonus": 0,
        "derived_multipliers": {},
        "roll_modifiers": [],
    }

    core_multipliers = {
        "strength": 1.0,
        "dexterity": 1.0,
        "stamina": 1.0,
        "magic_affinity": 1.0,
        "mana": 1.0,
    }

    derived_multipliers: dict[str, float] = {}
    roll_modifiers: list[dict[str, Any]] = []

    for bundle in trait_bundles:
        trait = bundle.get("trait") or {}
        effects = _as_dict(trait.get("effects_json") or {})

        # Support BOTH:
        # old format -> effects["stat_multiplier"], effects["luck_bonus"], etc.
        # new format -> effects["passives"]["stat_multiplier"], etc.
        passives = _as_dict(effects.get("passives") or {})
        roll_mods = _as_list(effects.get("roll_modifiers") or [])

        stat_multiplier = _as_dict(
            passives.get("stat_multiplier")
            or effects.get("stat_multiplier")
            or {}
        )

        flat_bonus = _as_dict(passives.get("flat_bonus") or {})
        derived_multiplier = _as_dict(passives.get("derived_multiplier") or {})

        # old flat fields fallback
        if "luck_bonus" in effects and "luck" not in flat_bonus:
            flat_bonus["luck"] = effects.get("luck_bonus")

        if "carry_capacity_bonus" in effects and "carry_capacity" not in flat_bonus:
            flat_bonus["carry_capacity"] = effects.get("carry_capacity_bonus")

        # apply core stat multipliers
        for stat_key, mult in stat_multiplier.items():
            if stat_key in CORE_STAT_KEYS:
                try:
                    core_multipliers[stat_key] *= float(mult)
                except Exception:
                    pass

        # apply flat bonuses
        for bonus_key, bonus_value in flat_bonus.items():
            if bonus_key not in FLAT_BONUS_KEYS:
                continue

            try:
                bonus_int = int(bonus_value)
            except Exception:
                continue

            if bonus_key == "luck":
                extras["luck_bonus"] += bonus_int
            elif bonus_key == "carry_capacity":
                extras["carry_capacity_bonus"] += bonus_int

        # apply derived multipliers
        for derived_key, mult in derived_multiplier.items():
            if derived_key not in DERIVED_STAT_KEYS:
                continue

            try:
                parsed = float(mult)
            except Exception:
                continue

            derived_multipliers[derived_key] = (
                derived_multipliers.get(derived_key, 1.0) * parsed
            )

        # keep roll modifiers for future roll/check system
        for mod in roll_mods:
            if isinstance(mod, dict):
                roll_modifiers.append(mod)

    for stat_key, base_value in stats.items():
        stats[stat_key] = int(base_value * core_multipliers[stat_key])

    extras["derived_multipliers"] = derived_multipliers
    extras["roll_modifiers"] = roll_modifiers

    return {
        "core_stats": stats,
        "extras": extras,
    }