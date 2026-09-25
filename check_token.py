#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Checks whether a CLAUDE_CODE_OAUTH_TOKEN still authenticates, before it goes to the secret.

Usage: ./check_token.py <token>
Exits 0 when the token works, 1 when it is rejected (e.g. a 401 from an expired token).
"""

import json
import os
import subprocess
import sys
import tempfile

TIMEOUT_SECONDS = 120


def check_token(oauth_token: str) -> tuple[bool, str]:
    # Same isolation as run_claude in bot.py, so the result reflects what the bot sees
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model", "haiku",
            "--tools", "",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "json",
        ],
        input="Responda apenas: ok",
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        cwd=tempfile.gettempdir(),
        check=False,
    )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = {}
    if result.returncode == 0 and not output.get("is_error"):
        return True, "Token válido."
    status = output.get("api_error_status")
    if status == 401:
        return False, "Token recusado (401): expirou ou foi revogado. Gere outro com `claude setup-token`."
    detail = f"api_error_status={status}" if status else (result.stderr.strip() or "sem detalhes")
    return False, f"Falha ao chamar o Claude (código {result.returncode}, {detail})."


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        sys.exit("Uso: ./check_token.py <token>")
    ok, message = check_token(sys.argv[1].strip())
    print(message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
