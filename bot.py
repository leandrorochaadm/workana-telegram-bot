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
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from playwright.sync_api import Browser, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

import preco
import varredura

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
SEEN_FILE = BASE_DIR / "seen.json"
# Matched jobs still waiting for a proposal: {id: {"title", "url", "attempts"}}
PENDING_FILE = BASE_DIR / "pending.json"
# Jobs the triage turned down, kept to tune FIT_PROMPT: {id: {"title", "url", "date"}}
REJECTED_FILE = BASE_DIR / "rejected.json"
# Encrypted because the repo is public and the prompt holds private pricing rules
PROMPT_FILE = BASE_DIR / "proposal_prompt.enc"
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

PROPOSAL_MODEL = "claude-opus-5-5"
# Cheap first pass so the Opus call is spent only on jobs that fit the work offered
FIT_MODEL = "claude-haiku-4-5-20251001"
FIT_TIMEOUT_SECONDS = 60
FIT_PROMPT = """Você faz a triagem de vagas da Workana para um desenvolvedor freelancer.
Ele aceita vagas para desenvolver software com telas: aplicativo mobile (Android, iOS,
multiplataforma) ou sistema web (SaaS, painel, plataforma, área logada), do zero ou evoluindo
um que já existe.

Ele não aceita: landing page, site institucional, blog, loja montada em plataforma pronta
(Shopify, WordPress, Wix, Nuvemshop), só design ou protótipo (UI/UX, Figma), bot, automação,
integração ou scraping sem telas, planilha, tráfego pago, marketing, conteúdo, vídeo, suporte
de TI, vaga de emprego fixo ou revenda de app pronto.

Leia a vaga e responda só sim ou não: ela dá match? Na dúvida, quando a vaga pode ser um app
ou sistema com telas, responda sim."""
# Keeps a burst of new jobs from blowing the workflow timeout and the Max usage limit
MAX_PROPOSALS_PER_RUN = 5
# Worst case must fit the workflow's 14-min timeout: ~1.5 min of setup, up to 2 min
# connecting WARP, the deadline below, then one last job (page + Haiku triage + one
# Opus call) and the commit.
# Revisions only start before the deadline, so they never add a call past it.
PROPOSAL_TIMEOUT_SECONDS = 180
PAGE_TIMEOUT_MS = 30_000
PROPOSAL_DEADLINE_SECONDS = 5 * 60
# A job that keeps failing (refusal, page gone) is sent without a proposal after
# this many tries, so it cannot bill every run forever
MAX_PROPOSAL_ATTEMPTS = 3
# Extra Claude calls per proposal to remove what the scan flagged as an error
MAX_REVISIONS = 1
TELEGRAM_MAX_CHARS = 4096
# The proposal goes out inside the alert, which must fit one Telegram message. The
# real check measures the whole alert, so the proposal takes all the room the header
# (title, price, notes, scan) leaves; the revision loop shortens what goes over and
# the code never trims the text. These replace varredura.py's 600-800 word range
PROPOSAL_MIN_CHARS = 2800
# The prompt's ceiling: most headers take 500 to 700 characters
PROPOSAL_TARGET_CHARS = 3400
# Average of the skill's own texts, space included
CHARS_PER_WORD = 5.6
# Only a guide for Claude, which estimates words far better than characters: at
# CHARS_PER_WORD, plus the greeting, this range lands inside the target
PROPOSAL_WORDS_HINT = (470, 570)
# Asked on top of the overflow, since Claude cuts by eye and tends to fall short
REVISION_MARGIN_CHARS = 150


class ProposalError(Exception):
    """A failure whose message is safe for public logs: it never holds model output."""


class ProposalTooLong(ProposalError):
    """The proposal does not fit one Telegram message, even after the revisions."""


class JobFit(BaseModel):
    """The Haiku triage answer: whether the job is worth a proposal."""

    is_match: bool


