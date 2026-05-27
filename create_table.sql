-- Run this in Supabase Dashboard → SQL Editor
-- https://supabase.com/dashboard/project/ggpcmpniswzruomjcjnu/sql

CREATE TABLE IF NOT EXISTS public.subreddits (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    category    TEXT,
    subscribers BIGINT DEFAULT 0,
    added_date  DATE DEFAULT CURRENT_DATE,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Allow read/write with the anon (publishable) key
ALTER TABLE public.subreddits ENABLE ROW LEVEL SECURITY;

CREATE POLICY allow_all ON public.subreddits
    FOR ALL TO anon
    USING (true)
    WITH CHECK (true);

-- ─── weekly_insights ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.weekly_insights (
    id          BIGSERIAL PRIMARY KEY,
    week_start  DATE NOT NULL,
    week_end    DATE NOT NULL,
    raw_data    JSONB,
    analysis    TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.weekly_insights ENABLE ROW LEVEL SECURITY;

CREATE POLICY allow_all ON public.weekly_insights
    FOR ALL TO anon
    USING (true)
    WITH CHECK (true);

-- ─── competitor_insights ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.competitor_insights (
    id                  BIGSERIAL PRIMARY KEY,
    week_start          DATE NOT NULL,
    competitor_username TEXT NOT NULL,
    new_subreddits      JSONB DEFAULT '[]',
    top_subreddits      JSONB DEFAULT '[]',
    content_ideas       JSONB DEFAULT '[]',
    priority_subs       JSONB DEFAULT '[]',
    raw_analysis        TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.competitor_insights ENABLE ROW LEVEL SECURITY;

CREATE POLICY allow_all ON public.competitor_insights
    FOR ALL TO anon
    USING (true)
    WITH CHECK (true);
