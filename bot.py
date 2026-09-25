#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "anthropic",
#   "cryptography",
#   "playwright==1.63.0",
#   "requests",
# ]
# ///
"""Monitora projetos novos na Workana filtrados por palavras-chave e avisa no Telegram."""

import html
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import anthropic
import requests
from cryptography.fernet import Fernet, InvalidToken
from playwright.sync_api import Browser, Page, sync_playwright
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
SEEN_FILE = BASE_DIR / "seen.json"
# Matched jobs still waiting for a proposal: {id: {"title", "url", "attempts"}}
PENDING_FILE = BASE_DIR / "pending.json"
# Encrypted because the repo is public and the prompt holds private pricing rules
PROMPT_FILE = BASE_DIR / "proposal_prompt.enc"

# Jobs from the last 24h; a job listed by both searches is kept once
WORKANA_URLS = (
    "https://www.workana.com/jobs?language=pt&publication=1d&query=aplicativo&region=029%2C013%2C005",
    "https://www.workana.com/jobs?language=pt&publication=1d&query=app&region=029%2C013%2C005",
)
JOB_LINK_SELECTOR = "a[href^='/job/']"
NO_RESULTS_TEXT = "Não foram encontrados projetos"
# Logged-out listings show 7 jobs per page, sorted by relevance, not by date
MAX_PAGES_PER_SEARCH = 5
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

PROPOSAL_MODEL = "claude-opus-5-5"
# Keeps a burst of new jobs from blowing the workflow timeout and the API bill
MAX_PROPOSALS_PER_RUN = 5
# Worst case must fit the workflow's 14-min timeout: ~1.5 min of setup, the
# deadline below, then one last proposal (job page + one API call) and the commit
PROPOSAL_TIMEOUT_SECONDS = 180
PROPOSAL_MAX_RETRIES = 0
PAGE_TIMEOUT_MS = 30_000
PROPOSAL_DEADLINE_SECONDS = 8 * 60
# A job that keeps failing (refusal, page gone) is sent without a proposal after
# this many tries, so it cannot bill every run forever
MAX_PROPOSAL_ATTEMPTS = 3
TELEGRAM_MAX_CHARS = 4096


class Proposal(BaseModel):
    proposal: str
    price: str
    deadline: str
    negotiation_floor: str
    notes: str


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


def load_pending() -> dict[str, dict]:
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text())
    return {}


def save_pending(pending: dict[str, dict]) -> None:
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2))


@contextmanager
def open_browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@contextmanager
def fresh_page(browser: Browser) -> Iterator[Page]:
    """A page in its own context, so no cookies carry over between loads."""
    context = browser.new_context(user_agent=USER_AGENT)
    try:
        yield context.new_page()
    finally:
        context.close()


@contextmanager
def open_page() -> Iterator[Page]:
    with open_browser() as browser, fresh_page(browser) as page:
        yield page


def fetch_projects() -> list[dict[str, str]]:
    projects: dict[str, dict[str, str]] = {}
    with open_browser() as browser:
        for search_url in WORKANA_URLS:
            for page_number in range(1, MAX_PAGES_PER_SEARCH + 1):
                page_url = f"{search_url}&page={page_number}"
                if not _fetch_listing_page(browser, page_url, page_number, projects):
                    break
            else:
                print(
                    f"Limite de {MAX_PAGES_PER_SEARCH} páginas atingido em {search_url}",
                    file=sys.stderr,
                )
    return list(projects.values())


def _fetch_listing_page(
    browser: Browser, url: str, page_number: int, projects: dict[str, dict[str, str]]
) -> bool:
    """Adds the page's jobs to `projects` (first listing wins) and says if a next page exists."""
    # A fresh context per page: Cloudflare challenges the second load in the same session,
    # and relaunching the whole browser instead costs ~1s more per page
    with fresh_page(browser) as page:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # A 24h search can legitimately come back empty; a Cloudflare block still times out
        page.locator(JOB_LINK_SELECTOR).or_(page.get_by_text(NO_RESULTS_TEXT)).first.wait_for(
            timeout=60_000
        )
        for link in page.query_selector_all(JOB_LINK_SELECTOR):
            href = link.get_attribute("href") or ""
            span = link.query_selector("span[title]")
            title = ((span.get_attribute("title") if span else None) or link.inner_text()).strip()
            if not href or not title:
                continue
            job_url = f"https://www.workana.com{href.split('?')[0]}"
            projects.setdefault(job_url, {"id": job_url, "title": title, "url": job_url})
        return page.query_selector(f"ul.pagination a[href$='page={page_number + 1}']") is not None


def fetch_description(url: str) -> str:
    """Returns the job detail block: budget, description, deadline and skills."""
    with open_page() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_selector(".block-detail", timeout=PAGE_TIMEOUT_MS)
        return page.inner_text(".block-detail").strip()


def load_prompt(key: str) -> str:
    return Fernet(key.encode()).decrypt(PROMPT_FILE.read_bytes()).decode()


