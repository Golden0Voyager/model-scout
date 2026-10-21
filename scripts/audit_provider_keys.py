#!/usr/bin/env python3
"""Audit provider key availability and catalogue reachability.

Read-only: GET <base_url>/models only. Never prints key values or lengths.

Usage:
    backend/.venv/bin/python scripts/audit_provider_keys.py            # table + live /models
    backend/.venv/bin/python scripts/audit_provider_keys.py --no-network
    backend/.venv/bin/python scripts/audit_provider_keys.py --json

Exit code is 1 when any provider key is absent/unusable, else 0. Network failures
do not affect the exit code.

Key provenance is reported as one of: "backend/.env", "shell (~/.zshrc chain)",
"shell + .env", or "missing". The script re-executes itself through zsh and
sources ~/.zshrc, because that is exactly how start.sh builds the service
environment; ~/.zshrc in turn sources ~/.zshenv.secrets, where some keys live.
Loading only backend/.env would report those keys as missing.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import httpx  # noqa: E402
from core.config import PROVIDERS  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

PLACEHOLDERS = {"", "***", "YOUR_API_KEY", "placeholder"}
SHELL_SOURCE = "shell (~/.zshrc chain)"


def reexec_via_shell() -> None:
    if "--via-shell" in sys.argv or not shutil.which("zsh"):
        return
    args = [a for a in sys.argv[1:] if a != "--via-shell"]
    inner = " ".join(shlex.quote(part) for part in [sys.executable, os.path.abspath(__file__), *args, "--via-shell"])
    os.execvp("zsh", ["zsh", "-c", f"source ~/.zshrc >/dev/null 2>&1; exec {inner}"])


def resolve_proxy(env_file: dict[str, str]) -> tuple[str | None, str]:
    """Mirror app._resolve_proxy_url, with backend/.env standing in for the ambient
    variables that app.py's load_dotenv() would have exported."""
    explicit = os.getenv("MODELSCOUT_PROXY_URL", "").strip()
    if explicit:
        return explicit, "env"
    explicit = (env_file.get("MODELSCOUT_PROXY_URL") or "").strip()
    if explicit:
        return explicit, "backend/.env"
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.getenv(name, "").strip()
        if value:
            return value, "env"
        value = (env_file.get(name) or "").strip()
        if value:
            return value, "backend/.env"
    return None, "none"


def key_state(provider, env_file: dict[str, str]) -> tuple[str, str | None]:
    env_name = provider.api_key_env
    shell_value = os.getenv(env_name)
    file_value = env_file.get(env_name)
    shell_ok = shell_value is not None and shell_value.strip() not in PLACEHOLDERS
    file_ok = file_value is not None and file_value.strip() not in PLACEHOLDERS

    if shell_ok and file_ok:
        return "shell + .env", shell_value
    if shell_ok:
        return SHELL_SOURCE, shell_value
    if file_ok:
        return "backend/.env", file_value

    present = shell_value is not None or file_value is not None
    return ("empty/placeholder" if present else "missing"), None


def probe(provider, key: str, client: httpx.Client) -> dict:
    if not provider.models_endpoint:
        return {"http": "-", "models": "-", "note": "static catalogue (no /models)"}
    url = provider.base_url.rstrip("/") + provider.models_endpoint
    headers = {"api-key": key} if provider.auth_style == "api_key" else {"Authorization": f"Bearer {key}"}
    if provider.user_agent:
        headers["User-Agent"] = provider.user_agent
    last: httpx.HTTPError | None = None
    for attempt in range(3):
        try:
            response = client.get(url, headers=headers)
            if response.status_code != 200:
                return {"http": str(response.status_code), "models": "-", "note": ""}
            payload = response.json()
            rows = payload.get("data") or payload.get("models") or []
            return {"http": "200", "models": str(len(rows)), "note": ""}
        except httpx.HTTPError as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    reason = f"{type(last).__name__}: {str(last).splitlines()[0]}"[:70] if last else "unknown"
    return {"http": "ERR", "models": "-", "note": reason}


def main() -> int:
    reexec_via_shell()

    parser = argparse.ArgumentParser(description="Audit provider keys without printing them")
    parser.add_argument("--no-network", action="store_true", help="skip the live GET /models probe")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--via-shell", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    env_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", ".env"))
    env_file = dotenv_values(env_path) if os.path.exists(env_path) else {}
    proxy, proxy_source = resolve_proxy(env_file)
    client = httpx.Client(
        proxy=proxy,
        timeout=20.0,
        follow_redirects=True,
        trust_env=False,
    )

    results = []
    for provider in PROVIDERS.values():
        provenance, key = key_state(provider, env_file)
        row = {
            "key": provider.key,
            "name": provider.name,
            "env": provider.api_key_env,
            "provenance": provenance,
            "network": provider.network,
        }
        if key and not args.no_network:
            row.update(probe(provider, key, client))
        else:
            row.update({"http": "-", "models": "-", "note": ""})
        results.append(row)

    client.close()

    missing = [r for r in results if not (r["provenance"] == "backend/.env" or "shell" in r["provenance"])]
    if args.json:
        print(
            json.dumps(
                {"env_file": env_path, "proxy": proxy, "proxy_source": proxy_source, "rows": results},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"env file : {env_path}")
        print(f"proxy    : {proxy or '(none)'} (from {proxy_source})")
        print(f"shell    : {'loaded via ~/.zshrc' if '--via-shell' in sys.argv else 'not loaded'}")
        print()
        header = f"{'provider':18s} {'key source':26s} {'/models':8s} {'models':7s} note"
        print(header)
        print("-" * len(header))
        for row in results:
            http = row["http"]
            mark = "OK " if http == "200" else ("-- " if http in ("-", "ERR") else http + " ")
            print(
                f"{row['key']:18s} {row['provenance']:26s} {mark:8s} {row['models']:7s} {row['note']}"
                f"{'' if row['provenance'] not in ('missing', 'empty/placeholder') else '  <= ' + row['env']}"
            )
        print()
        rejected = [r for r in results if r["http"] in ("401", "403")]
        print(
            f"{len(results)} providers, {len(results) - len(missing)} keys present, "
            f"{len(missing)} missing/empty, {len(rejected)} rejected by provider (401/403)"
        )

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
