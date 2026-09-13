-- ==============================================================================
-- Supabase Schema for Distributed Referral Checker
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

-- Index for quickly filtering valid codes and checking dates
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

-- 3. Row Level Security (RLS) Policies
-- Enables access for both 'anon' key and 'service_role' key
ALTER TABLE public.referral_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.found_codes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow all access to referral_checks" ON public.referral_checks;
CREATE POLICY "Allow all access to referral_checks" 
    ON public.referral_checks 
    FOR ALL 
    TO anon, authenticated, service_role 
    USING (true) 
    WITH CHECK (true);

DROP POLICY IF EXISTS "Allow all access to found_codes" ON public.found_codes;
CREATE POLICY "Allow all access to found_codes" 
    ON public.found_codes 
    FOR ALL 
    TO anon, authenticated, service_role 
    USING (true) 
    WITH CHECK (true);

-- 4. Enable Realtime on found_codes (optional, allows live dashboard alerts)
ALTER PUBLICATION supabase_realtime ADD TABLE public.found_codes;
