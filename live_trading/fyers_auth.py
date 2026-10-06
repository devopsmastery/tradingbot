"""
Fyers Authentication Module (using raw HTTP requests).
Bypasses fyers-apiv3 SDK to avoid Python 3.14 compatibility issues.

Token lifecycle:
  - Fyers tokens are valid for ~24h from generation.
  - .fyers_token           : the raw access token string
  - .fyers_token_meta.json : structured metadata (generated_at, date, source)
  - .last_auth_date        : legacy date stamp (kept for backward compat)

All writes go through _save_token_with_meta() to ensure the metadata
file is always in sync with the token file.
"""

import os
import sys
import json
import hashlib
import requests
from datetime import date, datetime
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

FYERS_APP_ID = os.getenv("FYERS_APP_ID")
FYERS_SECRET_KEY = os.getenv("FYERS_SECRET_KEY")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI")

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", ".fyers_token")
LAST_AUTH_DATE_FILE = os.path.join(os.path.dirname(__file__), "..", ".last_auth_date")
TOKEN_META_FILE = os.path.join(os.path.dirname(__file__), "..", ".fyers_token_meta.json")

# Fyers API endpoints
AUTH_BASE_URL = "https://api-t1.fyers.in/api/v3"
TOKEN_URL = f"{AUTH_BASE_URL}/validate-authcode"


# ================================================================
# Token persistence helpers
# ================================================================

def _save_token_with_meta(access_token: str, source: str = "unknown") -> None:
    """
    Writes the access token to .fyers_token AND writes structured
    metadata to .fyers_token_meta.json so we can track *when* and
    *how* the token was generated.

    Args:
        access_token: the raw JWT access token string.
        source: one of "gui", "cli", "env" — how the token was generated.
    """
    now = datetime.now()

    # Write the raw token
    with open(TOKEN_FILE, "w") as f:
        f.write(access_token)

    # Write structured metadata
    meta = {
        "generated_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "source": source,
        "token_preview": f"{access_token[:10]}...{access_token[-5:]}" if len(access_token) > 15 else "***",
    }
    with open(TOKEN_META_FILE, "w") as f:
        json.dump(meta, f, indent=2)

    # Also write legacy .last_auth_date for backward compat
    with open(LAST_AUTH_DATE_FILE, "w") as f:
        f.write(now.strftime("%Y-%m-%d"))


def get_token_meta() -> dict:
    """
    Returns the structured token metadata, or an empty dict if no
    metadata file exists.

    Returned dict has keys:
        generated_at (str, ISO datetime), date (str, YYYY-MM-DD),
        time (str, HH:MM:SS), source (str), token_preview (str)
    """
    if os.path.exists(TOKEN_META_FILE):
        try:
            with open(TOKEN_META_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    # Fallback: infer from file modification time if meta file is missing
    if os.path.exists(TOKEN_FILE):
        mod_time = datetime.fromtimestamp(os.path.getmtime(TOKEN_FILE))
        return {
            "generated_at": mod_time.isoformat(),
            "date": mod_time.strftime("%Y-%m-%d"),
            "time": mod_time.strftime("%H:%M:%S"),
            "source": "unknown (no meta file)",
            "token_preview": "",
        }

    return {}


TOKEN_VALIDITY_HOURS = 24  # Fyers tokens expire 24h after generation


def token_age_hours() -> float:
    """
    Returns how many hours ago the token was generated.
    Returns float('inf') if no token metadata exists.
    """
    meta = get_token_meta()
    if not meta or "generated_at" not in meta:
        return float("inf")
    try:
        generated = datetime.fromisoformat(meta["generated_at"])
        age = (datetime.now() - generated).total_seconds() / 3600.0
        return age
    except (ValueError, TypeError):
        return float("inf")


def token_hours_remaining() -> float:
    """
    Returns how many hours of validity remain for the current token.
    Negative means expired. Returns -inf if no token exists.
    """
    age = token_age_hours()
    if age == float("inf"):
        return float("-inf")
    return TOKEN_VALIDITY_HOURS - age


def is_token_fresh() -> bool:
    """
    Returns True if the token was generated TODAY (same calendar date).

    Although Fyers documentation sometimes implies 24h rolling validity,
    in practice Fyers wipes/expires all tokens overnight (usually by 6 AM IST).
    A token generated at 11 PM will be invalid by 9 AM the next morning.
    Therefore, calendar date is the safest validity check.
    """
    meta = get_token_meta()
    if not meta:
        return False
    return meta.get("date") == date.today().isoformat()


def generate_login_url() -> str:
    """Generates the Fyers login URL. User must open this in a browser."""
    params = {
        "client_id": FYERS_APP_ID,
        "redirect_uri": FYERS_REDIRECT_URI,
        "response_type": "code",
        "state": "fyers_trading_strategy",
    }
    url = f"https://api-t1.fyers.in/api/v3/generate-authcode?{urlencode(params)}"
    return url


def generate_access_token(auth_code: str, source: str = "unknown") -> str:
    """
    Exchanges the auth_code for an access_token using raw HTTP POST.
    The auth_code is obtained after the user logs in via the login URL.

    Args:
        auth_code: the authorization code from Fyers redirect.
        source: tracking label — "gui", "cli", or "env".
    """
    # Fyers requires an appIdHash = SHA256(app_id + ":" + secret_key)
    app_id_hash = hashlib.sha256(
        f"{FYERS_APP_ID}:{FYERS_SECRET_KEY}".encode()
    ).hexdigest()

    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code,
    }

    response = requests.post(TOKEN_URL, json=payload)
    data = response.json()

    if data.get("s") == "ok" and "access_token" in data:
        access_token = data["access_token"]
        _save_token_with_meta(access_token, source=source)
        print(f"  [OK] Access token generated and saved successfully! (source={source})")
        return access_token
    else:
        raise RuntimeError(f"Failed to generate access token: {data}")


