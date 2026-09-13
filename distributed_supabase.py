import os
import random
import secrets
import socket
import string
import sys
import threading
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
# Each machine gets a unique worker identifier (e.g. macbook-3a9f)
if configured_worker and configured_worker != "1":
    WORKER_ID = configured_worker
else:
    rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    WORKER_ID = f"{hostname}-{rand_suffix}"

REQUEST_INTERVAL = float(os.environ.get("REQUEST_INTERVAL", "2.5"))
# Per-proxy interval (2.5s pace)
PROXY_INTERVAL = float(os.environ.get("PROXY_INTERVAL", "2.5"))

CHARACTERS = string.ascii_letters + string.digits + "-_"
INVALID_PHRASES = [
    "This referral link is no longer valid",
    "referral link is no longer valid",
    "Page not found",
    "404",
]

# Optional proxy pool configuration (proxies.txt or PROXY_URL)
PROXIES_FILE = Path("proxies.txt")
PROXIES = []

def format_proxy(p: str) -> str:
    p = p.strip()
    if p.startswith("http://") or p.startswith("https://") or p.startswith("socks"):
        return p
    parts = p.split(":")
    if len(parts) == 4:  # ip:port:user:pass
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2:  # ip:port
        return f"http://{p}"
    return p

if PROXIES_FILE.exists():
    raw_lines = PROXIES_FILE.read_text(encoding="utf-8").splitlines()
    PROXIES = [format_proxy(p) for p in raw_lines if p.strip() and not p.startswith("#") and not p.startswith("{")]
    
    if PROXIES:
        # Randomly shuffle so if multiple servers download the same huge list, they use a different subset of 10!
        random.shuffle(PROXIES)
    else:
        print("⚠️ Warning: proxies.txt is empty or contains an error. Running in Direct IP mode.")
elif os.environ.get("PROXY_URL"):
    PROXIES = [format_proxy(os.environ.get("PROXY_URL").strip())]

# State variables
HAS_CANDIDATE_QUEUE = False
total_attempts = 0
total_lock = threading.Lock()
stop_event = threading.Event()


def validate_supabase_setup():
    global HAS_CANDIDATE_QUEUE
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("=" * 65)
        print("❌ Supabase configuration missing!")
        print("Please set SUPABASE_URL and SUPABASE_KEY in your .env file or environment.")
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

    # Check candidate queue existence ONCE at startup to avoid hot-loop DB latency
    try:
        client.table("candidate_queue").select("slug").limit(1).execute()
        HAS_CANDIDATE_QUEUE = True
    except Exception:
        HAS_CANDIDATE_QUEUE = False

    return client


def generate_slug():
    # All verified Claude codes are canonical unpadded Base64url encodings of 7 bytes
    # (ends strictly in A, Q, g, or w, eliminating 93.75% of impossible codes)
    return secrets.token_urlsafe(7)


def get_next_slug(client: Client) -> str:
    """Prioritizes candidates from Supabase candidate_queue if table exists, otherwise instant in-memory generator."""
    if HAS_CANDIDATE_QUEUE:
        try:
            res = client.table("candidate_queue").select("slug").is_("claimed_by", "null").limit(1).execute()
            if res.data:
                cand = res.data[0]["slug"].strip()
                client.table("candidate_queue").update({"claimed_by": WORKER_ID}).eq("slug", cand).execute()
                return cand
        except Exception:
            pass
    return generate_slug()


def save_valid_result(client: Client, slug: str, url: str):
    """Immediately record working code in Supabase."""
    try:
        client.table("referral_checks").upsert(
            {
                "slug": slug,
                "url": url,
                "status_code": 200,
                "is_valid": True,
                "worker_id": WORKER_ID,
            },
            on_conflict="slug",
        ).execute()

        client.table("found_codes").insert(
            {"slug": slug, "url": url, "worker_id": WORKER_ID},
            returning="minimal",
        ).execute()
    except Exception as e:
        print(f"[{WORKER_ID}] Failed to save jackpot to Supabase: {e}")


