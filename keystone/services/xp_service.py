# services/xp_service.py
from __future__ import annotations

from typing import Any, Optional


class XPServiceError(Exception):
    """Base XP service error."""


class XPNotFoundError(XPServiceError):
    """Requested XP object was not found."""


class XPValidationError(XPServiceError):
    """Invalid user input or invalid progression request."""


class XPInsufficientError(XPServiceError):
    """Not enough XP to complete the request."""


class XPDuplicateAwardError(XPServiceError):
    """Duplicate XP award blocked by DB uniqueness rules."""


class XPService:
    def __init__(self, supabase):
        self.sb = supabase

    # -------------------------------------------------------------------------
    # Basic wallet helpers
    # -------------------------------------------------------------------------
    def ensure_wallet(self, guild_id: int, character_id: str) -> None:
        self.sb.rpc(
            "ensure_oc_xp_wallet",
            {
                "p_guild_id": int(guild_id),
                "p_character_id": str(character_id),
            },
        ).execute()

    def get_wallet(self, guild_id: int, character_id: str) -> dict[str, Any]:
        self.ensure_wallet(guild_id, character_id)

        res = (
            self.sb.table("oc_xp_wallets")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            raise XPNotFoundError("XP wallet not found.")
        return rows[0]

    def get_balance_summary(self, guild_id: int, character_id: str) -> dict[str, int]:
        wallet = self.get_wallet(guild_id, character_id)
        return {
            "available_xp": int(wallet.get("available_xp") or 0),
            "total_earned_xp": int(wallet.get("total_earned_xp") or 0),
            "total_spent_xp": int(wallet.get("total_spent_xp") or 0),
        }

    # -------------------------------------------------------------------------
    # History / audit
    # -------------------------------------------------------------------------
    def get_history(self, guild_id: int, character_id: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(50, int(limit)))

        res = (
            self.sb.table("oc_xp_transactions")
            .select("xp_tx_id,direction,amount,source,reference_type,reference_key,reason,actor_discord_id,created_at")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(res, "data", None) or []

    def get_awards(self, guild_id: int, character_id: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(50, int(limit)))

        res = (
            self.sb.table("oc_xp_awards")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .order("awarded_at", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(res, "data", None) or []

    # -------------------------------------------------------------------------
    # Character/stat helpers
    # -------------------------------------------------------------------------
    def get_character_name(self, guild_id: int, character_id: str) -> str:
        res = (
            self.sb.table("characters")
            .select("name")
            .eq("character_id", str(character_id))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return "Unknown OC"
        return str(rows[0].get("name") or "Unknown OC")

    def get_stat_value(self, guild_id: int, character_id: str, stat_key: str) -> int:
        res = (
            self.sb.table("oc_stats")
            .select("stat_value")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("stat_key", str(stat_key))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            return 0
        return int(rows[0].get("stat_value") or 0)

    def get_last_stat_change(self, guild_id: int, character_id: str, stat_key: str) -> Optional[dict[str, Any]]:
        res = (
            self.sb.table("oc_stat_changes")
            .select("*")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("stat_key", str(stat_key))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None

    # -------------------------------------------------------------------------
    # Award / spend
    # -------------------------------------------------------------------------
    def award_xp(
        self,
        *,
        guild_id: int,
        character_id: str,
        amount: int,
        source: str,
        title: str,
        actor_discord_id: int | None = None,
        external_ref: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise XPValidationError("XP amount must be greater than 0.")
        if not source.strip():
            raise XPValidationError("XP source cannot be blank.")
        if not title.strip():
            raise XPValidationError("XP title cannot be blank.")

        try:
            rpc = self.sb.rpc(
                "award_xp",
                {
                    "p_guild_id": int(guild_id),
                    "p_character_id": str(character_id),
                    "p_amount": int(amount),
                    "p_source": str(source).strip(),
                    "p_title": str(title).strip(),
                    "p_actor_discord_id": int(actor_discord_id) if actor_discord_id is not None else None,
                    "p_external_ref": str(external_ref).strip() if external_ref else None,
                    "p_notes": str(notes).strip() if notes else None,
                },
            ).execute()
        except Exception as e:
            self._raise_mapped_error(e)

        wallet = self.get_wallet(guild_id, character_id)
        return {
            "xp_tx_id": getattr(rpc, "data", None),
            "wallet": wallet,
        }

    def spend_xp(
        self,
        *,
        guild_id: int,
        character_id: str,
        amount: int,
        source: str,
        reference_type: str | None = None,
        reference_key: str | None = None,
        reason: str | None = None,
        actor_discord_id: int | None = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise XPValidationError("XP amount must be greater than 0.")
        if not source.strip():
            raise XPValidationError("XP source cannot be blank.")

        try:
            rpc = self.sb.rpc(
                "spend_xp",
                {
                    "p_guild_id": int(guild_id),
                    "p_character_id": str(character_id),
                    "p_amount": int(amount),
                    "p_source": str(source).strip(),
                    "p_reference_type": str(reference_type).strip() if reference_type else None,
                    "p_reference_key": str(reference_key).strip() if reference_key else None,
                    "p_reason": str(reason).strip() if reason else None,
                    "p_actor_discord_id": int(actor_discord_id) if actor_discord_id is not None else None,
                },
            ).execute()
        except Exception as e:
            self._raise_mapped_error(e)

        wallet = self.get_wallet(guild_id, character_id)
        return {
            "xp_tx_id": getattr(rpc, "data", None),
            "wallet": wallet,
        }

    # -------------------------------------------------------------------------
    # Stat purchases
    # -------------------------------------------------------------------------
    def buy_stat_points(
        self,
        *,
        guild_id: int,
        character_id: str,
        stat_key: str,
        points: int = 1,
        actor_discord_id: int | None = None,
    ) -> dict[str, Any]:
        if not stat_key.strip():
            raise XPValidationError("Stat key cannot be blank.")
        if points <= 0:
            raise XPValidationError("Points must be greater than 0.")
        if points > 100:
            raise XPValidationError("Too many points at once.")

        old_value = self.get_stat_value(guild_id, character_id, stat_key)

        try:
            self.sb.rpc(
                "buy_stat_point",
                {
                    "p_guild_id": int(guild_id),
                    "p_character_id": str(character_id),
                    "p_stat_key": str(stat_key).strip(),
                    "p_points": int(points),
                    "p_actor_discord_id": int(actor_discord_id) if actor_discord_id is not None else None,
                },
            ).execute()
        except Exception as e:
            self._raise_mapped_error(e)

        new_value = self.get_stat_value(guild_id, character_id, stat_key)
        wallet = self.get_wallet(guild_id, character_id)
        last_change = self.get_last_stat_change(guild_id, character_id, stat_key)

        return {
            "stat_key": stat_key,
            "old_value": old_value,
            "new_value": new_value,
            "points_bought": int(points),
            "xp_cost": int(last_change.get("xp_cost") or 0) if last_change else None,
            "wallet": wallet,
            "stat_change": last_change,
        }

    # -------------------------------------------------------------------------
    # Read helpers for future skill/trait work
    # -------------------------------------------------------------------------
    def has_skill(self, guild_id: int, character_id: str, skill_key: str) -> bool:
        res = (
            self.sb.table("oc_skills")
            .select("skill_key")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("skill_key", str(skill_key))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return bool(rows)

    def has_trait(self, guild_id: int, character_id: str, trait_key: str) -> bool:
        res = (
            self.sb.table("oc_traits")
            .select("trait_key")
            .eq("guild_id", int(guild_id))
            .eq("character_id", str(character_id))
            .eq("trait_key", str(trait_key))
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        return bool(rows)

    # -------------------------------------------------------------------------
    # Error mapping
    # -------------------------------------------------------------------------
    def _raise_mapped_error(self, error: Exception) -> None:
        msg = str(error).lower()

        if "duplicate key" in msg or "uq_oc_xp_awards_dedupe" in msg:
            raise XPDuplicateAwardError("Duplicate XP award blocked.") from error

        if "not enough xp" in msg or "not enough xp (" in msg or "required" in msg:
            raise XPInsufficientError("Not enough XP.") from error

        if "no xp cost band found" in msg:
            raise XPValidationError("No XP cost band found for that stat range.") from error

        if "xp amount must be positive" in msg or "points must be positive" in msg:
            raise XPValidationError("Amount must be positive.") from error

        raise XPServiceError(str(error)) from error