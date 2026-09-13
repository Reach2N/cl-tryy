"""Read-only referral lookup used by Claude's public referral page."""

import json

API_BASE = "https://claude.ai/api/referral/code/"


def api_url(referral_url):
    return API_BASE + referral_url.rsplit("/", 1)[-1]


def classify_api_response(response, referral_url):
    status = response.status_code
    if status == 429:
        retry = response.headers.get("Retry-After")
        return "rate_limited", "HTTP 429; stopping." + (f" Retry-After: {retry}." if retry else "")
    if status in (401, 403):
        return "blocked", f"HTTP {status}; access denied, stopping."
    if response.headers.get("cf-mitigated", "").casefold() == "challenge":
        return "blocked", "Browser challenge; stopping."
    if status >= 500:
        return "error", f"HTTP {status}; server error."
    if status != 200 or response.url != api_url(referral_url):
        return "unknown", f"Unexpected validity response (HTTP {status}); stopping."

    try:
        payload = json.loads(response.text)
    except (ValueError, TypeError):
        text = response.text.casefold()
        if any(phrase in text for phrase in ("just a moment", "verify you are human",
                                             "checking your browser")):
            return "blocked", "Browser challenge; stopping."
        return "unknown", "The validity endpoint did not return JSON; stopping."
    # Observed response for a nonexistent code is JSON null.
    if payload is None:
        return "invalid", "Code does not exist."
    if not isinstance(payload, dict) or payload.get("code") != referral_url.rsplit("/", 1)[-1]:
        return "unknown", "Unexpected referral data or mismatched code; stopping."
    if payload.get("is_valid") is True:
        return "valid", "Server reports is_valid=true; account eligibility still applies."
    if payload.get("is_valid") is False:
        return "invalid", "Server reports is_valid=false (expired or unavailable)."
    return "unknown", "Missing boolean is_valid field; stopping."
