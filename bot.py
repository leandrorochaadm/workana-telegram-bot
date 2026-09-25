#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cryptography",
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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from playwright.sync_api import Browser, Page, sync_playwright
from pydantic import BaseModel

import preco
import varredura

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
# Keeps a burst of new jobs from blowing the workflow timeout and the Max usage limit
MAX_PROPOSALS_PER_RUN = 5
# Worst case must fit the workflow's 14-min timeout: ~1.5 min of setup, the
# deadline below, then one last proposal (job page + one Claude call) and the commit.
# Revisions only start before the deadline, so they never add a call past it.
PROPOSAL_TIMEOUT_SECONDS = 180
PAGE_TIMEOUT_MS = 30_000
PROPOSAL_DEADLINE_SECONDS = 8 * 60
# A job that keeps failing (refusal, page gone) is sent without a proposal after
# this many tries, so it cannot bill every run forever
MAX_PROPOSAL_ATTEMPTS = 3
# Extra Claude calls per proposal to remove what the scan flagged as an error
MAX_REVISIONS = 2
TELEGRAM_MAX_CHARS = 4096


class ProposalError(Exception):
    """A failure whose message is safe for public logs: it never holds model output."""


class ProposalDraft(BaseModel):
    """What the model returns: the text with price placeholders, plus the estimate."""

    proposal: str
    dev_hours: int
    screens: int
    notes: str


class Proposal(BaseModel):
    proposal: str
    price: str
    deadline: str
    negotiation_floor: str
    notes: str
    # varredura.py findings on the final text, for a manual fix before pasting
    review: list[str] = []


# The model estimates hours and writes these markers; preco.py does the arithmetic,
# since the model gets it wrong and the rates stay out of the prompt
PRICE_PLACEHOLDERS = ("{{PRECO}}", "{{PRAZO}}", "{{COBRANCA}}", "{{REGUA}}")
PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z_]+\}\}")
# The prompt mandates "Pelo que está escrito, fecho em", as the skill's own examples do
IGNORED_REVIEW_RULES = {"compromisso antes das perguntas"}
# Links are only a style alert for direct clients, but they get the Workana account suspended
WORKANA_BLOCKING_RULES = ("endereço no texto",)
REVIEW_EXCERPT_CHARS = 60
# What Workana's filter blocks and varredura.py (written for direct clients) allows
WORKANA_FORBIDDEN = (
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "e-mail no texto"),
    (r"\(?\b\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}\b", "telefone no texto"),
    (r"\b[\w-]+(\.[\w-]+)*\.(com|net|org|dev|app|io|me|site|br)\b", "endereço de site no texto"),
    (
        # Narrow on purpose: "call center", "app de reuniões" or "login com Facebook"
        # can be the client's own scope
        r"\b((fazer|marcar|marcamos|uma|numa|em) (call|reuni[ãa]o|chamada)|v[íi]deo ?chamada|"
        r"chamada de v[íi]deo|conversar por v[íi]deo|me chama|me chame|entr(e|ar) em contato|"
        r"fal(a|e|ar) comigo|skype|zoom|google meet|me (segue|siga|encontra|acha) n[oa])\b",
        "convite para conversa ou rede social",
    ),
)
# Wording the scan flags as an error, spelled out in the prompt so the first draft
# avoids it and the scan stays a safety net. test_bot keeps it in sync with the scan
FORBIDDEN_WORDING = (
    (
        "Conectores com cara de IA",
        (
            "além disso", "portanto", "dessa forma", "desta forma", "em suma", "vale ressaltar",
            "é importante notar", "é importante destacar", "é importante ressaltar",
            "não apenas X, mas também Y", "no mundo de hoje", "solução robusta",
        ),
    ),
    (
        "Palavras vetadas, em qualquer forma",
        (
            "taxa", "taxas", "WhatsApp", "whats", "zap", "comissão", "comissionamento", "Pix",
            "ligar", "ligo", "ligue", "liguei", "ligamos", "ligando", "ligaremos", "ligarei",
        ),
    ),
    (
        "Promessas de exclusividade",
        (
            "só no seu projeto", "só ao seu projeto", "dedicação exclusiva", "exclusivamente no seu",
            "sem dividir com outro", "não pego outro projeto", "não pego outro cliente",
            "um projeto por vez", "um cliente por vez",
        ),
    ),
    (
        "Convites para conversa fora da Workana",
        (
            "fazer uma call", "marcar uma reunião", "numa chamada", "videochamada",
            "chamada de vídeo", "conversar por vídeo", "me chama", "me chame", "entre em contato",
            "fale comigo", "Skype", "Zoom", "Google Meet", "me segue no",
        ),
    ),
)
FORBIDDEN_RULES = (
    "Sem travessão (—) nem meia risca (–): use vírgula, dois-pontos ou ponto.",
    "Sem emoji, negrito ou itálico (nada de * ou **).",
    "Sem e-mail, telefone, link, domínio (.com, .br, .app...), GitHub ou Behance.",
    "Sem número de telas, horas ou funcionalidades (ex.: \"8 telas\", \"três funcionalidades\"); "
    "a única exceção é o prazo de resposta (\"respondo em até duas horas\").",
    "A frase do pagamento que diz que algo é cobrado depois da entrega nomeia a entrada no "
    "mesmo parágrafo.",
    "A promessa de versão toda semana ou toda sexta vem ancorada na mesma frase: \"Dentro da "
    "fase, toda semana...\" ou \"depois que você aprovar\".",
    "Não prometa acesso, senha ou credencial ao cliente desde o começo ou durante o projeto, "
    "e não explique quando os acessos são entregues.",
)


