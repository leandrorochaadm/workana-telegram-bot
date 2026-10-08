#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "playwright==1.63.0",
#   "pydantic",
#   "requests",
# ]
# ///
"""Monitora projetos novos na Workana filtrados por palavras-chave e avisa no Telegram."""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import requests
from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
# Checkout of the private repo leandrorochaadm/proposta: state and triage prompt stay
# out of this public repo
PROPOSTA_DIR = BASE_DIR / "proposta"
DATA_DIR = PROPOSTA_DIR / "data"
SEEN_FILE = DATA_DIR / "seen.json"
# Matched jobs not alerted yet: {id: {"title", "url", "attempts"}}
PENDING_FILE = DATA_DIR / "pending.json"
# Jobs the triage turned down, kept to tune the triage prompt: {id: {"title", "url", "date"}}
REJECTED_FILE = DATA_DIR / "rejected.json"
# What the developer takes and turns down; read on every run, so editing it needs no deploy
FIT_PROMPT_FILE = PROPOSTA_DIR / "prompts" / "fit_prompt.md"
# Listing that timed out (screenshot + HTML), uploaded by the workflow
# to tell a Cloudflare block from a layout change
DEBUG_DIR = BASE_DIR / "debug"

# Jobs from the last 24h; a job listed by both searches is kept once
WORKANA_URLS = (
    "https://www.workana.com/jobs?language=pt&publication=1d&query=aplicativo&region=029%2C013%2C005",
    "https://www.workana.com/jobs?language=pt&publication=1d&query=app&region=029%2C013%2C005",
)
JOB_LINK_SELECTOR = "a[href^='/job/']"
# The site language follows the exit IP too (WARP may land in a Spanish-speaking region)
NO_RESULTS_TEXT = re.compile(r"Não foram encontrados projetos|No hay proyectos")
# Logged-out listings show 7 jobs per page, sorted by relevance, not by date
MAX_PAGES_PER_SEARCH = 5
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Cheap yes-or-no pass: only jobs that fit the work offered go on
FIT_MODEL = "claude-haiku-4-5-20251001"
FIT_TIMEOUT_SECONDS = 60
PAGE_TIMEOUT_MS = 30_000
# Worst case must fit the workflow's 14-min timeout: ~1.5 min of setup, up to 2 min
# connecting WARP, the deadline below, then one last job (page + Haiku triage) and
# the commit
PROPOSAL_DEADLINE_SECONDS = 5 * 60
# A job whose page keeps failing is sent without a proposal after this many tries,
# so it cannot cost a page load every run forever
MAX_PROPOSAL_ATTEMPTS = 3
TELEGRAM_MAX_CHARS = 4096
# Covers a short Telegram hiccup before the job waits for the next run
TELEGRAM_SEND_ATTEMPTS = 3
TELEGRAM_RETRY_SECONDS = 2


class ProposalError(Exception):
    """A failure whose message is safe for public logs: it never holds model output."""


class JobFit(BaseModel):
    """The Haiku triage answer: whether the job is worth a proposal."""

    is_match: bool


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


def load_rejected() -> dict[str, dict]:
    if REJECTED_FILE.exists():
        return json.loads(REJECTED_FILE.read_text())
    return {}


def save_rejected(rejected: dict[str, dict]) -> None:
    REJECTED_FILE.write_text(json.dumps(rejected, ensure_ascii=False, indent=2))


def load_fit_prompt() -> str | None:
    """The triage criteria from the proposta checkout; None when missing or blank."""
    if not FIT_PROMPT_FILE.exists():
        return None
    # The file ends in a newline the old literal did not have
    return FIT_PROMPT_FILE.read_text().strip() or None


@contextmanager
def open_browser() -> Iterator[Browser]:
    # Set by the workflow to WARP's local SOCKS proxy: Cloudflare blocks the runners' datacenter IPs
    proxy = os.environ.get("BROWSER_PROXY")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        try:
            yield browser
        finally:
            browser.close()


@contextmanager
def fresh_page(browser: Browser) -> Iterator[Page]:
    """A page in its own context, so no cookies carry over between loads."""
    context = browser.new_context(user_agent=USER_AGENT, locale="pt-BR")
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
        try:
            page.locator(JOB_LINK_SELECTOR).or_(page.get_by_text(NO_RESULTS_TEXT)).first.wait_for(
                timeout=60_000
            )
        except PlaywrightTimeoutError:
            _dump_page(page)
            raise
        for link in page.query_selector_all(JOB_LINK_SELECTOR):
            href = link.get_attribute("href") or ""
            span = link.query_selector("span[title]")
            title = ((span.get_attribute("title") if span else None) or link.inner_text()).strip()
            if not href or not title:
                continue
            job_url = f"https://www.workana.com{href.split('?')[0]}"
            projects.setdefault(job_url, {"id": job_url, "title": title, "url": job_url})
        return page.query_selector(f"ul.pagination a[href$='page={page_number + 1}']") is not None


