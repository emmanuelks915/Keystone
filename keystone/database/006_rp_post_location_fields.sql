-- 006_rp_post_location_fields.sql
-- Long-term RP location storage for Citizen Registry / Last Seen.
-- Run this in Supabase SQL Editor before deploying the bot patch.

alter table if exists public.rp_posts
  add column if not exists channel_id bigint,
  add column if not exists channel_name text,
  add column if not exists thread_name text,
  add column if not exists parent_channel_id bigint,
  add column if not exists parent_channel_name text,
  add column if not exists location_name text,
  add column if not exists jump_url text;

create index if not exists idx_rp_posts_channel_id on public.rp_posts(channel_id);
create index if not exists idx_rp_posts_thread_id on public.rp_posts(thread_id);
create index if not exists idx_rp_posts_character_id on public.rp_posts(character_id);
create index if not exists idx_rp_posts_posted_at on public.rp_posts(posted_at);
