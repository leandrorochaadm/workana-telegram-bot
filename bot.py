#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "playwright",
#   "requests",
# ]
# ///
"""Monitora projetos novos na Workana filtrados por palavras-chave e avisa no Telegram."""

import html
import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
SEEN_FILE = BASE_DIR / "seen.json"

WORKANA_URL = "https://www.workana.com/jobs?language=pt&category=it-programming"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def fetch_projects() -> list[dict[str, str]]:
    projects: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )
        page.goto(WORKANA_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("a[href^='/job/']", timeout=60_000)
        for link in page.query_selector_all("a[href^='/job/']"):
            href = link.get_attribute("href") or ""
            span = link.query_selector("span[title]")
            title = ((span.get_attribute("title") if span else None) or link.inner_text()).strip()
            if not href or not title:
                continue
            url = f"https://www.workana.com{href.split('?')[0]}"
            projects.append({"id": url, "title": title, "url": url})
        browser.close()
    return projects


def matches_keywords(title: str, keywords: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(kw)}s?\b", title, re.IGNORECASE) for kw in keywords)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    env = load_env()
    token = env.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    keywords_raw = env.get("KEYWORDS") or os.environ.get("KEYWORDS", "")
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

    if not token or not chat_id:
        print("Faltam TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID no .env", file=sys.stderr)
        sys.exit(1)
    if not keywords:
        print("Nenhuma keyword configurada em KEYWORDS no .env", file=sys.stderr)
        sys.exit(1)

    seen = load_seen()
    projects = fetch_projects()

    sent = failed = 0
    try:
        for project in projects:
            if project["id"] in seen:
                continue
            if not matches_keywords(project["title"], keywords):
                seen.add(project["id"])
                continue
            text = f"🆕 <b>{html.escape(project['title'])}</b>\n{project['url']}"
            try:
                send_telegram(token, chat_id, text)
            except requests.RequestException as exc:
                # left out of seen so the next run retries it
                failed += 1
                print(f"Falha ao enviar {project['url']}: {exc}", file=sys.stderr)
                continue
            seen.add(project["id"])
            sent += 1
    finally:
        save_seen(seen)

    print(f"{len(projects)} projetos lidos, {sent} enviados, {failed} com falha.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