def forbidden_wording_prompt() -> str:
    """The scan's error rules as a prompt section, so Claude skips them up front."""
    lines = [
        "<proibicoes>",
        "Uma varredura automática reprova a proposta que tiver qualquer item abaixo. "
        "Não use nenhum deles, nem em outra forma ou flexão.",
    ]
    for title, terms in FORBIDDEN_WORDING:
        lines.append(f"- {title}: " + "; ".join(f'"{term}"' for term in terms) + ".")
    lines.extend(f"- {rule}" for rule in FORBIDDEN_RULES)
    lines.append("</proibicoes>")
    return "\n".join(lines)


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


def configure_pricing(hourly_rate: str | None, min_project: str | None) -> bool:
    """Loads the private rates into preco.py; False when either is missing or invalid."""
    try:
        rate, minimum = float(hourly_rate or ""), float(min_project or "")
    except ValueError:
        return False
    if rate <= 0 or minimum <= 0:
        return False
    preco.VALOR_HORA, preco.MINIMO_PROJETO = rate, minimum
    return True


def client_deadline(r: preco.Resultado) -> str:
    """The deadline as the client reads it: both units, spelled out."""
    weeks = r.fases - 1
    weeks_text = "uma semana" if weeks == 1 else f"{preco.extenso(weeks)} semanas"
    return (
        f"{preco.extenso_masculino(r.dias_fase_1)} dias para fechar o projeto no papel "
        f"e {weeks_text} de desenvolvimento"
    )


def workana_findings(text: str) -> list[varredura.Achado]:
    return [
        varredura.Achado(
            varredura.ERRO,
            rule,
            "A Workana barra contato e convite para conversa fora do texto, e suspende a conta. "
            "Corte o trecho; a prova do trabalho é o vídeo em anexo ou o nome do app na loja.",
            varredura.contexto(text, pattern),
        )
        for pattern, rule in WORKANA_FORBIDDEN
        if re.search(pattern, text, re.IGNORECASE)
    ]


def scan_proposal(text: str) -> list[varredura.Achado]:
    """varredura.py's text checks, adjusted for Workana, errors first."""
    findings = workana_findings(varredura.desdobra(text))
    for finding in varredura.varre(text, None):
        if finding.regra in IGNORED_REVIEW_RULES:
            continue
        if finding.regra.startswith(WORKANA_BLOCKING_RULES):
            finding.nivel = varredura.ERRO
        findings.append(finding)
    return sorted(findings, key=lambda finding: finding.nivel != varredura.ERRO)


def review_proposal(text: str) -> list[str]:
    """The scan as short labels for the Telegram alert."""
    labels = []
    for finding in scan_proposal(text):
        label = f"{'ERRO' if finding.nivel == varredura.ERRO else 'alerta'}: {finding.regra}"
        if finding.trecho:
            label += f" ({finding.trecho[:REVIEW_EXCERPT_CHARS].strip()})"
        labels.append(label)
    return labels


