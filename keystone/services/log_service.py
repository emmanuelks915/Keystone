from services.db import get_supabase_client

def log_tx(
    actor_discord_id: int,
    oc_id: str,
    tx_type: str,
    ap_delta: int = 0,
    item_id: str | None = None,
    skill_id: str | None = None,
    quantity: int | None = None,
    notes: str | None = None,
) -> None:
    sb = get_supabase_client()
    payload = {
        "actor_discord_id": str(actor_discord_id),
        "oc_id": oc_id,
        "tx_type": tx_type,
        "ap_delta": int(ap_delta),
        "item_id": item_id,
        "skill_id": skill_id,
        "quantity": quantity,
        "notes": notes,
    }
    sb.table("transactions_log").insert(payload).execute()