def _dump_page(page: Page) -> None:
    """Saves what the browser was showing; never masks the original error."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        page.screenshot(path=DEBUG_DIR / f"listing-{stamp}.png", full_page=True)
        (DEBUG_DIR / f"listing-{stamp}.html").write_text(
            f"<!-- {page.url} | title: {page.title()} -->\n{page.content()}"
        )
    except Exception as error:
        print(f"Não foi possível salvar a página para diagnóstico: {error}", file=sys.stderr)


def fetch_description(url: str) -> str:
    """Returns the job detail block: budget, description, deadline and skills."""
    with open_page() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_selector(".block-detail", timeout=PAGE_TIMEOUT_MS)
        return page.inner_text(".block-detail").strip()


def job_content(project: dict[str, str], description: str) -> str:
    return f"<vaga>\nTítulo: {project['title']}\nLink: {project['url']}\n\n{description}\n</vaga>"


def check_fit(
    oauth_token: str, fit_prompt: str, project: dict[str, str], description: str
) -> JobFit:
    """Asks Haiku whether the job is an app or web system, before any proposal work."""
    output = call_claude(
        oauth_token,
        fit_prompt,
        job_content(project, description),
        model=FIT_MODEL,
        schema=JobFit,
        timeout=FIT_TIMEOUT_SECONDS,
    )
    return JobFit.model_validate(output)


def call_claude(
    oauth_token: str,
    system: str,
    content: str,
    *,
    model: str,
    schema: type[BaseModel],
    timeout: int,
    effort: str | None = None,
) -> dict:
    """Runs Claude Code in print mode, billed to the Max plan behind `oauth_token`.

    Returns the structured output, still unvalidated.
    """
    # Without the API key, the CLI cannot fall back to pay-per-use billing
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    # The triage is a plain yes or no, so it runs at the model's default effort
    effort_args = ["--effort", effort] if effort else []
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model", model,
            *effort_args,
            "--system-prompt", system,
            # A plain answer: no tools, no settings, no saved session
            "--tools", "",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(schema.model_json_schema()),
        ],
        input=content,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        # Outside the repo, so no CLAUDE.md is picked up as context
        cwd=tempfile.gettempdir(),
        check=False,
    )
    # The CLI prints its JSON result on failure too, e.g. an expired token's 401
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = {}
    if result.returncode != 0 or output.get("is_error") or output.get("structured_output") is None:
        raise ProposalError(
            f"claude saiu com código {result.returncode} (subtype={output.get('subtype')}, "
            f"api_error_status={output.get('api_error_status')})"
        )
    return output["structured_output"]


def format_notification(project: dict[str, str], error: str | None) -> str:
    text = f"🆕 <b>{html.escape(project['title'])}</b>\n{project['url']}"
    if error:
        text += f"\n\n⚠️ Proposta não gerada: {html.escape(error)}"
    return text


def telegram_len(html_text: str) -> int:
    """Length Telegram checks against its limit: after parsing, so tags and entities shrink."""
    return utf16_len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def utf16_len(text: str) -> int:
    """Length as Telegram counts it: an emoji such as 🆕 takes two units."""
    return len(text.encode("utf-16-le")) // 2


def _utf16_prefix(text: str, limit: int) -> int:
    """How many characters of text fit in limit UTF-16 code units.

    Telegram counts message length like JavaScript does: an emoji such as 🆕
    takes two units, while Python's len() counts it as one.
    """
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return index
    return len(text)


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    if telegram_len(text) <= limit:
        return [text]
    # Past the limit, chunks are measured on the raw HTML: a bit short, never too long
    chunks: list[str] = []
    while (fit := _utf16_prefix(text, limit)) < len(text):
        # The text is HTML: cutting at a space never splits a tag or an entity like &amp;
        cut = text.rfind("\n", 0, fit)
        if cut <= 0:
            cut = text.rfind(" ", 0, fit)
        if cut <= 0:
            # At least one character, or a limit smaller than an emoji loops forever
            cut = max(fit, 1)
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def matches_keywords(title: str, keywords: list[str]) -> bool:
    # A space in a keyword also matches a hyphen or nothing: "no code", "no-code", "nocode"
    patterns = (re.escape(kw).replace(r"\ ", r"[\s-]?") for kw in keywords)
    return any(re.search(rf"\b{pattern}s?\b", title, re.IGNORECASE) for pattern in patterns)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=15,
    )
    resp.raise_for_status()


def send_with_retry(token: str, chat_id: str, text: str) -> None:
    """send_telegram, tried again on network errors, rate limits and server errors."""
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        try:
            send_telegram(token, chat_id, text)
            return
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            # Any other 4xx is a bad request: sending it again gives the same answer
            permanent = status is not None and status < 500 and status != 429
            if permanent or attempt == TELEGRAM_SEND_ATTEMPTS:
                raise
            time.sleep(TELEGRAM_RETRY_SECONDS)


def main() -> None:
    # The triage deadline counts from here: the listing scrape eats the same job timeout
    started = time.monotonic()
    env = load_env()
    token = env.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    keywords_raw = env.get("KEYWORDS") or os.environ.get("KEYWORDS", "")
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    excluded_raw = env.get("EXCLUDE_KEYWORDS") or os.environ.get("EXCLUDE_KEYWORDS", "")
    excluded = [k.strip() for k in excluded_raw.split(",") if k.strip()]

    if not token or not chat_id:
        print("Faltam TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID no .env", file=sys.stderr)
        sys.exit(1)
    if not keywords:
        print("Nenhuma keyword configurada em KEYWORDS no .env", file=sys.stderr)
        sys.exit(1)
    if not DATA_DIR.is_dir():
        # Without seen.json every job in the listing would be alerted again
        print(
            "Pasta proposta/data não encontrada: o checkout do repositório proposta falhou",
            file=sys.stderr,
        )
        sys.exit(1)

    oauth_token = env.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    fit_prompt = load_fit_prompt()
    # Without any of the three every keyword match is alerted, as on a local run
    can_triage = False
    if oauth_token:
        if fit_prompt is None:
            print("Falta proposta/prompts/fit_prompt.md, seguindo sem triagem", file=sys.stderr)
        elif shutil.which("claude") is None:
            print("Claude Code não instalado, seguindo sem triagem", file=sys.stderr)
        else:
            can_triage = True

    seen = load_seen()
    pending = load_pending()
    rejected = load_rejected()
    projects = fetch_projects()

    def save_state() -> None:
        save_seen(seen)
        save_pending(pending)
        save_rejected(rejected)

    # New matches are queued (and saved) before any work, so a run killed mid-way
    # cannot lose a job that has already left the listing page
    for project in projects:
        if project["id"] in seen or project["id"] in pending:
            continue
        if matches_keywords(project["title"], keywords):
            pending[project["id"]] = _pending_entry({**project, "attempts": 0})
        else:
            seen.add(project["id"])
    # Also drops jobs queued before a word was excluded
    for pid, job in list(pending.items()):
        if matches_keywords(job["title"], excluded):
            pending.pop(pid)
            seen.add(pid)
    save_state()
    # dicts keep insertion order, so this is oldest first
    candidates = [{"id": pid, **job} for pid, job in pending.items()]

    sent = failed = deferred = turned_down = 0
    try:
        for project in candidates:
            error = None
            off_profile = False
            if can_triage:
                if time.monotonic() - started > PROPOSAL_DEADLINE_SECONDS:
                    # Out of time: stays pending instead of alerting without the triage
                    deferred += 1
                    continue
                try:
                    description = fetch_description(project["url"])
                    off_profile = not triage(oauth_token, fit_prompt, project, description)
                except Exception as exc:  # noqa: BLE001
                    # Broad on purpose: a page error must never crash the run. Actions
                    # logs are public, so only ProposalError, built to be safe, is logged
                    # in full.
                    detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
                    print(f"Falha ao ler a vaga {project['url']}: {detail}", file=sys.stderr)
                    project["attempts"] += 1
                    if project["attempts"] < MAX_PROPOSAL_ATTEMPTS:
                        pending[project["id"]] = _pending_entry(project)
                        save_state()
                        deferred += 1
                        continue
                    error = "erro ao ler a vaga"
            if off_profile:
                # Silently dropped: no Telegram alert for a rejected job
                rejected[project["id"]] = {
                    "title": project["title"],
                    "url": project["url"],
                    "date": datetime.now(UTC).date().isoformat(),
                }
                seen.add(project["id"])
                pending.pop(project["id"], None)
                save_state()
                turned_down += 1
                continue
            try:
                for chunk in split_message(format_notification(project, error)):
                    send_with_retry(token, chat_id, chunk)
            except requests.RequestException as exc:
                failed += 1
                print(f"Falha ao enviar {project['url']}: {exc}", file=sys.stderr)
                # The next run sends the whole alert again
                pending[project["id"]] = _pending_entry(project)
                save_state()
                continue
            seen.add(project["id"])
            pending.pop(project["id"], None)
            # Saved per job so a run killed mid-way does not re-send what already went out
            save_state()
            sent += 1
    finally:
        save_state()

    print(
        f"{len(projects)} projetos lidos, {sent} enviados, {turned_down} rejeitados na triagem, "
        f"{deferred} adiados, {failed} com falha."
    )
    if failed:
        sys.exit(1)


def triage(
    oauth_token: str, fit_prompt: str, project: dict[str, str], description: str
) -> bool:
    """Whether the job deserves a proposal.

    Fails open: a triage error must not cost a job that could fit.
    """
    try:
        return check_fit(oauth_token, fit_prompt, project, description).is_match
    except Exception as exc:  # noqa: BLE001
        # Same rule as the proposal: only ProposalError is safe for the public logs
        detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
        print(f"Triagem falhou em {project['url']}, seguindo como aprovada: {detail}", file=sys.stderr)
        return True


def _pending_entry(project: dict) -> dict:
    return {"title": project["title"], "url": project["url"], "attempts": project["attempts"]}


if __name__ == "__main__":
    main()