def get_access_token() -> str:
    """
    Returns an access token. Checks in order:
    1. Saved token file (.fyers_token) — warns if older than 24h
    2. FYERS_AUTH_CODE in .env -> exchanges for a new token

    Always returns a token if one exists on disk (even if expired) so that
    scripts can still attempt API calls, but emits a clear warning.
    """
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            token = f.read().strip()
        if token:
            meta = get_token_meta()
            remaining = token_hours_remaining()
            age = token_age_hours()
            src = meta.get("source", "??")
            gen_date = meta.get("date", "unknown")
            gen_time = meta.get("time", "??")

            if remaining > 0:
                print(f"  Using saved access token (generated {gen_date} {gen_time} via {src}, {remaining:.1f}h remaining).")
            else:
                print(f"  [WARNING] Access token EXPIRED (generated {gen_date} {gen_time} via {src}, {abs(remaining):.1f}h past expiry).")
                print(f"            Fyers tokens are valid for {TOKEN_VALIDITY_HOURS}h. Please refresh via the GUI or daily_auth_check().")
            return token

    # Try to get auth_code from .env
    auth_code = os.getenv("FYERS_AUTH_CODE", "").strip()

    if not auth_code or auth_code == "PASTE_YOUR_AUTH_CODE_HERE":
        print("=" * 60)
        print("  FYERS AUTHENTICATION REQUIRED")
        print("=" * 60)
        print()
        print("Step 1: Open the following URL in your browser:")
        print()
        print(f"  {generate_login_url()}")
        print()
        print("Step 2: Log in with your Fyers credentials.")
        print("Step 3: Copy the 'auth_code' from the redirected URL.")
        print("Step 4: Paste it into your .env file as FYERS_AUTH_CODE=<code>")
        print("Step 5: Re-run this script.")
        raise RuntimeError("No auth_code found. Please add FYERS_AUTH_CODE to your .env file.")

    return generate_access_token(auth_code, source="env")


def daily_auth_check():
    """
    Ensures the Fyers token is still valid (within 24h of generation).

    - Uses is_token_fresh() to check if the token is within its 24h window.
    - If still valid: skips silently (no prompt).
    - If expired: prompts user to paste a fresh auth code.

    Call this at the very start of main.py before showing the menu.
    """
    today = date.today().isoformat()

    # Check if token is still within its 24h validity window
    if is_token_fresh():
        meta = get_token_meta()
        remaining = token_hours_remaining()
        print(f"  [OK] Token is valid ({remaining:.1f}h remaining, generated {meta.get('date', '??')} {meta.get('time', '??')} via {meta.get('source', '??')}).")
        return

    # Need fresh auth for today
    print()
    print("=" * 60)
    print("  DAILY TOKEN REFRESH")
    print("=" * 60)
    print()
    print("  A fresh Fyers auth code is required once per trading day.")
    print()
    print("  Step 1: Open this URL in your browser:")
    print(f"          {generate_login_url()}")
    print()
    print("  Step 2: Log in -> copy the 'auth_code' from the redirect URL.")
    print()

    # Check if user already updated .env with today's code
    env_code = os.getenv("FYERS_AUTH_CODE", "").strip()
    prompt_hint = f" (or press Enter to use code already in .env)" if env_code and env_code != "PASTE_YOUR_AUTH_CODE_HERE" else ""

    auth_code_input = input(f"  Paste auth code here{prompt_hint}: ").strip()

    if auth_code_input:
        auth_code = auth_code_input
    elif env_code and env_code != "PASTE_YOUR_AUTH_CODE_HERE":
        auth_code = env_code
        print("  Using auth code from .env file.")
    else:
        print()
        print("  [!] No auth code provided.")
        print("  Add FYERS_AUTH_CODE=<your_code> to your .env file and retry.")
        sys.exit(1)

    # Remove stale token to force fresh generation
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

    print()
    print("  Exchanging auth code for access token...")
    try:
        generate_access_token(auth_code, source="cli")
        # _save_token_with_meta() inside generate_access_token() already
        # wrote .last_auth_date and .fyers_token_meta.json
        print(f"  [OK] Token refreshed. Valid for today ({today}).")
    except RuntimeError as e:
        print(f"  [!] Token generation failed: {e}")
        print("  Please verify your auth code and try again.")
        sys.exit(1)


if __name__ == "__main__":
    token = get_access_token()
    print(f"\nAccess Token: {token[:20]}...")