def generate_proposal(
    client: anthropic.Anthropic, system: str, project: dict[str, str], description: str
) -> Proposal:
    response = client.messages.parse(
        model=PROPOSAL_MODEL,
        max_tokens=16000,
        output_config={"effort": "high"},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"<vaga>\nTítulo: {project['title']}\nLink: {project['url']}\n\n"
                    f"{description}\n</vaga>"
                ),
            }
        ],
        output_format=Proposal,
    )
    if response.parsed_output is None:
        raise RuntimeError(f"resposta sem proposta (stop_reason={response.stop_reason})")
    return response.parsed_output


def format_notification(
    project: dict[str, str], proposal: Proposal | None, error: str | None
) -> str:
    text = f"🆕 <b>{html.escape(project['title'])}</b>\n{project['url']}"
    if proposal:
        text += (
            f"\n\n💰 <b>Preço:</b> {html.escape(proposal.price)}"
            f"\n⏱ <b>Prazo:</b> {html.escape(proposal.deadline)}"
            f"\n🔻 <b>Piso:</b> {html.escape(proposal.negotiation_floor)}"
        )
        if proposal.notes:
            text += f"\n📝 {html.escape(proposal.notes)}"
    elif error:
        text += f"\n\n⚠️ Proposta não gerada: {html.escape(error)}"
    return text


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def matches_keywords(title: str, keywords: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(kw)}s?\b", title, re.IGNORECASE) for kw in keywords)


def send_telegram(token: str, chat_id: str, text: str, html_mode: bool = True) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if html_mode:
        payload["parse_mode"] = "HTML"
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    # The proposal deadline counts from here: the listing scrape eats the same job timeout
    started = time.monotonic()
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

    api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    prompt_key = env.get("PROMPT_KEY") or os.environ.get("PROMPT_KEY")
    client = system = None
    if api_key and prompt_key and PROMPT_FILE.exists():
        try:
            system = load_prompt(prompt_key)
            client = anthropic.Anthropic(
                api_key=api_key,
                timeout=PROPOSAL_TIMEOUT_SECONDS,
                max_retries=PROPOSAL_MAX_RETRIES,
            )
        except (InvalidToken, ValueError) as exc:
            print(f"PROMPT_KEY inválida, seguindo sem propostas: {exc!r}", file=sys.stderr)

    seen = load_seen()
    pending = load_pending()
    projects = fetch_projects()
    proposals_left = MAX_PROPOSALS_PER_RUN

    def save_state() -> None:
        save_seen(seen)
        save_pending(pending)

    # New matches are queued (and saved) before any work, so a run killed mid-way
    # cannot lose a job that has already left the listing page
    for project in projects:
        if project["id"] in seen or project["id"] in pending:
            continue
        if matches_keywords(project["title"], keywords):
            pending[project["id"]] = _pending_entry({**project, "attempts": 0})
        else:
            seen.add(project["id"])
    save_state()
    # dicts keep insertion order, so this is oldest first
    candidates = [{"id": pid, **job} for pid, job in pending.items()]

    sent = failed = deferred = 0
    try:
        for project in candidates:
            proposal = error = None
            if client and system:
                if proposals_left <= 0 or time.monotonic() - started > PROPOSAL_DEADLINE_SECONDS:
                    # Out of budget: stays pending instead of alerting without a proposal
                    deferred += 1
                    continue
                proposals_left -= 1
                try:
                    description = fetch_description(project["url"])
                    proposal = generate_proposal(client, system, project, description)
                except Exception as exc:  # noqa: BLE001
                    # Broad on purpose: a proposal error must never crash the run. Only the
                    # type is logged: Actions logs are public and a validation error
                    # would echo the model output.
                    print(
                        f"Falha na proposta de {project['url']}: {type(exc).__name__}",
                        file=sys.stderr,
                    )
                    project["attempts"] += 1
                    if project["attempts"] < MAX_PROPOSAL_ATTEMPTS:
                        pending[project["id"]] = _pending_entry(project)
                        save_state()
                        deferred += 1
                        continue
                    error = "erro ao ler a vaga ou falar com o Claude"
            try:
                send_telegram(token, chat_id, format_notification(project, proposal, error))
                if proposal:
                    # Plain text, alone in its message, so a long-press copies it whole
                    for chunk in split_message(proposal.proposal):
                        send_telegram(token, chat_id, chunk, html_mode=False)
            except requests.RequestException as exc:
                # Stays pending: the next run re-sends it, proposal included, rather
                # than leaving an alert whose proposal never arrived
                failed += 1
                pending[project["id"]] = _pending_entry(project)
                save_state()
                print(f"Falha ao enviar {project['url']}: {exc}", file=sys.stderr)
                continue
            seen.add(project["id"])
            pending.pop(project["id"], None)
            # Saved per job so a run killed mid-way does not re-send what already went out
            save_state()
            sent += 1
    finally:
        save_state()

    print(
        f"{len(projects)} projetos lidos, {sent} enviados, "
        f"{deferred} adiados, {failed} com falha."
    )
    if failed:
        sys.exit(1)


def _pending_entry(project: dict) -> dict:
    return {"title": project["title"], "url": project["url"], "attempts": project["attempts"]}

if __name__ == "__main__":
    main()
