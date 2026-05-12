import math


def calculate_derived_stats(core):
    STR = core.get("strength", 0)
    DEX = core.get("dexterity", 0)
    STA = core.get("stamina", 0)
    AFF = core.get("magic_affinity", 0)
    MAN = core.get("mana", 0)

    reaction_score = math.floor(DEX * 1.5)

    fortitude = math.floor(STA * 1.25)

    safe_output = math.floor(fortitude * 1.15)

    magic_safe_output = math.floor(
        (fortitude * 0.6) + (MAN * 0.8)
    )

    ap = max(1, math.floor(fortitude / 150))

    cc = 4 + math.floor(STR / 150)

    return {
        "reaction_score": reaction_score,
        "fortitude": fortitude,
        "safe_output": safe_output,
        "magic_safe_output": magic_safe_output,
        "ap": ap,
        "carry_capacity": cc,
    }