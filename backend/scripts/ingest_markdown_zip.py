#!/usr/bin/env python3
"""Upload a Markdown ZIP and run all ingest batches until the session is complete.

Example (existing user):

    python scripts/ingest_markdown_zip.py dds_knowledge_base_full.zip \\
      --email you@example.com --password 'your-password'

Example (token from env):

    ACCESS_TOKEN=... python scripts/ingest_markdown_zip.py dds_knowledge_base_full.zip

Requires: API reachable, OPENAI_API_KEY on the server, MongoDB + Qdrant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        _die(f"Login failed ({r.status_code}): {r.text}")
    body = r.json()
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        _die(f"Login response missing access_token: {body!r}")
    return token


def _maybe_register(client: httpx.Client, email: str, password: str, full_name: str) -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    if r.status_code == 201:
        print("Registered new user.", file=sys.stderr)
        return
    if r.status_code == 400:
        try:
            detail = r.json().get("detail", r.text)
        except json.JSONDecodeError:
            detail = r.text
        if isinstance(detail, str) and "already exists" in detail.lower():
            print("User already exists; continuing to login.", file=sys.stderr)
            return
    _die(f"Register failed ({r.status_code}): {r.text}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "zip_file",
        type=Path,
        help="Path to .zip containing Markdown (.md / .markdown) files",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        help="API root (default: env API_BASE_URL or http://localhost:8000)",
    )
    p.add_argument(
        "--token",
        default=os.environ.get("ACCESS_TOKEN"),
        help="Bearer token (default: env ACCESS_TOKEN)",
    )
    p.add_argument("--email", help="Login email (with --password) if no --token")
    p.add_argument("--password", help="Login password")
    p.add_argument(
        "--register",
        action="store_true",
        help="POST /auth/register before login (for first-time account)",
    )
    p.add_argument(
        "--full-name",
        default="DDS Tester",
        help="With --register: full_name field",
    )
    args = p.parse_args()

    zpath = args.zip_file.expanduser().resolve()
    if not zpath.is_file():
        _die(f"Not a file: {zpath}")
    if zpath.suffix.lower() != ".zip":
        _die("zip_file must end with .zip")

    token = args.token
    base = args.base_url.rstrip("/")
    timeout = httpx.Timeout(600.0, connect=30.0)

    with httpx.Client(base_url=base, timeout=timeout) as client:
        if token:
            pass
        elif args.email and args.password:
            if args.register:
                _maybe_register(client, args.email, args.password, args.full_name)
            token = _login(client, args.email, args.password)
        else:
            _die(
                "Provide --token or ACCESS_TOKEN, or both --email and --password "
                "(use --register on first run)."
            )

        headers = _auth_headers(token)

        with zpath.open("rb") as zf:
            r = client.post(
                "/api/v1/documents/zip-sessions",
                headers=headers,
                files={"file": (zpath.name, zf, "application/zip")},
            )
        if r.status_code != 201:
            _die(f"zip-sessions failed ({r.status_code}): {r.text}")
        session = r.json()
        session_id = session.get("session_id")
        total = len(session.get("markdown_files") or [])
        if not isinstance(session_id, str) or not session_id:
            _die(f"Unexpected zip-sessions response: {session!r}")
        print(f"Session {session_id}: {total} markdown path(s) in manifest.", file=sys.stderr)

        batch = 0
        while True:
            batch += 1
            ir = client.post(
                f"/api/v1/documents/zip-sessions/{session_id}/ingest",
                headers={**headers, "Content-Type": "application/json"},
                content=b"{}",
            )
            if ir.status_code != 201:
                _die(f"Ingest batch {batch} failed ({ir.status_code}): {ir.text}")
            body = ir.json()
            indexed = body.get("files_indexed", 0)
            skipped = body.get("files_skipped", 0)
            more = body.get("has_more_markdown_files", False)
            print(
                f"Ingest batch {batch}: indexed={indexed} skipped={skipped} "
                f"has_more={more}",
                file=sys.stderr,
            )
            if not more:
                print(json.dumps(body, indent=2))
                break


if __name__ == "__main__":
    main()
