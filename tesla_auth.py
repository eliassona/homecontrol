"""
Tesla Fleet API OAuth setup script.
Run once to generate tesla_token.json.

Usage:
  python3 tesla_auth.py

You will need:
  - A Tesla Developer account: https://developer.tesla.com
  - An application with client_id and client_secret
  - Your Tesla account email and password

The generated tesla_token.json contains only tokens (no password).
HomeControl uses it to refresh access automatically.
"""

import json
import urllib.request
import urllib.parse
import webbrowser
import secrets
import hashlib
import base64
import sys


CLIENT_ID     = input("Tesla client_id: ").strip()
CLIENT_SECRET = input("Tesla client_secret: ").strip()
REDIRECT_URI  = "https://auth.tesla.com/void/callback"
SCOPES        = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"
TOKEN_FILE    = "tesla_token.json"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# PKCE
code_verifier  = b64url(secrets.token_bytes(32))
code_challenge = b64url(hashlib.sha256(code_verifier.encode()).digest())
state          = secrets.token_hex(16)

auth_url = (
    "https://auth.tesla.com/oauth2/v3/authorize"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&response_type=code"
    f"&scope={urllib.parse.quote(SCOPES)}"
    f"&state={state}"
    f"&code_challenge={code_challenge}"
    f"&code_challenge_method=S256"
)

print("\nOpening Tesla login in your browser...")
print("If it doesn't open, visit this URL manually:\n")
print(auth_url)
webbrowser.open(auth_url)

print("\nAfter logging in, Tesla will redirect to a page that may show an error.")
print("Copy the full URL from your browser's address bar and paste it below.\n")
redirect = input("Redirected URL: ").strip()

# Extract code from redirect URL
parsed = urllib.parse.urlparse(redirect)
params = urllib.parse.parse_qs(parsed.query)
code   = params.get("code", [None])[0]
if not code:
    print("ERROR: Could not find 'code' in the URL. Did you copy the full URL?")
    sys.exit(1)

# Exchange code for tokens
payload = urllib.parse.urlencode({
    "grant_type":    "authorization_code",
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code":          code,
    "redirect_uri":  REDIRECT_URI,
    "code_verifier": code_verifier,
}).encode()

req = urllib.request.Request(
    "https://auth.tesla.com/oauth2/v3/token",
    data=payload,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)

with urllib.request.urlopen(req, timeout=15) as resp:
    token_data = json.loads(resp.read().decode())

import time
output = {
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "access_token":  token_data["access_token"],
    "refresh_token": token_data["refresh_token"],
    "expires_at":    time.time() + token_data.get("expires_in", 3600) - 60,
}

with open(TOKEN_FILE, "w") as f:
    json.dump(output, f, indent=2)

print(f"\nTokens saved to {TOKEN_FILE}")
print("You can now add the tesla_car device to config.json.")
print("Do NOT share or commit tesla_token.json — it gives full access to your vehicle.")
