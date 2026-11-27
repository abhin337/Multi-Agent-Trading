"""Isolated Zerodha Kite credential loader, OS keyring support, and daily OAuth helper."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import keyring
from dotenv import dotenv_values
from pydantic import SecretStr

KEYRING_SERVICE_NAME = "nse-multi-agent-trader-kite"


@dataclass(frozen=True)
class KiteCredentials:
    """Masked container for Zerodha Kite API credentials."""

    api_key: SecretStr
    access_token: SecretStr

    def __repr__(self) -> str:
        return "KiteCredentials(api_key='[REDACTED]', access_token='[REDACTED]')"


def _read_from_keyring(key_name: str) -> str:
    try:
        val = keyring.get_password(KEYRING_SERVICE_NAME, key_name)
        return (val or "").strip()
    except Exception:
        return ""


def _write_to_keyring(key_name: str, value: str) -> bool:
    try:
        keyring.set_password(KEYRING_SERVICE_NAME, key_name, value)
        return True
    except Exception:
        return False


def load_kite_credentials(
    env_file: str | Path = ".env",
    use_keyring: bool = True,
) -> KiteCredentials | None:
    """Load Zerodha credentials strictly inside execution/ (os.environ -> keyring -> .env).

    Never mutates os.environ so read-only agents and MCP subprocesses remain isolated.
    """
    api_key = os.getenv("KITE_API_KEY", "").strip()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    if use_keyring and (not api_key or not access_token):
        api_key = api_key or _read_from_keyring("KITE_API_KEY")
        access_token = access_token or _read_from_keyring("KITE_ACCESS_TOKEN")

    if (not api_key or not access_token) and Path(env_file).is_file():
        file_vars = dotenv_values(env_file)
        api_key = api_key or (file_vars.get("KITE_API_KEY") or "").strip()
        access_token = access_token or (file_vars.get("KITE_ACCESS_TOKEN") or "").strip()

    if not api_key or not access_token:
        return None
    return KiteCredentials(
        api_key=SecretStr(api_key),
        access_token=SecretStr(access_token),
    )


def save_kite_credentials(
    api_key: str,
    access_token: str,
    target: Literal["keyring", "dotenv"] = "keyring",
    env_file: str | Path = ".env",
) -> str:
    """Persist KITE_API_KEY and KITE_ACCESS_TOKEN safely (never storing api_secret)."""
    if target == "keyring":
        ok_key = _write_to_keyring("KITE_API_KEY", api_key)
        ok_tok = _write_to_keyring("KITE_ACCESS_TOKEN", access_token)
        if ok_key and ok_tok:
            return (
                f"Saved KITE_API_KEY and KITE_ACCESS_TOKEN to OS keyring "
                f"({KEYRING_SERVICE_NAME})."
            )

    path = Path(env_file)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    updated: list[str] = []
    seen_key = False
    seen_tok = False
    for line in lines:
        if line.startswith("KITE_API_KEY="):
            updated.append(f"KITE_API_KEY={api_key}")
            seen_key = True
        elif line.startswith("KITE_ACCESS_TOKEN="):
            updated.append(f"KITE_ACCESS_TOKEN={access_token}")
            seen_tok = True
        else:
            updated.append(line)
    if not seen_key:
        updated.append(f"KITE_API_KEY={api_key}")
    if not seen_tok:
        updated.append(f"KITE_ACCESS_TOKEN={access_token}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return f"Saved KITE_API_KEY and KITE_ACCESS_TOKEN to {path}."


def run_kite_login_cli(
    target: Literal["keyring", "dotenv"] = "keyring",
    input_fn: Callable[[str], str] = input,
    secret_input_fn: Callable[[str], str] = getpass.getpass,
    echo_fn: Callable[[str], None] = print,
) -> str:
    """Interactive daily Kite Connect login helper to exchange request_token for access_token."""
    from kiteconnect import KiteConnect

    api_key = input_fn("Enter KITE_API_KEY: ").strip()
    api_secret = secret_input_fn("Enter KITE_API_SECRET (never stored): ").strip()
    if not api_key or not api_secret:
        raise ValueError("Both KITE_API_KEY and KITE_API_SECRET are required.")

    kite = KiteConnect(api_key=api_key)
    echo_fn(f"\nOpen this Kite login URL in your browser:\n{kite.login_url()}\n")
    request_token = input_fn("Paste the 'request_token' from redirect URL: ").strip()
    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = str(session["access_token"]).strip()
    status_msg = save_kite_credentials(api_key=api_key, access_token=access_token, target=target)
    echo_fn(status_msg)
    return status_msg