def worker_thread(thread_idx: int, proxy: str | None, client: Client, initial_pace: float):
    global total_attempts
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    impersonate = "chrome" if BASE_URL.startswith("https://") else None
    # Each thread keeps a persistent warm session with its own proxy
    session = requests.Session(impersonate=impersonate, proxies=proxy_dict)
    px_tag = f"Px-{thread_idx + 1}" if proxy else "Direct"

    pace = initial_pace
    consecutive_success = 0

    while not stop_event.is_set():
        slug = get_next_slug(client)
        url = f"{BASE_URL.rstrip('/')}/{slug}"
        check_url = f"https://claude.ai/api/referral/code/{slug}" if "claude.ai" in BASE_URL else url

        with total_lock:
            total_attempts += 1
            att = total_attempts

        try:
            response = session.get(check_url, timeout=10, allow_redirects=True)

            if response.status_code == 429:
                retry_header = response.headers.get("Retry-After")
                try:
                    retry_wait = max(float(retry_header), 25.0) if retry_header else 30.0
                except (ValueError, TypeError):
                    retry_wait = 30.0
                print(f"[{WORKER_ID} #{att} | {px_tag}] Rate limited (HTTP 429). Proxy cooling down {retry_wait:.0f}s...")
                time.sleep(retry_wait)
                # Adaptive backoff: auto-tune pace to be slightly slower for this specific proxy
                pace = min(pace + 0.4, 7.0)
                consecutive_success = 0
                continue

            if response.status_code == 403:
                print(f"[{WORKER_ID} #{att} | {px_tag}] Blocked (403): Cloudflare / IP restriction.")
                time.sleep(10.0)
                continue

            elif response.status_code == 200:
                is_valid = False
                if "claude.ai" in BASE_URL:
                    try:
                        data = response.json()
                    except Exception:
                        data = None
                    if isinstance(data, dict) and data.get("is_valid") is True:
                        is_valid = True
                else:
                    body_text = response.text
                    is_valid = not any(phrase in body_text for phrase in INVALID_PHRASES)

                if is_valid:
                    print("\n" + "=" * 65)
                    print(f"🎉 [{WORKER_ID}] WORKING LINK FOUND on attempt #{att}!")
                    print(f"🔗 {url}")
                    print("=" * 65 + "\n")
                    save_valid_result(client, slug, url)
                    with open("found_code.txt", "a", encoding="utf-8") as f:
                        f.write(f"{url}\n")
                    stop_event.set()
                    break
                else:
                    print(f"[{WORKER_ID} #{att} | {px_tag}] Invalid code: {slug}")

            else:
                print(f"[{WORKER_ID} #{att} | {px_tag}] Status: {response.status_code} ({slug})")

        except Exception as e:
            print(f"[{WORKER_ID} #{att} | {px_tag}] Network error: {e}")

        # Pace this individual thread/proxy
        time.sleep(pace)


def run_worker():
    client = validate_supabase_setup()

    has_proxies = len(PROXIES) > 0
    num_threads = min(len(PROXIES), int(os.environ.get("CONCURRENCY", "200"))) if has_proxies else 1
    pace = PROXY_INTERVAL if has_proxies else REQUEST_INTERVAL

    print(f"\n🚀 Worker ID       : {WORKER_ID}")
    print(f"🎯 Target URL base : {BASE_URL}")
    print(f"⚡ Concurrency     : {num_threads} parallel thread(s)")
    print(f"⏱️  Pace per worker : {pace}s")
    if has_proxies:
        est_rpm = (num_threads / pace) * 60
        print(f"🛡️  Proxy pool      : {len(PROXIES)} proxies loaded (~{est_rpm:.0f} checks/min aggregate throughput)")
    else:
        print(f"🛡️  Direct IP mode  : Single thread, paced at {pace}s (~27 checks/min)")
    print("📡 Connected to shared Supabase database. Press Ctrl + C to stop.\n")

    threads = []
    for i in range(num_threads):
        proxy = PROXIES[i % len(PROXIES)] if has_proxies else None
        t = threading.Thread(target=worker_thread, args=(i, proxy, client, pace), daemon=True)
        t.start()
        threads.append(t)

    start_time = time.time()
    last_attempts = 0
    last_time = start_time

    try:
        while not stop_event.is_set():
            time.sleep(10.0)
            now = time.time()
            with total_lock:
                current_att = total_attempts
            delta_att = current_att - last_attempts
            delta_t = now - last_time
            speed_sec = delta_att / delta_t if delta_t > 0 else 0
            speed_min = speed_sec * 60
            elapsed_min = (now - start_time) / 60
            print(f"📊 [{WORKER_ID} Stats] Total: {current_att:,} checks | Speed: {speed_sec:.1f}/s ({speed_min:.0f}/min) | Elapsed: {elapsed_min:.1f}m")
            last_attempts = current_att
            last_time = now
    except KeyboardInterrupt:
        print(f"\nStopping worker ({WORKER_ID})...")
        stop_event.set()

    for t in threads:
        t.join(timeout=1.0)
    print(f"Worker {WORKER_ID} stopped.")


if __name__ == "__main__":
    run_worker()
