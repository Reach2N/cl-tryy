import os
import random
import socket
import string
import sys
import time
from pathlib import Path

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# pyrefly: ignore [missing-import]
from curl_cffi import requests

try:
    from supabase import Client, create_client
except ImportError:
    print("Error: 'supabase' package is required. Install it using: pip install supabase")
    sys.exit(1)

# --- Configuration ---
DEFAULT_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtzaHFhdndzdnN5cmNpdWhhdXFvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODkzMDMwMTAsImV4cCI6MjEwNDg3OTAxMH0.nVBB-bqG08c0xCg_HiBjw3zCVEHqCFqPQVEOT0JjLbU"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://kshqavwsvsyrciuhauqo.supabase.co").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", DEFAULT_KEY).strip()
BASE_URL = os.environ.get("BASE_URL", "https://claude.ai/referral").strip()

configured_worker = os.environ.get("WORKER_ID", "").strip()
hostname = socket.gethostname().split(".")[0]
# Each machine gets a unique worker identifier (e.g. ubuntu-3a9f)
if configured_worker and configured_worker != "1":
    WORKER_ID = configured_worker
else:
    rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    WORKER_ID = f"{hostname}-{rand_suffix}"

REQUEST_INTERVAL = float(os.environ.get("REQUEST_INTERVAL", "1.0"))

CHARACTERS = string.ascii_letters + string.digits + "-_"
INVALID_PHRASES = [
    "This referral link is no longer valid",
    "referral link is no longer valid",
    "Page not found",
    "404",
]


def validate_supabase_setup():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("=" * 65)
        print("❌ Supabase configuration missing!")
        print("Please set SUPABASE_URL and SUPABASE_KEY in your .env file or environment.")
        print("Example:")
        print("  SUPABASE_URL=https://your-project.supabase.co")
        print("  SUPABASE_KEY=eyJhbGciOi...")
        print("=" * 65)
        sys.exit(1)

    client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        # Test if referral_checks table exists
        client.table("referral_checks").select("slug").limit(1).execute()
        print("✅ Connected to Supabase successfully.")
    except Exception as exc:
        print("=" * 65)
        print("❌ Error connecting to Supabase or tables not found:")
        print(f"   {exc}")
        print("\nPlease execute the SQL script 'supabase_schema.sql' in your")
        print("Supabase SQL Editor before starting the worker.")
        print("=" * 65)
        sys.exit(1)

    return client


def generate_slug(length=None):
    # All verified Claude codes are canonical unpadded Base64url encodings of 7 bytes
    # (ends strictly in A, Q, g, or w, eliminating 93.75% of impossible codes)
    return secrets.token_urlsafe(7)


def is_already_checked(client: Client, slug: str) -> bool:
    """Check Supabase to skip codes checked by this or any other server."""
    try:
        res = client.table("referral_checks").select("slug").eq("slug", slug).limit(1).execute()
        return len(res.data) > 0
    except Exception as e:
        print(f"[{WORKER_ID}] Database check error: {e}")
        return False


def save_check_result(client: Client, slug: str, url: str, status_code: int, is_valid: bool):
    """Record result in Supabase with conflict handling."""
    try:
        client.table("referral_checks").upsert(
            {
                "slug": slug,
                "url": url,
                "status_code": status_code,
                "is_valid": is_valid,
                "worker_id": WORKER_ID,
            },
            on_conflict="slug",
        ).execute()

        if is_valid:
            client.table("found_codes").insert(
                {"slug": slug, "url": url, "worker_id": WORKER_ID},
                returning="minimal",
            ).execute()
    except Exception as e:
        print(f"[{WORKER_ID}] Failed to save to Supabase: {e}")


def run_worker():
    client = validate_supabase_setup()

    print(f"\n🚀 Worker ID      : {WORKER_ID}")
    print(f"🎯 Target URL base: {BASE_URL}")
    print(f"⏱️  Pace interval  : {REQUEST_INTERVAL}s")
    print("📡 Connected to shared Supabase database. Press Ctrl + C to stop.\n")

    # Use Chrome TLS impersonation on HTTPS; plain HTTP on localhost
    impersonate = "chrome" if BASE_URL.startswith("https://") else None
    session = requests.Session(impersonate=impersonate)
    attempts = 0

    while True:
        attempts += 1
        slug = generate_slug()
        url = f"{BASE_URL.rstrip('/')}/{slug}"
        # Use Claude's direct lightweight JSON API (4 bytes vs 113,000 bytes of HTML)
        check_url = f"https://claude.ai/api/referral/code/{slug}" if "claude.ai" in BASE_URL else url

        try:
            response = session.get(check_url, timeout=10, allow_redirects=True)

            if response.status_code == 429:
                print(f"[{WORKER_ID} #{attempts}] Rate limited (HTTP 429). Retrying in 15s...")
                time.sleep(15)
                continue

            if response.status_code == 403:
                print(f"[{WORKER_ID} #{attempts}] Blocked (403): Rate limits or security challenge.")
                save_check_result(client, slug, url, 403, False)

            elif response.status_code == 200:
                is_valid = False

                if "claude.ai" in BASE_URL:
                    # Parse JSON API response
                    try:
                        data = response.json()
                    except Exception:
                        data = None

                    # If data is null -> code does not exist. If is_valid == True -> jackpot!
                    if isinstance(data, dict) and data.get("is_valid") is True:
                        is_valid = True
                else:
                    # Fallback HTML checking for local mock server
                    body_text = response.text
                    is_invalid = any(phrase in body_text for phrase in INVALID_PHRASES)
                    is_valid = not is_invalid

                if is_valid:
                    print(f"\n🎉 [{WORKER_ID}] WORKING LINK FOUND on attempt #{attempts}: {url}")
                    save_check_result(client, slug, url, 200, True)
                    with open("found_code.txt", "a", encoding="utf-8") as f:
                        f.write(f"{url}\n")
                    break
                else:
                    print(f"[{WORKER_ID} #{attempts}] Invalid code: {slug}")
                    save_check_result(client, slug, url, 200, False)

            else:
                print(f"[{WORKER_ID} #{attempts}] Status: {response.status_code} ({slug})")
                save_check_result(client, slug, url, response.status_code, False)

        except Exception as e:
            print(f"[{WORKER_ID} #{attempts}] Network error: {e}")

        time.sleep(REQUEST_INTERVAL)


if __name__ == "__main__":
    try:
        run_worker()
    except KeyboardInterrupt:
        print(f"\nStopped by user (Worker: {WORKER_ID}).")
