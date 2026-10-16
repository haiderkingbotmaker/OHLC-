"""Shared session/token helpers for Quotex authentication."""
import re
import json
from typing import Optional, Tuple, Dict, Any


def is_valid_ws_token(token: Optional[str]) -> bool:
    """Return True if value looks like a Quotex WebSocket session token."""
    if not token or not isinstance(token, str):
        return False
    token = token.strip()
    if len(token) < 20 or len(token) > 128:
        return False
    # Laravel/encrypted cookies are long base64 blobs — never valid WS tokens
    if token.startswith("eyJ") or "%3D" in token or token.count(".") >= 2:
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_-]+", token))


def extract_settings_json(text: str) -> Dict[str, Any]:
    """Extract window.settings JSON object using balanced-brace parsing."""
    idx = text.find("window.settings")
    if idx == -1:
        return {}
    brace_start = text.find("{", idx)
    if brace_start == -1:
        return {}
    depth = 0
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def extract_token_from_html(html: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """Extract WebSocket token from /trade page HTML."""
    from bs4 import BeautifulSoup

    settings_json: Dict[str, Any] = {}
    token: Optional[str] = None

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = (script.get_text() or "").strip()
        if not text:
            continue

        if "window.settings" in text:
            settings_json = extract_settings_json(text)
            candidate = settings_json.get("token")
            if is_valid_ws_token(candidate):
                token = candidate
                break

        if not token:
            match = re.search(
                r'(?:"token"|token)\s*[:=]\s*["\']([a-zA-Z0-9_-]{20,128})["\']',
                text,
            )
            if match and is_valid_ws_token(match.group(1)):
                token = match.group(1)
                break

    return token, settings_json


def extract_ws_session_cookie(cookies: Dict[str, str]) -> Optional[str]:
    """Return a WebSocket session value from cookies, excluding Laravel cookies."""
    for name, value in cookies.items():
        lower = name.lower()
        if lower in ("laravel_session", "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"):
            continue
        if lower in ("session", "ssid", "qx_session") and is_valid_ws_token(value):
            return value
    return None