class ProposalDraft(BaseModel):
    """What the model returns: the text with price and hour placeholders, plus the estimate."""

    proposal: str
    dev_hours: int
    # Parts of dev_hours shown as steps; the tests step takes the rest, rounding included
    build_hours: int
    backend_hours: int
    store_hours: int
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
# The six steps of the proposal, in the skill's order, plus their sum. A step with
# no hours leaves the text, so its marker must be absent
HOUR_PLACEHOLDERS = (
    "{{HORAS_PAPEL}}",
    "{{HORAS_DESENHO}}",
    "{{HORAS_CONSTRUCAO}}",
    "{{HORAS_BASTIDORES}}",
    "{{HORAS_TESTES}}",
    "{{HORAS_LOJA}}",
    "{{HORAS_TOTAL}}",
)
PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z_]+\}\}")
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
            "não apenas X, mas também Y", "no mundo de hoje", "solução robusta", "por fim",
            "ademais", "sendo assim",
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
    "Sem número de telas ou de funcionalidades (ex.: \"8 telas\", \"três funcionalidades\").",
    "Horas só as das etapas, pelos marcadores, e o prazo de resposta (\"respondo em até duas "
    "horas\"); nenhuma outra conta de horas, nem horas por semana.",
    "Cada parágrafo numa linha só, com uma linha em branco entre eles, sem título, marcador nem "
    "lista numerada.",
    "O primeiro parágrafo é o cumprimento fixo, literal e sozinho. Ele é a única exceção aos "
    "convites acima: o \"é só me chamar\" dele fica como está.",
    "A frase do pagamento que diz que algo é cobrado depois da entrega nomeia a entrada no "
    "mesmo parágrafo.",
    "A promessa de versão toda semana ou toda sexta vem ancorada na mesma frase: \"Dentro da "
    "fase, toda semana...\" ou \"depois que você aprovar\".",
    "Não prometa acesso, senha ou credencial ao cliente desde o começo ou durante o projeto, "
    "e não explique quando os acessos são entregues.",
    f"Tamanho: o texto inteiro, já com preço e horas, tem entre {PROPOSAL_MIN_CHARS} e "
    f"{PROPOSAL_TARGET_CHARS} caracteres contando espaços, para caber numa mensagem só. Use o "
    "espaço: perto do teto é melhor que perto do piso. Como "
    f"referência, isso dá cerca de {PROPOSAL_WORDS_HINT[0]} a {PROPOSAL_WORDS_HINT[1]} palavras "
    "depois do cumprimento. Esta faixa vence qualquer outra faixa de tamanho do sistema. "
    "Para caber, encurte o porquê de cada etapa e junte frases, sem tirar etapa, preço, prazo "
    "ou pergunta.",
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


def load_rejected() -> dict[str, dict]:
    if REJECTED_FILE.exists():
        return json.loads(REJECTED_FILE.read_text())
    return {}


def save_rejected(rejected: dict[str, dict]) -> None:
    REJECTED_FILE.write_text(json.dumps(rejected, ensure_ascii=False, indent=2))


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


def size_findings(text: str) -> list[varredura.Achado]:
    """The floor of the size range; the ceiling is checked on the whole alert."""
    findings = []
    units = utf16_len(text)
    if units < PROPOSAL_MIN_CHARS:
        findings.append(
            varredura.Achado(
                varredura.ALERTA,
                f"abaixo de {PROPOSAL_MIN_CHARS} caracteres",
                f"{units} caracteres. Abaixo da faixa alguma coisa foi cortada, quase sempre uma "
                "etapa do trabalho. Exceção: vaga vaga demais para orçar.",
            )
        )
    return findings


# varredura.py's word range, replaced by size_findings
SKIPPED_SCAN_RULES = (
    f"abaixo de {varredura.PALAVRAS_MIN} palavras",
    f"acima de {varredura.PALAVRAS_MAX} palavras",
)


def scan_proposal(text: str) -> list[varredura.Achado]:
    """varredura.py's text checks, adjusted for Workana, errors first."""
    findings = workana_findings(varredura.desdobra(text)) + size_findings(text)
    for finding in varredura.varre(text, None):
        if finding.regra in SKIPPED_SCAN_RULES:
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


def proposal_problems(draft: ProposalDraft, project: dict[str, str]) -> tuple[bool, list[str]]:
    """Says if the draft can be priced, and lists what must leave the text for Claude."""
    try:
        proposal = price_proposal(draft)
    except ProposalError as exc:
        return False, [f"{exc}. Siga as regras dos marcadores de preço e de horas do sistema."]
    problems = []
    for finding in scan_proposal(proposal.proposal):
        if finding.nivel != varredura.ERRO:
            continue
        problem = f"{finding.regra}: {finding.detalhe}"
        if finding.trecho:
            problem += f' Trecho: "{finding.trecho}"'
        problems.append(problem)
    excess = message_excess(proposal, project)
    if excess > 0:
        cut = excess + REVISION_MARGIN_CHARS
        problems.append(
            f"proposta longa demais: com o alerta, a mensagem passa {excess} caracteres do limite "
            f"de {TELEGRAM_MAX_CHARS} do Telegram. Corte cerca de {cut} caracteres, umas "
            f"{math.ceil(cut / CHARS_PER_WORD)} palavras, encurtando o porquê de cada etapa e "
            "juntando frases, sem tirar etapa, preço, prazo ou pergunta."
        )
    return True, problems


