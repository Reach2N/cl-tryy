import os
import random
import string
import time

# pyrefly: ignore [missing-import]
from curl_cffi import requests

CHARACTERS = string.ascii_letters + string.digits + "-_"
INVALID_PHRASES = [
    "This referral link is no longer valid",
    "referral link is no longer valid",
    "Page not found",
    "404",
]

BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000/referral")


def generate_slug(length=10):
    return "".join(random.choice(CHARACTERS) for _ in range(length))


def run_continuous_search(base_url=BASE_URL):
    print(f"Starting deep referral check against {base_url}... Press Ctrl + C to stop.\n")

    # curl_cffi Chrome impersonation is designed for HTTPS; on plain HTTP (e.g. localhost)
    # it can cause curl error 52 (empty reply), so only impersonate on HTTPS.
    impersonate = "chrome" if base_url.startswith("https://") else None
    session = requests.Session(impersonate=impersonate)
    attempts = 0

    while True:
        attempts += 1
        slug = generate_slug(10)
        url = f"{base_url.rstrip('/')}/{slug}"

        try:
            response = session.get(url, timeout=10, allow_redirects=True)

            if response.status_code == 403:
                print(f"[{attempts}] Blocked (403): Rate limits or security challenge.")

            elif response.status_code == 429:
                print(f"\n[!] Rate limited (HTTP 429). Retrying in 10s...")
                time.sleep(10)

            elif response.status_code == 200:
                body_text = response.text

                # Check if the page contains invalid/expired warnings
                is_invalid = any(phrase in body_text for phrase in INVALID_PHRASES)

                if is_invalid:
                    print(f"[{attempts}] Invalid code: {slug}")
                else:
                    # Valid code found
                    print(f"\n[!] WORKING LINK FOUND on attempt #{attempts}: {url}")
                    with open("found_code.txt", "a") as f:
                        f.write(f"{url}\n")
                    break

            else:
                print(f"[{attempts}] Status: {response.status_code}")

        except Exception as e:
            print(f"[{attempts}] Network error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    try:
        run_continuous_search()
    except KeyboardInterrupt:
        print("\nStopped by user.")