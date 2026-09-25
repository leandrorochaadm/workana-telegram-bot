import json
import re
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import requests
from cryptography.fernet import Fernet

import bot
import encrypt_prompt
import preco
import varredura

KEYWORDS = ["aplicativo", "app"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Desenvolvimento de Aplicativo Móvel", True),
        ("Aplicativos de delivery", True),
        ("Criar App ou Site", True),
        ("Criar Apps iOS", True),
        ("APP de vendas", True),
        ("Automação de Atendimento via Whatsapp", False),
        ("Apple Watch integration", False),
        ("Sistema de gestão web", False),
    ],
)
def test_matches_keywords(title: str, expected: bool) -> None:
    assert bot.matches_keywords(title, KEYWORDS) is expected


def test_matches_keywords_escapes_special_chars() -> None:
    assert bot.matches_keywords("Vaga para C++ dev", ["c++"]) is False
    assert bot.matches_keywords("Vaga para node.js", ["node.js"]) is True


@pytest.mark.parametrize(
    "title", ["App no code", "App No-Code", "App nocode", "Apps low codes"]
)
def test_matches_keywords_space_matches_hyphen_or_nothing(title: str) -> None:
    assert bot.matches_keywords(title, ["no code", "low code"]) is True


def test_matches_keywords_empty_list_matches_nothing() -> None:
    assert bot.matches_keywords("App de jogo", []) is False


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bot, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bot, "PROMPT_FILE", tmp_path / "proposal_prompt.enc")
    for var in (
        "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KEYWORDS", "EXCLUDE_KEYWORDS",
        "CLAUDE_CODE_OAUTH_TOKEN", "PROMPT_KEY", "PRICE_HOURLY_RATE", "PRICE_MIN_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "# comment\nTELEGRAM_TOKEN=tok\n\nTELEGRAM_CHAT_ID=42\nKEYWORDS= aplicativo , app ,\n"
    )
    return tmp_path


def test_load_env_ignores_comments_and_blank_lines(workdir) -> None:
    assert bot.load_env() == {
        "TELEGRAM_TOKEN": "tok",
        "TELEGRAM_CHAT_ID": "42",
        "KEYWORDS": "aplicativo , app ,",
    }


def test_seen_roundtrip(workdir) -> None:
    assert bot.load_seen() == set()
    bot.save_seen({"b", "a"})
    assert bot.load_seen() == {"a", "b"}


def _project(pid: str, title: str) -> dict[str, str]:
    return {"id": pid, "title": title, "url": f"https://www.workana.com/job/{pid}"}


@pytest.fixture
def sent(monkeypatch):
    messages: list[str] = []

    def fake_send(token: str, chat_id: str, text: str, html_mode: bool = True) -> None:
        assert (token, chat_id) == ("tok", "42")
        if "FALHA" in text:
            raise requests.ConnectionError("offline")
        messages.append(text)

    monkeypatch.setattr(bot, "send_telegram", fake_send)
    return messages


def test_main_sends_only_new_matches(workdir, sent, monkeypatch) -> None:
    bot.save_seen({"old"})
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [
            _project("old", "App antigo"),
            _project("new", "App novo"),
            _project("site", "Site institucional"),
        ],
    )

    bot.main()

    assert len(sent) == 1
    assert "App novo" in sent[0]
    assert bot.load_seen() == {"old", "new", "site"}


def test_main_escapes_html_in_title(workdir, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App <b>&</b>")])

    bot.main()

    assert "App &lt;b&gt;&amp;&lt;/b&gt;" in sent[0]


def test_main_retries_failed_send_on_next_run(workdir, sent, monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [_project("ok", "App ok"), _project("fail", "App FALHA")],
    )

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1
    assert len(sent) == 1
    assert bot.load_seen() == {"ok"}


def test_main_saves_seen_when_fetch_succeeds_but_send_crashes(workdir, monkeypatch) -> None:
    def boom(*_args) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bot, "send_telegram", boom)
    monkeypatch.setattr(
        bot, "fetch_projects", lambda: [_project("site", "Site"), _project("app", "App")]
    )

    with pytest.raises(RuntimeError):
        bot.main()

    assert bot.load_seen() == {"site"}