def proposal_problems(draft: ProposalDraft) -> tuple[bool, list[str]]:
    """Says if the draft can be priced, and lists what must leave the text for Claude."""
    try:
        proposal = price_proposal(draft)
    except ProposalError as exc:
        return False, [f"{exc}. Siga a regra dos quatro marcadores do sistema."]
    problems = []
    for finding in scan_proposal(proposal.proposal):
        if finding.nivel != varredura.ERRO:
            continue
        problem = f"{finding.regra}: {finding.detalhe}"
        if finding.trecho:
            problem += f' Trecho: "{finding.trecho}"'
        problems.append(problem)
    return True, problems


def revision_request(draft: ProposalDraft, problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        "\n\n<revisao>\n"
        "Sua proposta anterior está em <proposta_anterior>. A varredura automática achou estes "
        "problemas, e cada um precisa sair do texto:\n"
        f"{listed}\n\n"
        "Reescreva só o necessário para eliminar todos eles, mantendo o resto do texto, as regras "
        "do sistema e os quatro marcadores. Os trechos citados mostram o texto já com os "
        "marcadores trocados pelos valores. Devolva o resultado completo, com as mesmas horas e "
        "telas, a não ser que algum problema exija mudar.\n"
        f"</revisao>\n\n<proposta_anterior>\n{draft.proposal}\n</proposta_anterior>"
    )


def price_proposal(draft: ProposalDraft) -> Proposal:
    """Fills the draft's placeholders with preco.py's numbers and phrases."""
    found = PLACEHOLDER_PATTERN.findall(draft.proposal)
    markers = set(found)
    if draft.dev_hours <= 0:
        # Too vague to price: the proposal promises the number after the answers
        if markers:
            raise ProposalError("proposta sem estimativa, mas com marcador de preço")
        return Proposal(
            proposal=draft.proposal,
            price="a definir após as respostas",
            deadline="a definir",
            negotiation_floor="a definir",
            notes=draft.notes,
            review=review_proposal(draft.proposal),
        )
    if draft.screens <= 0:
        raise ProposalError(f"estimativa sem telas ({draft.dev_hours} h)")
    # Each exactly once: a repeated marker would print the price twice
    if sorted(found) != sorted(PRICE_PLACEHOLDERS):
        raise ProposalError(f"marcadores de preço errados: {sorted(found)}")

    r = preco.calcula(horas_dev=draft.dev_hours, telas=draft.screens)
    text = draft.proposal
    for marker, value in zip(
        PRICE_PLACEHOLDERS,
        (preco.brl0(r.preco), client_deadline(r), preco.frase_da_cobranca(r), preco.frase_da_regua(r)),
    ):
        text = text.replace(marker, value)

    installments = r.parcelas
    price = f"{preco.brl0(r.preco)}: entrada de {preco.brl0(installments[0].valor)}"
    if len(installments) > 1:
        price += f" e mais {len(installments) - 1} de {preco.brl0(installments[1].valor)}"
    notes = [draft.notes.strip().rstrip(".")] if draft.notes.strip() else []
    notes.append(f"{draft.dev_hours} h de dev, {draft.screens} telas")
    if r.preco < preco.MINIMO_PROJETO:
        notes.append(f"abaixo do mínimo de {preco.brl0(preco.MINIMO_PROJETO)}")
    if r.fases - 1 > preco.SEMANAS_PROJETO_LONGO:
        notes.append("projeto longo: considere propor só a primeira metade do escopo")
    return Proposal(
        proposal=text,
        price=price,
        deadline=preco.prazo_texto(r.fases, r.dias_fase_1),
        negotiation_floor=preco.brl0(r.piso),
        notes=". ".join(notes),
        review=review_proposal(text),
    )


def load_prompt(key: str) -> str:
    return Fernet(key.encode()).decrypt(PROMPT_FILE.read_bytes()).decode()


def generate_proposal(
    oauth_token: str,
    system: str,
    project: dict[str, str],
    description: str,
    can_revise: Callable[[], bool] = lambda: True,
) -> Proposal:
    """Drafts the proposal, then asks Claude to fix what the scan flags, while time allows.

    Whatever is left after the last revision still goes out, listed in the alert.
    """
    job = f"<vaga>\nTítulo: {project['title']}\nLink: {project['url']}\n\n{description}\n</vaga>"
    draft = run_claude(oauth_token, system, job)
    # The latest draft whose markers are right: a revision that breaks them, or a
    # revision call that fails, must not throw away a proposal that could go out
    usable = None
    for _ in range(MAX_REVISIONS):
        priceable, problems = proposal_problems(draft)
        if not problems or not can_revise():
            break
        if priceable:
            usable = draft
        try:
            draft = run_claude(oauth_token, system, job + revision_request(draft, problems))
        except Exception as exc:  # noqa: BLE001
            if usable is None:
                raise
            detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
            print(f"Revisão falhou, seguindo com a versão anterior: {detail}", file=sys.stderr)
            return price_proposal(usable)
    try:
        return price_proposal(draft)
    except ProposalError:
        if usable is None:
            raise
        return price_proposal(usable)