def message_excess(proposal: Proposal, project: dict[str, str]) -> int:
    """How far the alert carrying the proposal goes past Telegram's limit; 0 or less fits."""
    return telegram_len(format_notification(project, proposal, None)) - TELEGRAM_MAX_CHARS


def fitting_proposal(draft: ProposalDraft, project: dict[str, str]) -> Proposal:
    """Prices the draft, refusing one whose alert would not fit one Telegram message."""
    proposal = price_proposal(draft)
    excess = message_excess(proposal, project)
    if excess > 0:
        raise ProposalTooLong(f"proposta passa {excess} caracteres do limite do Telegram")
    return proposal


def revision_request(draft: ProposalDraft, problems: list[str]) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        "\n\n<revisao>\n"
        "Sua proposta anterior está em <proposta_anterior>. A varredura automática achou estes "
        "problemas, e cada um precisa sair do texto:\n"
        f"{listed}\n\n"
        "Reescreva só o necessário para eliminar todos eles, mantendo o resto do texto, as regras "
        "do sistema e os marcadores. Os trechos citados mostram o texto já com os marcadores "
        "trocados pelos valores. Devolva o resultado completo, com as mesmas horas e telas, a não "
        "ser que algum problema exija mudar.\n"
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

    r = preco.calcula(horas_dev=draft.dev_hours, telas=draft.screens)
    hours = step_hours(draft, r)
    expected = [*PRICE_PLACEHOLDERS, *(marker for marker, h in hours.items() if h > 0)]
    # Each exactly once: a repeated marker would print the price twice
    if sorted(found) != sorted(expected):
        raise ProposalError(f"marcadores errados: {sorted(found)}, esperados {sorted(expected)}")

    text = draft.proposal
    for marker, value in zip(
        PRICE_PLACEHOLDERS,
        (preco.brl0(r.preco), client_deadline(r), preco.frase_da_cobranca(r), preco.frase_da_regua(r)),
    ):
        text = text.replace(marker, value)
    for marker, h in hours.items():
        text = text.replace(marker, str(h))

    installments = r.parcelas
    price = f"{preco.brl0(r.preco)}: entrada de {preco.brl0(installments[0].valor)}"
    if len(installments) > 1:
        price += f" e mais {len(installments) - 1} de {preco.brl0(installments[1].valor)}"
    notes = [draft.notes.strip().rstrip(".")] if draft.notes.strip() else []
    notes.append(f"{r.horas_total} h no total, {draft.dev_hours} de dev, {draft.screens} telas")
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


def step_hours(draft: ProposalDraft, r: preco.Resultado) -> dict[str, int]:
    """Hours of each proposal step, adding up to preco.py's total.

    The first two steps come from preco.py; the tests step takes whatever the other
    dev steps leave, which is where the skill puts the rounding.
    """
    shown = draft.build_hours + draft.backend_hours + draft.store_hours
    if min(draft.build_hours, draft.backend_hours, draft.store_hours) < 0:
        raise ProposalError("horas por etapa negativas")
    tests = r.horas_dev - shown
    if draft.build_hours <= 0 or tests <= 0:
        raise ProposalError(
            f"horas por etapa ({shown} h em construção, bastidores e loja) sem sobra para testes "
            f"dentro de dev_hours ({r.horas_dev} h)"
        )
    return dict(
        zip(
            HOUR_PLACEHOLDERS,
            (
                r.horas_fase_1,
                r.horas_desenho,
                draft.build_hours,
                draft.backend_hours,
                tests,
                draft.store_hours,
                r.horas_total,
            ),
        )
    )


def job_content(project: dict[str, str], description: str) -> str:
    return f"<vaga>\nTítulo: {project['title']}\nLink: {project['url']}\n\n{description}\n</vaga>"


