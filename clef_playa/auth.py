import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

CF_CONFIGS = [
    Path.home() / "Library/Preferences/cloudflare/config/default.json",
    Path.home() / ".config/cloudflare/config/default.json",
]
WRANGLER_CONFIGS = [
    Path.home() / ".wrangler/config/default.toml",
    Path.home() / "Library/Preferences/.wrangler/config/default.toml",
]
# cf's OAuth tokens live about an hour; refresh ahead of time so a tile request does
# not race the expiry.
REFRESH_MARGIN = timedelta(minutes=5)


@dataclass
class Credentials:
    token: str
    account_id: str
    source: str


class AuthError(RuntimeError):
    pass


def _parse_exp(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _cf_config() -> tuple[Path, dict] | None:
    for path in CF_CONFIGS:
        if path.exists():
            return path, json.loads(path.read_text())
    return None


def _cf_refresh() -> bool:
    # Any authenticated cf command refreshes the on-disk token with the refresh token;
    # whoami is the cheapest one.
    cf = shutil.which("cloudflare") or shutil.which("cf")
    if not cf:
        return False
    r = subprocess.run([cf, "auth", "whoami", "-q"], check=False, capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def _cf_token(force_refresh: bool = False) -> tuple[str, str] | None:
    found = _cf_config()
    if not found:
        return None
    path, cfg = found
    exp = _parse_exp(cfg.get("expiration_time"))
    stale = force_refresh or exp is None or exp - REFRESH_MARGIN <= datetime.now(UTC)
    if stale:
        if not _cf_refresh():
            raise AuthError(
                f"cf token in {path} is expired and `cf auth whoami` could not refresh it. Run `cf auth login`."
            )
        _, cfg = _cf_config()
        exp = _parse_exp(cfg.get("expiration_time"))
        if exp and exp <= datetime.now(UTC):
            raise AuthError(f"cf token in {path} is still expired after refresh. Run `cf auth login`.")
    token = cfg.get("oauth_token")
    return (token, f"cf:{path}") if token else None


def _wrangler_token() -> tuple[str, str] | None:
    now = datetime.now(UTC)
    for path in WRANGLER_CONFIGS:
        if not path.exists():
            continue
        cfg = tomllib.loads(path.read_text())
        token = cfg.get("oauth_token")
        if not token or "ai:write" not in cfg.get("scopes", []):
            continue
        exp = _parse_exp(cfg.get("expiration_time"))
        if exp and exp <= now:
            continue
        return token, f"wrangler:{path}"
    return None


def _resolve_account(token: str) -> str:
    r = httpx.get(
        "https://api.cloudflare.com/client/v4/accounts",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 50},
        timeout=30,
    )
    r.raise_for_status()
    accounts = r.json()["result"]
    if len(accounts) == 1:
        return accounts[0]["id"]
    listing = "\n".join(f"  {a['id']}  {a['name']}" for a in accounts)
    raise AuthError("Token can see multiple accounts; set CLOUDFLARE_ACCOUNT_ID or pass --account-id:\n" + listing)


def resolve(account_id: str | None = None, force_refresh: bool = False) -> Credentials:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    source = "env:CLOUDFLARE_API_TOKEN"
    if not token:
        found = _cf_token(force_refresh) or _wrangler_token()
        if not found:
            raise AuthError(
                "No credentials. Run `cf auth login` (npm i -g cf) or set CLOUDFLARE_API_TOKEN with Workers AI: Edit."
            )
        token, source = found
    account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or _resolve_account(token)
    return Credentials(token=token, account_id=account_id, source=source)
