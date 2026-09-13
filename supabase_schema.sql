-- ==============================================================================
-- Supabase Schema: Private Found Codes & Secure Worker Policies
-- Run this in your Supabase SQL Editor (Dashboard -> SQL Editor -> New query)
-- ==============================================================================

-- 1. Table tracking all checked codes across all worker servers
CREATE TABLE IF NOT EXISTS public.referral_checks (
    slug TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INTEGER,
    is_valid BOOLEAN DEFAULT FALSE,
    worker_id TEXT NOT NULL,
    checked_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_checks_is_valid ON public.referral_checks(is_valid);
CREATE INDEX IF NOT EXISTS idx_referral_checks_checked_at ON public.referral_checks(checked_at DESC);

-- 2. Dedicated table for valid / working referral codes
CREATE TABLE IF NOT EXISTS public.found_codes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    found_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Enable Row Level Security (RLS)
ALTER TABLE public.referral_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.found_codes ENABLE ROW LEVEL SECURITY;

-- Clean up existing policies
DROP POLICY IF EXISTS "Allow all access to referral_checks" ON public.referral_checks;
DROP POLICY IF EXISTS "Allow all access to found_codes" ON public.found_codes;
DROP POLICY IF EXISTS "Workers can insert found_codes" ON public.found_codes;
DROP POLICY IF EXISTS "Only admin can view found_codes" ON public.found_codes;
DROP POLICY IF EXISTS "Workers can manage referral_checks" ON public.referral_checks;
DROP POLICY IF EXISTS "Anon cannot see valid codes in referral_checks" ON public.referral_checks;

-- ==============================================================================
-- SECURE POLICIES:
-- 1. Found codes are 100% PRIVATE.
--    - Workers (anon) can only INSERT into found_codes (blind drop box).
--    - Workers or anyone with the anon key CANNOT SELECT (read) found_codes.
--    - ONLY YOU in the Supabase Dashboard (service_role) can view found_codes.
-- ==============================================================================

CREATE POLICY "Workers can insert found_codes" 
    ON public.found_codes 
    FOR INSERT 
    TO anon, authenticated 
    WITH CHECK (true);

CREATE POLICY "Only admin can view found_codes" 
    ON public.found_codes 
    FOR SELECT 
    TO service_role 
    USING (true);

-- 2. Referral checks:
--    - Workers can insert and update referral checks.
--    - When reading, anon can ONLY see invalid codes (is_valid = false),
--      so even querying referral_checks will NEVER reveal working codes to the public!
CREATE POLICY "Workers can insert and update checks"
    ON public.referral_checks
    FOR ALL
    TO anon, authenticated, service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Anon cannot see valid codes in referral_checks"
    ON public.referral_checks
    FOR SELECT
    TO anon
    USING (is_valid = false);

-- 3. Candidate Queue (Optional):
--    Drop any scraped or suspected codes here; workers will automatically
--    prioritize checking these before generating random codes!
CREATE TABLE IF NOT EXISTS public.candidate_queue (
    slug TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    claimed_by TEXT
);

ALTER TABLE public.candidate_queue ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Allow all access to candidate_queue" ON public.candidate_queue;
CREATE POLICY "Allow all access to candidate_queue" 
    ON public.candidate_queue 
    FOR ALL 
    TO anon, authenticated, service_role 
    USING (true) 
    WITH CHECK (true);
