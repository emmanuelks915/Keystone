-- Journal XP: remember which messages were already paid
-- Run this once in Supabase SQL editor before deploying the updated journal_tracker.py.

create table if not exists public.journal_xp_claimed_messages (
    id uuid primary key default gen_random_uuid(),
    guild_id bigint not null,
    journal_thread_id bigint not null,
    message_id bigint not null,
    claim_id uuid not null references public.rp_xp_claims(claim_id) on delete cascade,
    character_id uuid not null references public.characters(character_id) on delete cascade,
    user_id bigint not null,
    word_count integer not null default 0,
    claimed_by bigint,
    claimed_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    unique (guild_id, journal_thread_id, character_id, message_id)
);

create index if not exists idx_journal_xp_claimed_messages_thread_character
    on public.journal_xp_claimed_messages (guild_id, journal_thread_id, character_id);

create index if not exists idx_journal_xp_claimed_messages_claim
    on public.journal_xp_claimed_messages (claim_id);
