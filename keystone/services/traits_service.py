from __future__ import annotations

from typing import Any


class TraitsService:
    def __init__(self, supabase):
        self.sb = supabase

    @staticmethod
    def normalize_slug(value: str) -> str:
        return (
            value.strip()
            .lower()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("'", "")
        )

    def get_trait_by_slug(self, guild_id: int, slug: str) -> dict | None:
        slug = self.normalize_slug(slug)
        res = (
            self.sb.table("traits")
            .select("*")
            .eq("guild_id", guild_id)
            .eq("slug", slug)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    def get_character_traits(
        self,
        *,
        guild_id: int,
        character_id: str,
    ) -> list[dict[str, Any]]:
        links_res = (
            self.sb.table("character_traits")
            .select("trait_id, approved_by, notes, created_at")
            .eq("guild_id", guild_id)
            .eq("character_id", character_id)
            .execute()
        )
        links = links_res.data or []
        if not links:
            return []

        trait_ids = [str(row["trait_id"]) for row in links]
        traits_res = (
            self.sb.table("traits")
            .select("*")
            .eq("guild_id", guild_id)
            .in_("trait_id", trait_ids)
            .execute()
        )
        traits = traits_res.data or []
        by_id = {str(t["trait_id"]): t for t in traits}

        out: list[dict[str, Any]] = []
        for link in links:
            trait = by_id.get(str(link["trait_id"]))
            if not trait:
                continue
            out.append({
                "trait": trait,
                "link": link,
            })

        return out

    def add_trait_to_character(
        self,
        *,
        guild_id: int,
        character_id: str,
        trait_id: str,
        approved_by: int | None = None,
        notes: str | None = None,
    ) -> dict:
        row = {
            "guild_id": guild_id,
            "character_id": character_id,
            "trait_id": trait_id,
            "approved_by": approved_by,
            "notes": notes,
        }
        res = self.sb.table("character_traits").insert(row).execute()
        return res.data[0]

    def remove_trait_from_character(
        self,
        *,
        guild_id: int,
        character_id: str,
        trait_id: str,
    ) -> None:
        (
            self.sb.table("character_traits")
            .delete()
            .eq("guild_id", guild_id)
            .eq("character_id", character_id)
            .eq("trait_id", trait_id)
            .execute()
        )