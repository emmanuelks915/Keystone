from services.db import get_supabase_client

def has_skill(oc_id: str, skill_id: str) -> bool:
    sb = get_supabase_client()
    res = (
        sb.table("oc_skills")
        .select("oc_id")
        .eq("oc_id", oc_id)
        .eq("skill_id", skill_id)
        .single()
        .execute()
    )
    return getattr(res, "data", None) is not None

def grant_skill(oc_id: str, skill_id: str) -> None:
    sb = get_supabase_client()
    sb.table("oc_skills").insert({"oc_id": oc_id, "skill_id": skill_id}).execute()

def revoke_skill(oc_id: str, skill_id: str) -> None:
    sb = get_supabase_client()
    sb.table("oc_skills").delete().eq("oc_id", oc_id).eq("skill_id", skill_id).execute()
