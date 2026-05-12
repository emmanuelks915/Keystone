# services/stats_service.py
from __future__ import annotations

from typing import Any


CORE_STAT_KEYS = {
    "strength",
    "dexterity",
    "stamina",
    "affinity",
    "mana",
}


STAT_ALIASES = {
    "str": "strength",
    "strength": "strength",

    "dex": "dexterity",
    "dexterity": "dexterity",

    "sta": "stamina",
    "stamina": "stamina",

    # current live DB key
    "aff": "affinity",
    "affinity": "affinity",

    # compatibility aliases so old code / docs still work
    "mag": "affinity",
    "magic": "affinity",
    "magic_affinity": "affinity",
    "magic affinity": "affinity",

    "mana": "mana",

    # allowed so legacy/derived rows don't crash lookups
    "luck": "luck",
    "fortitude": "fortitude",
}


DISPLAY_NAMES = {
    "strength": "Strength",
    "dexterity": "Dexterity",
    "stamina": "Stamina",
    "affinity": "Affinity",
    "mana": "Mana",
    "luck": "Luck",
    "fortitude": "Fortitude",
}


class StatsService:
    def __init__(self, supabase):
        self.sb = supabase

    # ---------------------------------------------------------------------
    # helpers
    # ---------------------------------------------------------------------
    def normalize_stat_key(self, raw: str) -> str:
        key = (raw or "").strip().lower().replace("-", "_")
        key = " ".join(key.split())
        if key in STAT_ALIASES:
            return STAT_ALIASES[key]
        raise ValueError(f"Unknown stat: {raw}")

    def _get_definition(self, stat_key: str) -> dict[str, Any]:
        key = self.normalize_stat_key(stat_key)

        # definition rows currently use the live DB keys
        definition_lookup_key = key

        res = (
            self.sb.table("stat_definitions")
            .select("stat_key,display_name,is_active,sort_order")
            .eq("stat_key", definition_lookup_key)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if rows:
            row = rows[0]
            return {
                "id": row["stat_key"],  # keep compatibility with stats.py
                "key": row["stat_key"],
                "display_name": row.get("display_name") or DISPLAY_NAMES.get(key, key.title()),
                "is_active": bool(row.get("is_active", True)),
                "sort_order": int(row.get("sort_order") or 0),
            }

        # fallback so bot doesn't die if definitions are incomplete
        return {
            "id": definition_lookup_key,
            "key": definition_lookup_key,
            "display_name": DISPLAY_NAMES.get(key, key.title()),
            "is_active": True,
            "sort_order": 0,
        }

    def _ensure_oc_stat_row(self, guild_id: int, character_id: str, stat_key: str) -> None:
        key = self.normalize_stat_key(stat_key)

        existing = (
            self.sb.table("oc_stats")
            .select("stat_key")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("stat_key", key)
            .limit(1)
            .execute()
        )
        rows = getattr(existing, "data", None) or []
        if rows:
            return

        self.sb.table("oc_stats").insert(
            {
                "guild_id": int(guild_id),
                "character_id": str(character_id),
                "stat_key": key,
                "stat_value": 0,
            }
        ).execute()

    def _get_stat_row(self, guild_id: int, character_id: str, stat_key: str) -> dict[str, Any]:
        key = self.normalize_stat_key(stat_key)
        self._ensure_oc_stat_row(guild_id, character_id, key)

        res = (
            self.sb.table("oc_stats")
            .select("guild_id,character_id,stat_key,stat_value,updated_at")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("stat_key", key)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            raise ValueError(f"Stat row not found for {key}")
        row = rows[0]
        return {
            "value": int(row.get("stat_value") or 0),
            "stat_key": row["stat_key"],
            "updated_at": row.get("updated_at"),
        }

    # ---------------------------------------------------------------------
    # reads
    # ---------------------------------------------------------------------
    def get_all_character_stats(
        self,
        *,
        guild_id: int,
        character_id: str,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        defs_res = (
            self.sb.table("stat_definitions")
            .select("stat_key,display_name,is_active,sort_order")
            .order("sort_order", desc=False)
            .order("display_name", desc=False)
            .execute()
        )
        defs_rows = getattr(defs_res, "data", None) or []

        by_key: dict[str, dict[str, Any]] = {}
        for row in defs_rows:
            key = str(row.get("stat_key") or "").strip()
            if not key:
                continue
            by_key[key] = {
                "id": key,  # compatibility with stats.py
                "key": key,
                "display_name": row.get("display_name") or DISPLAY_NAMES.get(key, key.title()),
                "is_active": bool(row.get("is_active", True)),
                "sort_order": int(row.get("sort_order") or 0),
            }

        # ensure current live core stats exist even if definitions are incomplete
        for key in CORE_STAT_KEYS:
            by_key.setdefault(
                key,
                {
                    "id": key,
                    "key": key,
                    "display_name": DISPLAY_NAMES.get(key, key.title()),
                    "is_active": True,
                    "sort_order": 0,
                },
            )

        stats_res = (
            self.sb.table("oc_stats")
            .select("stat_key,stat_value,updated_at")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .execute()
        )
        stat_rows = getattr(stats_res, "data", None) or []
        stat_map = {
            str(row.get("stat_key")): int(row.get("stat_value") or 0)
            for row in stat_rows
            if row.get("stat_key")
        }

        out: list[dict[str, Any]] = []
        for key, definition in by_key.items():
            if not include_hidden and not definition.get("is_active", True):
                continue

            out.append(
                {
                    "definition": definition,
                    "stat": {
                        "value": int(stat_map.get(key, 0)),
                    },
                }
            )

        out.sort(
            key=lambda r: (
                int(r["definition"].get("sort_order") or 0),
                str(r["definition"].get("display_name") or ""),
            )
        )
        return out

    # ---------------------------------------------------------------------
    # writes
    # ---------------------------------------------------------------------
    def add_stat(
        self,
        *,
        guild_id: int,
        character_id: str,
        stat_key: str,
        amount: int,
        actor_discord_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        key = self.normalize_stat_key(stat_key)
        definition = self._get_definition(key)
        current = self._get_stat_row(guild_id, character_id, key)
        old_value = int(current["value"])
        new_value = old_value + int(amount)

        if new_value < 0:
            raise ValueError("Stat cannot go below 0.")

        self.sb.table("oc_stats").update(
            {
                "stat_value": new_value,
            }
        ).eq("guild_id", int(guild_id)).eq("character_id", str(character_id)).eq("stat_key", key).execute()

        self.sb.table("oc_stat_changes").insert(
            {
                "guild_id": int(guild_id),
                "character_id": str(character_id),
                "stat_key": key,
                "old_value": old_value,
                "new_value": new_value,
                "delta": int(amount),
                "xp_cost": 0,
                "reason": reason,
                "actor_discord_id": int(actor_discord_id) if actor_discord_id is not None else None,
            }
        ).execute()

        return {
            "definition": definition,
            "old_value": old_value,
            "new_value": new_value,
            "delta": int(amount),
        }

    def set_stat(
        self,
        *,
        guild_id: int,
        character_id: str,
        stat_key: str,
        new_value: int,
        actor_discord_id: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        key = self.normalize_stat_key(stat_key)
        definition = self._get_definition(key)
        current = self._get_stat_row(guild_id, character_id, key)
        old_value = int(current["value"])
        final_value = int(new_value)

        if final_value < 0:
            raise ValueError("Stat cannot go below 0.")

        delta = final_value - old_value

        self.sb.table("oc_stats").update(
            {
                "stat_value": final_value,
            }
        ).eq("guild_id", int(guild_id)).eq("character_id", str(character_id)).eq("stat_key", key).execute()

        self.sb.table("oc_stat_changes").insert(
            {
                "guild_id": int(guild_id),
                "character_id": str(character_id),
                "stat_key": key,
                "old_value": old_value,
                "new_value": final_value,
                "delta": delta,
                "xp_cost": 0,
                "reason": reason,
                "actor_discord_id": int(actor_discord_id) if actor_discord_id is not None else None,
            }
        ).execute()

        return {
            "definition": definition,
            "old_value": old_value,
            "new_value": final_value,
            "delta": delta,
        }