def run_claude(oauth_token: str, system: str, content: str) -> ProposalDraft:
    """Runs Claude Code in print mode, billed to the Max plan behind `oauth_token`."""
    # Without the API key, the CLI cannot fall back to pay-per-use billing
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--model", PROPOSAL_MODEL,
            "--effort", "high",
            "--system-prompt", system,
            # A plain answer: no tools, no settings, no saved session
            "--tools", "",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(ProposalDraft.model_json_schema()),
        ],
        input=content,
        capture_output=True,
        text=True,
        timeout=PROPOSAL_TIMEOUT_SECONDS,
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
    return ProposalDraft.model_validate(output["structured_output"])


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
        if proposal.review:
            text += "\n\n🔎 <b>Varredura:</b>" + "".join(
                f"\n• {html.escape(item)}" for item in proposal.review
            )
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
    # A space in a keyword also matches a hyphen or nothing: "no code", "no-code", "nocode"
    patterns = (re.escape(kw).replace(r"\ ", r"[\s-]?") for kw in keywords)
    return any(re.search(rf"\b{pattern}s?\b", title, re.IGNORECASE) for pattern in patterns)


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
    excluded_raw = env.get("EXCLUDE_KEYWORDS") or os.environ.get("EXCLUDE_KEYWORDS", "")
    excluded = [k.strip() for k in excluded_raw.split(",") if k.strip()]

    if not token or not chat_id:
        print("Faltam TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID no .env", file=sys.stderr)
        sys.exit(1)
    if not keywords:
        print("Nenhuma keyword configurada em KEYWORDS no .env", file=sys.stderr)
        sys.exit(1)

    oauth_token = env.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    prompt_key = env.get("PROMPT_KEY") or os.environ.get("PROMPT_KEY")
    system = None
    if oauth_token and prompt_key and PROMPT_FILE.exists():
        pricing_ok = configure_pricing(
            env.get("PRICE_HOURLY_RATE") or os.environ.get("PRICE_HOURLY_RATE"),
            env.get("PRICE_MIN_PROJECT") or os.environ.get("PRICE_MIN_PROJECT"),
        )
        if not pricing_ok:
            print(
                "Faltam PRICE_HOURLY_RATE e/ou PRICE_MIN_PROJECT válidos, seguindo sem propostas",
                file=sys.stderr,
            )
        elif shutil.which("claude") is None:
            print("Claude Code não instalado, seguindo sem propostas", file=sys.stderr)
        else:
            try:
                system = load_prompt(prompt_key) + "\n\n" + forbidden_wording_prompt()
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
    # Also drops jobs queued before a word was excluded
    for pid, job in list(pending.items()):
        if matches_keywords(job["title"], excluded):
            pending.pop(pid)
            seen.add(pid)
    save_state()
    # dicts keep insertion order, so this is oldest first
    candidates = [{"id": pid, **job} for pid, job in pending.items()]

    sent = failed = deferred = 0
    try:
        for project in candidates:
            proposal = error = None
            if system:
                if proposals_left <= 0 or time.monotonic() - started > PROPOSAL_DEADLINE_SECONDS:
                    # Out of budget: stays pending instead of alerting without a proposal
                    deferred += 1
                    continue
                proposals_left -= 1
                try:
                    description = fetch_description(project["url"])
                    proposal = generate_proposal(
                        oauth_token,
                        system,
                        project,
                        description,
                        can_revise=lambda: time.monotonic() - started <= PROPOSAL_DEADLINE_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Broad on purpose: a proposal error must never crash the run. Actions
                    # logs are public and a validation error would echo the model output,
                    # so only ProposalError, built to be safe, is logged in full.
                    detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
                    print(f"Falha na proposta de {project['url']}: {detail}", file=sys.stderr)
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