def check_fit(oauth_token: str, project: dict[str, str], description: str) -> JobFit:
    """Asks Haiku whether the job is an app or web system, before the Opus proposal."""
    output = call_claude(
        oauth_token,
        FIT_PROMPT,
        job_content(project, description),
        model=FIT_MODEL,
        schema=JobFit,
        timeout=FIT_TIMEOUT_SECONDS,
    )
    return JobFit.model_validate(output)


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

    Scan errors left after the last revision still go out, listed in the alert. A
    proposal that does not fit one Telegram message never does: it raises
    ProposalTooLong, and the alert goes out saying so.
    """
    job = job_content(project, description)
    draft = run_claude(oauth_token, system, job)
    # The latest draft that could go out (right markers, fits one message): a revision
    # that breaks it, or a revision call that fails, must not throw it away
    usable: Proposal | None = None
    for _ in range(MAX_REVISIONS):
        priceable, problems = proposal_problems(draft, project)
        if not problems or not can_revise():
            break
        if priceable:
            try:
                usable = fitting_proposal(draft, project)
            except ProposalError:
                pass
        try:
            draft = run_claude(oauth_token, system, job + revision_request(draft, problems))
        except Exception as exc:  # noqa: BLE001
            if usable is None:
                raise
            detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
            print(f"Revisão falhou, seguindo com a versão anterior: {detail}", file=sys.stderr)
            return usable
    try:
        return fitting_proposal(draft, project)
    except ProposalError:
        if usable is None:
            raise
        return usable


def run_claude(oauth_token: str, system: str, content: str) -> ProposalDraft:
    output = call_claude(
        oauth_token,
        system,
        content,
        model=PROPOSAL_MODEL,
        schema=ProposalDraft,
        timeout=PROPOSAL_TIMEOUT_SECONDS,
        effort="high",
    )
    return ProposalDraft.model_validate(output)


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


def format_notification(
    project: dict[str, str],
    proposal: Proposal | None,
    error: str | None,
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
        text += f"\n\n✍️ <b>Proposta:</b>\n{html.escape(proposal.proposal)}"
    elif error:
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
    rejected = load_rejected()
    projects = fetch_projects()
    proposals_left = MAX_PROPOSALS_PER_RUN

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
            proposal = error = None
            off_profile = False
            if system:
                if proposals_left <= 0 or time.monotonic() - started > PROPOSAL_DEADLINE_SECONDS:
                    # Out of budget: stays pending instead of alerting without a proposal
                    deferred += 1
                    continue
                try:
                    description = fetch_description(project["url"])
                    off_profile = not triage(oauth_token, project, description)
                    if not off_profile:
                        proposals_left -= 1
                        proposal = generate_proposal(
                            oauth_token,
                            system,
                            project,
                            description,
                            can_revise=lambda: time.monotonic() - started
                            <= PROPOSAL_DEADLINE_SECONDS,
                        )
                except Exception as exc:  # noqa: BLE001
                    # Broad on purpose: a proposal error must never crash the run. Actions
                    # logs are public and a validation error would echo the model output,
                    # so only ProposalError, built to be safe, is logged in full.
                    detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
                    print(f"Falha na proposta de {project['url']}: {detail}", file=sys.stderr)
                    project["attempts"] += 1
                    # Too long already had its draft and revision: no other run redoes it
                    too_long = isinstance(exc, ProposalTooLong)
                    if not too_long and project["attempts"] < MAX_PROPOSAL_ATTEMPTS:
                        pending[project["id"]] = _pending_entry(project)
                        save_state()
                        deferred += 1
                        continue
                    if too_long:
                        error = "ficou longa demais para caber numa mensagem do Telegram"
                    else:
                        error = "erro ao ler a vaga ou falar com o Claude"
            if off_profile:
                # Silently dropped: no Telegram alert and no proposal for a rejected job
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
                # One message with the proposal: generate_proposal never returns one that
                # does not fit, so the split only guards an unforeseen overflow
                for chunk in split_message(format_notification(project, proposal, error)):
                    send_telegram(token, chat_id, chunk)
            except requests.RequestException as exc:
                failed += 1
                print(f"Falha ao enviar {project['url']}: {exc}", file=sys.stderr)
                # Stays pending: the next run re-sends the whole alert, proposal included
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


def triage(oauth_token: str, project: dict[str, str], description: str) -> bool:
    """Whether the job deserves a proposal.

    Fails open: a triage error must not cost a job that could fit.
    """
    try:
        return check_fit(oauth_token, project, description).is_match
    except Exception as exc:  # noqa: BLE001
        # Same rule as the proposal: only ProposalError is safe for the public logs
        detail = str(exc) if isinstance(exc, ProposalError) else type(exc).__name__
        print(f"Triagem falhou em {project['url']}, gerando proposta: {detail}", file=sys.stderr)
        return True


def _pending_entry(project: dict) -> dict:
    return {"title": project["title"], "url": project["url"], "attempts": project["attempts"]}

if __name__ == "__main__":
    main()
