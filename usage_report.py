#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Sends the Claude plan usage (5h window, weekly and per-model limits) to Telegram.

The subscription only exposes usage as a percentage, through the rate-limit headers of
any request made with the OAuth token. A 1-token Haiku call is enough to read them.
"""

import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
PROBE_MODEL = "claude-haiku-4-5-20251001"
HEADER_PREFIX = "anthropic-ratelimit-unified-"
UTILIZATION_HEADER = re.compile(rf"^{HEADER_PREFIX}(.+)-utilization$")
# Always listed, even when missing; any other limit in the response (e.g. "7d_fable") follows
WINDOWS = {"5h": "Janela de 5h", "7d": "Limite semanal"}
WINDOW_LENGTHS = {"5h": timedelta(hours=5), "7d": timedelta(days=7)}
MODEL_NAMES = {"fable": "Fable", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}


def fetch_usage(oauth_token: str) -> dict[str, tuple[float, datetime | None]]:
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Authorization": f"Bearer {oauth_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": PROBE_MODEL,
            "max_tokens": 1,
            "system": "You are Claude Code, Anthropic's official CLI for Claude.",
            "messages": [{"role": "user", "content": "ok"}],
        },
        timeout=30,
    )
    if response.status_code == 401:
        sys.exit("Token recusado (401): gere outro com `claude setup-token`.")
    # A 429 (limit reached) still carries the headers, so only other errors are fatal
    usage = {}
    for name, utilization in response.headers.items():
        match = UTILIZATION_HEADER.match(name.lower())
        if not match:
            continue
        key = match.group(1)
        reset_at = parse_reset(response.headers.get(f"{HEADER_PREFIX}{key}-reset"))
        usage[key] = (float(utilization), reset_at)
    if not usage:
        sys.exit(f"Resposta sem dados de uso (HTTP {response.status_code}): {response.text[:300]}")
    return usage


def parse_reset(value: str | None) -> datetime | None:
    # Expected as Unix seconds; ISO 8601 is accepted too, and anything else is just omitted
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), LOCAL_TZ)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    except ValueError:
        return None


def progress_bar(fraction: float, width: int = 20) -> str:
    filled = round(min(max(fraction, 0), 1) * width)
    return "█" * filled + "░" * (width - filled)


def window_length(key: str) -> timedelta | None:
    # Per-model limits share the length of the window they start with ("7d_fable" -> 7d)
    return next((length for prefix, length in WINDOW_LENGTHS.items() if key.startswith(prefix)), None)


def window_label(key: str) -> str:
    if key in WINDOWS:
        return WINDOWS[key]
    model = next((name for token, name in MODEL_NAMES.items() if token in key), None)
    base = next((label for prefix, label in WINDOWS.items() if key.startswith(prefix)), None)
    if model and base:
        return f"{base} do {model}"
    return f"Limite {key}"


def elapsed_fraction(key: str, reset_at: datetime, now: datetime) -> float | None:
    length = window_length(key)
    if length is None:
        return None
    # The window started one full length before its reset
    elapsed = 1 - (reset_at - now) / length
    return min(max(elapsed, 0), 1)


def format_message(
    usage: dict[str, tuple[float, datetime | None]], now: datetime | None = None
) -> str:
    now = now or datetime.now(LOCAL_TZ)
    lines = ["<b>Uso do Claude</b>"]
    extra_keys = sorted(key for key in usage if key not in WINDOWS)
    for key in [*WINDOWS, *extra_keys]:
        label = window_label(key)
        if key not in usage:
            lines.append(f"\n<b>{label}</b>\nSem dados.")
            continue
        fraction, reset_at = usage[key]
        lines.append(f"\n<b>{label}</b>\n{progress_bar(fraction)} {fraction:.0%} usado")
        if reset_at:
            elapsed = elapsed_fraction(key, reset_at, now)
            if elapsed is not None:
                lines.append(f"{progress_bar(elapsed)} {elapsed:.0%} do tempo")
            lines.append(f"Reinicia em {reset_at:%d/%m às %H:%M}")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    response.raise_for_status()


def main() -> None:
    names = ("CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        sys.exit(f"Variáveis ausentes: {', '.join(missing)}")
    usage = fetch_usage(os.environ["CLAUDE_CODE_OAUTH_TOKEN"])
    # Actions logs are public: only the limit names are logged, the report goes to Telegram
    print(f"Limites encontrados: {', '.join(sorted(usage))}")
    message = format_message(usage)
    send_telegram(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], message)
    print("Relatório de uso enviado.")


if __name__ == "__main__":
    main()