def test_main_skips_excluded_jobs_including_queued_ones(workdir, sent, monkeypatch) -> None:
    with bot.ENV_FILE.open("a") as f:
        f.write("EXCLUDE_KEYWORDS=jogo, wordpress ,\n")
    bot.save_pending({"old": {"title": "App de jogo", "url": "u", "attempts": 1}})
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [_project("wp", "App WordPress"), _project("ok", "App de delivery")],
    )

    bot.main()

    assert len(sent) == 1
    assert "App de delivery" in sent[0]
    assert bot.load_seen() == {"old", "wp", "ok"}
    assert bot.load_pending() == {}


def test_main_exits_without_credentials(workdir) -> None:
    bot.ENV_FILE.write_text("KEYWORDS=app\n")

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1


def test_main_exits_without_keywords(workdir) -> None:
    bot.ENV_FILE.write_text("TELEGRAM_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n")

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1


PROPOSAL = bot.Proposal(
    proposal="Olá! Proposta <texto>",
    price="R$ 4.800",
    deadline="3 semanas",
    negotiation_floor="R$ 4.000",
    notes="",
)


@pytest.fixture
def with_proposals(workdir, monkeypatch):
    key = Fernet.generate_key().decode()
    bot.PROMPT_FILE.write_bytes(Fernet(key.encode()).encrypt("prompt secreto".encode()))
    with bot.ENV_FILE.open("a") as f:
        f.write(
            f"CLAUDE_CODE_OAUTH_TOKEN=oauth-test\nPROMPT_KEY={key}\n"
            "PRICE_HOURLY_RATE=50\nPRICE_MIN_PROJECT=3000\n"
        )
    # configure_pricing writes preco's module globals; restore them after the test
    monkeypatch.setattr(preco, "VALOR_HORA", preco.VALOR_HORA)
    monkeypatch.setattr(preco, "MINIMO_PROJETO", preco.MINIMO_PROJETO)
    calls: list[tuple[str, str]] = []

    def fake_generate(_client, system, project, description, can_revise):
        assert can_revise() is True  # the run just started, well within the deadline
        calls.append((system, description))
        if "quebra" in project["title"]:
            raise RuntimeError("boom")
        if "vencido" in project["title"]:
            raise bot.ProposalError("claude saiu com código 1 (api_error_status=401)")
        if "inesperado" in project["title"]:
            raise ValueError("saída do modelo com texto sigiloso")
        return PROPOSAL

    monkeypatch.setattr(bot, "fetch_description", lambda url: f"descrição de {url}")
    monkeypatch.setattr(bot, "generate_proposal", fake_generate)
    monkeypatch.setattr(bot.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    return calls


def test_main_sends_proposal_after_notification(with_proposals, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    [(system, description)] = with_proposals
    assert system == "prompt secreto\n\n" + bot.forbidden_wording_prompt()
    assert description == "descrição de https://www.workana.com/job/x"
    assert len(sent) == 2
    assert "R$ 4.800" in sent[0] and "R$ 4.000" in sent[0]
    assert sent[1] == "Olá! Proposta <texto>"


def test_main_defers_failed_proposal_to_next_run(with_proposals, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App quebra")])

    bot.main()

    assert sent == []
    assert bot.load_seen() == set()
    assert bot.load_pending()["x"]["attempts"] == 1


def test_main_sends_without_proposal_after_max_attempts(with_proposals, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App quebra")])

    for _ in range(bot.MAX_PROPOSAL_ATTEMPTS):
        bot.main()

    assert len(with_proposals) == bot.MAX_PROPOSAL_ATTEMPTS
    assert len(sent) == 1
    assert "Proposta não gerada" in sent[0]
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}


def test_main_retries_pending_job_that_left_the_listing(with_proposals, sent, monkeypatch) -> None:
    project = _project("x", "App antigo")
    bot.save_pending({"x": {"title": project["title"], "url": project["url"], "attempts": 1}})
    monkeypatch.setattr(bot, "fetch_projects", lambda: [])

    bot.main()

    assert len(sent) == 2
    assert "App antigo" in sent[0]
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}


def test_main_caps_proposals_per_run(with_proposals, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "MAX_PROPOSALS_PER_RUN", 1)
    monkeypatch.setattr(
        bot, "fetch_projects", lambda: [_project("a", "App a"), _project("b", "App b")]
    )

    bot.main()

    assert len(with_proposals) == 1
    assert len(sent) == 2  # alert + proposal for "a" only
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {"b": {"title": "App b", "url": "https://www.workana.com/job/b", "attempts": 0}}


def test_main_skips_proposals_with_wrong_key(with_proposals, sent, monkeypatch) -> None:
    bot.ENV_FILE.write_text(
        bot.ENV_FILE.read_text() + f"PROMPT_KEY={Fernet.generate_key().decode()}\n"
    )
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_proposals == []
    assert len(sent) == 1


@pytest.mark.parametrize("rates", ["", "PRICE_HOURLY_RATE=abc\nPRICE_MIN_PROJECT=3000\n", "PRICE_HOURLY_RATE=0\nPRICE_MIN_PROJECT=3000\n"])
def test_main_skips_proposals_without_valid_rates(with_proposals, sent, monkeypatch, capsys, rates) -> None:
    env = bot.ENV_FILE.read_text()
    bot.ENV_FILE.write_text(
        "".join(line + "\n" for line in env.splitlines() if not line.startswith("PRICE_")) + rates
    )
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_proposals == []
    assert len(sent) == 1
    assert "PRICE_HOURLY_RATE" in capsys.readouterr().err


def test_main_skips_proposals_without_claude_cli(with_proposals, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_proposals == []
    assert len(sent) == 1
    assert "Claude Code não instalado" in capsys.readouterr().err


def test_main_logs_proposal_error_details(with_proposals, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App vencido")])

    bot.main()

    assert "api_error_status=401" in capsys.readouterr().err


def test_split_message_respects_limit() -> None:
    text = "\n".join(["linha " * 10] * 20)
    chunks = bot.split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert bot.split_message("x" * 250, limit=100) == ["x" * 100, "x" * 100, "x" * 50]


def test_main_survives_unexpected_error_without_leaking_it(
    with_proposals, sent, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App inesperado")])

    bot.main()

    assert bot.load_pending()["x"]["attempts"] == 1
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "sigiloso" not in err


def test_main_keeps_job_pending_when_proposal_send_fails(with_proposals, monkeypatch) -> None:
    messages: list[str] = []

    def fake_send(_token, _chat_id, text, html_mode=True):
        if not html_mode:
            raise requests.ConnectionError("offline")
        messages.append(text)

    monkeypatch.setattr(bot, "send_telegram", fake_send)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1
    assert len(messages) == 1
    # Retried next run so the proposal is not lost
    assert bot.load_seen() == set()
    assert "x" in bot.load_pending()


def test_format_notification_escapes_proposal_fields() -> None:
    proposal = bot.Proposal(
        proposal="x", price="<b>R$ 1</b>", deadline="1 & 2", negotiation_floor="<i>", notes="a<b"
    )

    text = bot.format_notification(_project("x", "App"), proposal, None)

    assert "&lt;b&gt;R$ 1&lt;/b&gt;" in text
    assert "1 &amp; 2" in text
    assert "&lt;i&gt;" in text
    assert "a&lt;b" in text


def test_format_notification_omits_empty_notes() -> None:
    text = bot.format_notification(_project("x", "App"), PROPOSAL, None)

    assert "📝" not in text


def _fake_claude(
    monkeypatch, returncode: int = 0, output: dict | None = None, outputs: list[dict] | None = None
) -> list[dict]:
    """Fakes the CLI; `outputs` answers call by call, repeating the last one."""
    calls: list[dict] = []
    answers = outputs or [output or {}]

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        answer = answers[min(len(calls), len(answers)) - 1]
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(answer))

    monkeypatch.setattr(bot.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setattr(preco, "VALOR_HORA", 50.0)
    monkeypatch.setattr(preco, "MINIMO_PROJETO", 3_000.0)


DRAFT_TEXT = "Fecho em {{PRECO}}, em {{PRAZO}}. {{COBRANCA}} {{REGUA}}"


def _draft(**overrides) -> bot.ProposalDraft:
    fields = {"proposal": DRAFT_TEXT, "dev_hours": 50, "screens": 4, "notes": ""}
    return bot.ProposalDraft(**{**fields, **overrides})


def test_generate_proposal_sends_prompt_and_job(monkeypatch, rates) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    calls = _fake_claude(monkeypatch, output={"structured_output": _draft().model_dump()})

    result = bot.generate_proposal("oauth-tok", "prompt", _project("x", "App novo"), "descrição")

    assert result == bot.price_proposal(_draft())
    cmd = calls[0]["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == bot.PROPOSAL_MODEL
    assert cmd[cmd.index("--system-prompt") + 1] == "prompt"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == bot.ProposalDraft.model_json_schema()
    content = calls[0]["input"]
    assert content.startswith("<vaga>") and content.endswith("</vaga>")
    assert "App novo" in content and "https://www.workana.com/job/x" in content
    assert "descrição" in content
    env = calls[0]["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert "ANTHROPIC_API_KEY" not in env


def _answer(**overrides) -> dict:
    return {"structured_output": _draft(**overrides).model_dump()}


def test_generate_proposal_asks_claude_to_fix_scan_errors(monkeypatch, rates) -> None:
    calls = _fake_claude(
        monkeypatch,
        outputs=[_answer(proposal=DRAFT_TEXT + " Pronto — publicado."), _answer()],
    )

    result = bot.generate_proposal("tok", "prompt", _project("x", "App novo"), "descrição")

    assert len(calls) == 2
    revision = calls[1]["input"]
    assert revision.startswith(calls[0]["input"])
    assert "<revisao>" in revision and "travessão" in revision and "Pronto — publicado" in revision
    assert f"<proposta_anterior>\n{DRAFT_TEXT} Pronto — publicado.\n</proposta_anterior>" in revision
    assert not any(item.startswith("ERRO") for item in result.review)


def test_generate_proposal_revises_wrong_markers(monkeypatch, rates) -> None:
    calls = _fake_claude(monkeypatch, outputs=[_answer(proposal="Fecho em {{PRECO}}."), _answer()])

    result = bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")

    assert len(calls) == 2
    assert "marcadores de preço errados" in calls[1]["input"]
    assert "{{" not in result.proposal


def test_generate_proposal_ignores_alerts(monkeypatch, rates) -> None:
    # Too short is only an alert: no revision for it
    calls = _fake_claude(monkeypatch, output=_answer())

    bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")

    assert len(calls) == 1


def test_generate_proposal_stops_after_max_revisions(monkeypatch, rates) -> None:
    calls = _fake_claude(monkeypatch, output=_answer(proposal=DRAFT_TEXT + " Pronto — publicado."))

    result = bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")

    assert len(calls) == 1 + bot.MAX_REVISIONS
    assert any("travessão" in item for item in result.review)


def test_generate_proposal_skips_revision_past_deadline(monkeypatch, rates) -> None:
    calls = _fake_claude(monkeypatch, output=_answer(proposal=DRAFT_TEXT + " Pronto — publicado."))

    result = bot.generate_proposal(
        "tok", "prompt", _project("x", "App"), "descrição", can_revise=lambda: False
    )

    assert len(calls) == 1
    assert result.review[0].startswith("ERRO: travessão")


def test_generate_proposal_keeps_previous_draft_when_revision_breaks_markers(
    monkeypatch, rates
) -> None:
    first = DRAFT_TEXT + " Pronto — publicado."
    calls = _fake_claude(monkeypatch, outputs=[_answer(proposal=first), _answer(proposal="{{PRECO}}")])

    result = bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")

    assert len(calls) == 3
    assert "Pronto — publicado." in result.proposal
    assert result.review[0].startswith("ERRO: travessão")


def test_generate_proposal_keeps_previous_draft_when_revision_call_fails(
    monkeypatch, rates, capsys
) -> None:
    runs = iter([_draft(proposal=DRAFT_TEXT + " Pronto — publicado.")])

    def fake_run_claude(_token, _system, _content):
        draft = next(runs, None)
        if draft is None:
            raise subprocess.TimeoutExpired("claude", 180)
        return draft

    monkeypatch.setattr(bot, "run_claude", fake_run_claude)

    result = bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")

    assert "Pronto — publicado." in result.proposal
    assert "Revisão falhou" in capsys.readouterr().err


def test_generate_proposal_raises_when_first_draft_unusable_and_revision_fails(
    monkeypatch, rates
) -> None:
    runs = iter([_draft(proposal="{{PRECO}}")])

    def fake_run_claude(_token, _system, _content):
        draft = next(runs, None)
        if draft is None:
            raise bot.ProposalError("claude saiu com código 1")
        return draft

    monkeypatch.setattr(bot, "run_claude", fake_run_claude)

    with pytest.raises(bot.ProposalError, match="código 1"):
        bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")


def test_generate_proposal_raises_when_markers_stay_wrong(monkeypatch, rates) -> None:
    _fake_claude(monkeypatch, output=_answer(proposal="Fecho em {{PRECO}}."))

    with pytest.raises(bot.ProposalError, match="marcadores de preço errados"):
        bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")


def test_generate_proposal_raises_on_cli_failure(monkeypatch) -> None:
    _fake_claude(
        monkeypatch, returncode=1, output={"is_error": True, "api_error_status": 401}
    )

    with pytest.raises(bot.ProposalError, match="código 1.*api_error_status=401"):
        bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")


def test_generate_proposal_raises_on_non_json_output(monkeypatch) -> None:
    monkeypatch.setattr(
        bot.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="boom")
    )

    with pytest.raises(bot.ProposalError, match="código 1"):
        bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")


def test_generate_proposal_raises_without_structured_output(monkeypatch) -> None:
    _fake_claude(monkeypatch, output={"is_error": True, "subtype": "error_max_turns"})

    with pytest.raises(bot.ProposalError, match="subtype=error_max_turns"):
        bot.generate_proposal("tok", "prompt", _project("x", "App"), "descrição")


def test_price_proposal_fills_placeholders_from_preco(rates) -> None:
    # 50 h de dev + 4 de desenho + 8 de Fase B + 2 de wireframe = 64 h x R$ 50 = R$ 3 200
    result = bot.price_proposal(_draft(notes="design assumido."))

    r = preco.calcula(horas_dev=50, telas=4)
    assert "{{" not in result.proposal
    assert result.proposal == (
        f"Fecho em R$ 3 200, em dois dias para fechar o projeto no papel e duas semanas de "
        f"desenvolvimento. {preco.frase_da_cobranca(r)} {preco.frase_da_regua(r)}"
    )
    assert result.price == f"R$ 3 200: entrada de {preco.brl0(r.parcelas[0].valor)} e mais 2 de {preco.brl0(r.parcelas[1].valor)}"
    assert result.deadline == preco.prazo_texto(r.fases, r.dias_fase_1)
    assert result.negotiation_floor == "R$ 3 200"
    assert result.notes == "design assumido. 50 h de dev, 4 telas"


def test_price_proposal_flags_price_below_minimum(rates) -> None:
    result = bot.price_proposal(_draft(dev_hours=10))

    assert "abaixo do mínimo de R$ 3 000" in result.notes


def test_price_proposal_flags_long_project(rates) -> None:
    result = bot.price_proposal(_draft(dev_hours=400))

    assert "projeto longo" in result.notes


def test_price_proposal_spells_single_remaining_installment(rates) -> None:
    result = bot.price_proposal(_draft(dev_hours=5, screens=1))

    assert "e mais uma de R$" in result.proposal


def test_price_proposal_without_estimate_leaves_price_open(rates) -> None:
    result = bot.price_proposal(_draft(proposal="Me responde essas três.", dev_hours=0, screens=0))

    assert result.proposal == "Me responde essas três."
    assert result.price.startswith("a definir")


@pytest.mark.parametrize(
    "overrides",
    [
        {"proposal": "Fecho em {{PRECO}}."},
        {"proposal": DRAFT_TEXT + " {{EXTRA}}"},
        {"proposal": DRAFT_TEXT + " {{PRECO}}"},
        {"dev_hours": 0},
        {"screens": 0},
    ],
)
def test_price_proposal_rejects_inconsistent_draft(rates, overrides) -> None:
    with pytest.raises(bot.ProposalError):
        bot.price_proposal(_draft(**overrides))


def test_review_proposal_lists_errors_before_alerts() -> None:
    review = bot.review_proposal("Oi — tudo bem? Veja em https://exemplo.com, fecho em R$ 1 000.")

    assert review[0].startswith("ERRO: ")
    assert any("travessão" in item for item in review)
    # A link is only an alert in varredura.py, but it suspends the Workana account
    assert any(item.startswith("ERRO: endereço no texto") for item in review)
    assert any(item.startswith("alerta: abaixo de 600 palavras") for item in review)
    assert not any("compromisso antes das perguntas" in item for item in review)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Me escreve em leandro@exemplo.com.br.", "e-mail no texto"),
        ("Meu número é (11) 99999-8888.", "telefone no texto"),
        ("Veja o portfólio em leandro.dev.br.", "endereço de site no texto"),
        ("Podemos fazer uma call amanhã.", "convite para conversa ou rede social"),
        ("Me chama que eu explico.", "convite para conversa ou rede social"),
        ("Me segue no Instagram.", "convite para conversa ou rede social"),
    ],
)
def test_review_proposal_blocks_what_workana_suspends(text, rule) -> None:
    assert f"ERRO: {rule}" in "\n".join(bot.review_proposal(text))


@pytest.mark.parametrize(
    "text",
    ["O app do call center mostra a fila.", "O app agenda reuniões da equipe.", "Entrar com a conta do Facebook."],
)
def test_review_proposal_allows_client_scope_words(text) -> None:
    assert not any("convite" in item for item in bot.review_proposal(text))


FORBIDDEN_TERMS = [term for _, terms in bot.FORBIDDEN_WORDING for term in terms]


@pytest.mark.parametrize("term", FORBIDDEN_TERMS)
def test_forbidden_wording_is_flagged_by_the_scan(term) -> None:
    # A term the scan lets through would only make Claude avoid harmless wording
    text = f"Posso seguir assim: {term} no projeto."
    findings = [f for f in bot.scan_proposal(text) if f.nivel == varredura.ERRO]
    assert any(term.lower() in f.trecho.lower() for f in findings)


@pytest.mark.parametrize(
    "pattern",
    varredura.CONECTORES_PROIBIDOS
    + [pad for pad, _ in varredura.PALAVRAS_PROIBIDAS]
    + varredura.EXCLUSIVIDADE,
)
def test_forbidden_wording_covers_every_scan_term(pattern) -> None:
    # A new term in varredura.py must reach the prompt too
    assert any(re.search(pattern, term, re.IGNORECASE) for term in FORBIDDEN_TERMS)


def test_forbidden_wording_prompt_lists_terms_and_rules() -> None:
    prompt = bot.forbidden_wording_prompt()

    assert prompt.startswith("<proibicoes>") and prompt.endswith("</proibicoes>")
    assert all(f'"{term}"' in prompt for term in FORBIDDEN_TERMS)
    assert all(rule in prompt for rule in bot.FORBIDDEN_RULES)


def test_review_proposal_keeps_skill_example_clean() -> None:
    # Prices, ratings and download counts are not phone numbers or sites
    text = (
        "Pelo que está escrito, fecho em R$ 12 000. O app tem nota 4,8 e passou de um milhão "
        "de downloads. Entrada de R$ 2 400 e mais quatro de R$ 2 400, uma por entrega."
    )
    assert not any(item.startswith("ERRO") for item in bot.review_proposal(text))


def test_price_proposal_reviews_filled_text(rates) -> None:
    result = bot.price_proposal(_draft(proposal=DRAFT_TEXT + " Sem travessão — nenhum."))

    assert any("travessão" in item for item in result.review)
    # The phrases preco.py fills in pass the scan on their own
    assert not any(item.startswith("ERRO") for item in bot.price_proposal(_draft()).review)


def test_format_notification_lists_review() -> None:
    proposal = PROPOSAL.model_copy(update={"review": ["ERRO: travessão (a <b>)"]})

    text = bot.format_notification(_project("x", "App"), proposal, None)

    assert "🔎 <b>Varredura:</b>\n• ERRO: travessão (a &lt;b&gt;)" in text


def test_configure_pricing_sets_preco_rates(rates) -> None:
    assert bot.configure_pricing("80", "4500") is True
    assert (preco.VALOR_HORA, preco.MINIMO_PROJETO) == (80.0, 4500.0)
    assert bot.configure_pricing("80", None) is False
    assert bot.configure_pricing("-1", "4500") is False


def test_fetch_projects_follows_pages_merges_searches_and_skips_empty_ones(
    monkeypatch, capsys
) -> None:
    # {page url: (job links, has next page)}
    pages = {
        "https://w/a&page=1": ([("/job/x?ref=1", "App X")], True),
        "https://w/a&page=2": ([("/job/y", "App Y")], False),
        "https://w/b&page=1": ([("/job/y?ref=2", "App Y de novo"), ("", "Sem link")], False),
        "https://w/empty&page=1": ([], False),
        "https://w/capped&page=1": ([("/job/z", "App Z")], True),
        "https://w/capped&page=2": ([], True),
    }
    visited: list[str] = []

    class FakeElement:
        def __init__(self, href, title):
            self.href, self.title = href, title

        def get_attribute(self, name):
            return {"href": self.href, "title": self.title}[name]

        def query_selector(self, _selector):
            return self

    class FakeLocator:
        first = property(lambda self: self)

        def or_(self, _other):
            return self

        def wait_for(self, **_kwargs):
            pass

    class FakePage:
        def goto(self, url, **_kwargs):
            visited[-1] = url

        def locator(self, _selector):
            return FakeLocator()

        def get_by_text(self, text):
            assert text == bot.NO_RESULTS_TEXT
            return FakeLocator()

        def query_selector_all(self, _selector):
            return [FakeElement(*link) for link in pages[visited[-1]][0]]

        def query_selector(self, selector):
            assert selector.startswith("ul.pagination")
            return object() if pages[visited[-1]][1] else None

    browsers: list[object] = []

    @contextmanager
    def fake_open_browser():
        browsers.append(object())
        yield browsers[-1]

    @contextmanager
    def fake_fresh_page(browser):
        assert browser is browsers[-1]
        visited.append("")
        yield FakePage()

    monkeypatch.setattr(bot, "open_browser", fake_open_browser)
    monkeypatch.setattr(bot, "fresh_page", fake_fresh_page)
    monkeypatch.setattr(bot, "MAX_PAGES_PER_SEARCH", 2)
    monkeypatch.setattr(
        bot, "WORKANA_URLS", ("https://w/a", "https://w/b", "https://w/empty", "https://w/capped")
    )

    def job(pid, title):
        url = f"https://www.workana.com/job/{pid}"
        return {"id": url, "title": title, "url": url}

    assert bot.fetch_projects() == [job("x", "App X"), job("y", "App Y"), job("z", "App Z")]
    # One browser for the whole scrape, a fresh page per listing, and the cap stops
    # a search that still has a next page
    assert len(browsers) == 1
    assert visited == list(pages)
    assert capsys.readouterr().err == "Limite de 2 páginas atingido em https://w/capped\n"


def test_fetch_description_reads_detail_block(monkeypatch) -> None:
    visited: list[str] = []

    class FakePage:
        def goto(self, url, **_kwargs):
            visited.append(url)

        def wait_for_selector(self, selector, **_kwargs):
            assert selector == ".block-detail"

        def inner_text(self, selector):
            assert selector == ".block-detail"
            return "  Orçamento e descrição  "

    @contextmanager
    def fake_open_page():
        yield FakePage()

    monkeypatch.setattr(bot, "open_page", fake_open_page)

    assert bot.fetch_description("https://w/job/x") == "Orçamento e descrição"
    assert visited == ["https://w/job/x"]


@pytest.fixture
def prompt_files(tmp_path, monkeypatch):
    monkeypatch.setattr(encrypt_prompt, "SOURCE", tmp_path / "proposal_prompt.md")
    monkeypatch.setattr(encrypt_prompt, "TARGET", tmp_path / "proposal_prompt.enc")
    monkeypatch.setattr(bot, "PROMPT_FILE", tmp_path / "proposal_prompt.enc")
    monkeypatch.delenv("PROMPT_KEY", raising=False)
    encrypt_prompt.SOURCE.write_text("regras de preço")
    return tmp_path


def test_encrypt_prompt_roundtrip_with_existing_key(prompt_files, monkeypatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PROMPT_KEY", key)

    encrypt_prompt.main()

    assert b"regras" not in encrypt_prompt.TARGET.read_bytes()
    assert bot.load_prompt(key) == "regras de preço"


def test_encrypt_prompt_generates_key_when_missing(prompt_files, capsys) -> None:
    encrypt_prompt.main()

    key = capsys.readouterr().out.splitlines()[1]
    assert bot.load_prompt(key) == "regras de preço"


def test_encrypt_prompt_exits_without_source(prompt_files) -> None:
    encrypt_prompt.SOURCE.unlink()

    with pytest.raises(SystemExit) as exc:
        encrypt_prompt.main()

    assert exc.value.code == 1


def test_main_stops_proposals_after_deadline(with_proposals, sent, monkeypatch) -> None:
    clock = iter([0.0, bot.PROPOSAL_DEADLINE_SECONDS + 1])
    monkeypatch.setattr(bot.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_proposals == []
    assert sent == []
    assert bot.load_pending()["x"]["attempts"] == 0


def test_main_saves_seen_after_each_alert(workdir, monkeypatch) -> None:
    def send_then_die(_token, _chat_id, text, html_mode=True):
        if "App b" in text:
            raise KeyboardInterrupt  # what a workflow timeout looks like to the script

    monkeypatch.setattr(bot, "send_telegram", send_then_die)
    monkeypatch.setattr(bot, "save_seen", lambda seen: saved.append(set(seen)))
    saved: list[set[str]] = []
    monkeypatch.setattr(
        bot, "fetch_projects", lambda: [_project("a", "App a"), _project("b", "App b")]
    )

    with pytest.raises(KeyboardInterrupt):
        bot.main()

    assert saved[1] == {"a"}
    # "b" never got its alert out but is still queued, even if it leaves the listing
    assert "b" in bot.load_pending()
