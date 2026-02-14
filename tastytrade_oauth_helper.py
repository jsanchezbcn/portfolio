#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import urllib.parse
from pathlib import Path

import requests

API_BASE = "https://api.tastyworks.com"
AUTH_BASE = "https://my.tastytrade.com/auth.html"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (key not in os.environ or not os.environ.get(key)):
            os.environ[key] = value


def env_value(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def build_auth_url(client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return f"{AUTH_BASE}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> dict:
    response = requests.post(
        f"{API_BASE}/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def refresh_access_token(
    client_secret: str,
    refresh_token: str,
) -> dict:
    response = requests.post(
        f"{API_BASE}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def upsert_env(path: Path, key: str, value: str) -> None:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = content.splitlines()
    updated = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tastytrade OAuth helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_url_parser = subparsers.add_parser("auth-url", help="Generate authorization URL")
    auth_url_parser.add_argument("--scope", default="read", help="OAuth scopes, e.g. 'read trade'")
    auth_url_parser.add_argument("--redirect-uri", default=None)

    exchange_parser = subparsers.add_parser("exchange", help="Exchange auth code for tokens")
    exchange_parser.add_argument("--code", required=True, help="Authorization code from redirect")
    exchange_parser.add_argument("--redirect-uri", default=None)
    exchange_parser.add_argument("--write-env", action="store_true", help="Write refresh token to .env")

    refresh_parser = subparsers.add_parser("verify-refresh", help="Validate a refresh token and get access token")
    refresh_parser.add_argument("--refresh-token", default=None, help="Refresh token (defaults to .env TASTYTRADE_REFRESH_TOKEN)")

    args = parser.parse_args()

    env_path = Path(".env")
    load_dotenv(env_path)

    client_id = env_value("TASTYTRADE_CLIENT_ID", "TASTYWORKS_CLIENT_ID", "CLIENTID")
    client_secret = env_value("TASTYTRADE_CLIENT_SECRET", "TASTYWORKS_CLIENT_SECRET", "SECRET")
    redirect_arg = getattr(args, "redirect_uri", None)
    redirect_uri = redirect_arg or env_value("TASTYTRADE_REDIRECT_URI", "TASTYWORKS_REDIRECT_URI") or "http://localhost:8080"

    if not client_id or not client_secret:
        raise SystemExit("Missing client ID/secret in env (.env keys: CLIENTID and SECRET)")

    if args.command == "auth-url":
        state = secrets.token_urlsafe(16)
        url = build_auth_url(client_id=client_id, redirect_uri=redirect_uri, scope=args.scope, state=state)
        print("Open this URL in your browser and authorize the app (trusted-partner OAuth flow):")
        print(url)
        print("\nIf this URL returns 404 for your personal app, create a personal grant in my.tastytrade.com and use:")
        print("python tastytrade_oauth_helper.py verify-refresh --refresh-token <TOKEN>")
        print("\nAfter redirect, copy the 'code' query parameter and run:")
        print("python tastytrade_oauth_helper.py exchange --code <CODE> --write-env")
        return

    if args.command == "exchange":
        tokens = exchange_code_for_tokens(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code=args.code,
        )
        refresh_token = tokens.get("refresh_token")
        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in")

        print("Token exchange succeeded")
        print(f"access_token_present={bool(access_token)} expires_in={expires_in}")
        print(f"refresh_token_present={bool(refresh_token)}")

        if args.write_env and refresh_token:
            upsert_env(env_path, "TASTYTRADE_REFRESH_TOKEN", refresh_token)
            print("Saved TASTYTRADE_REFRESH_TOKEN to .env")

    if args.command == "verify-refresh":
        refresh_token = args.refresh_token or env_value("TASTYTRADE_REFRESH_TOKEN", "TASTYWORKS_REFRESH_TOKEN")
        if not refresh_token:
            raise SystemExit("Missing refresh token. Provide --refresh-token or set TASTYTRADE_REFRESH_TOKEN in .env")

        tokens = refresh_access_token(
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
        access_token = tokens.get("access_token")
        expires_in = tokens.get("expires_in")
        print("Refresh-token exchange succeeded")
        print(f"access_token_present={bool(access_token)} expires_in={expires_in}")


if __name__ == "__main__":
    main()
