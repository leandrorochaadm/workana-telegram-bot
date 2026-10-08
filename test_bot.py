import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests

import bot


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


# Ends without a newline on purpose: the fixture adds one, as a text file has
FIT_PROMPT_TEXT = "Critério de triagem de teste.\nResponda só sim ou não."


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    # Same layout as the proposta checkout next to bot.py on the runner
    proposta = tmp_path / "proposta"
    data = proposta / "data"
    data.mkdir(parents=True)
    fit_prompt = proposta / "prompts" / "fit_prompt.md"
    fit_prompt.parent.mkdir()
    fit_prompt.write_text(FIT_PROMPT_TEXT + "\n")
    monkeypatch.setattr(bot, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bot, "PROPOSTA_DIR", proposta)
    monkeypatch.setattr(bot, "DATA_DIR", data)
    monkeypatch.setattr(bot, "SEEN_FILE", data / "seen.json")
    monkeypatch.setattr(bot, "PENDING_FILE", data / "pending.json")
    monkeypatch.setattr(bot, "REJECTED_FILE", data / "rejected.json")
    monkeypatch.setattr(bot, "FIT_PROMPT_FILE", fit_prompt)
    monkeypatch.setattr(bot, "GENERATED_DIR", proposta / "generated")
    monkeypatch.setattr(bot, "TELEGRAM_RETRY_SECONDS", 0)
    for var in (
        "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KEYWORDS", "EXCLUDE_KEYWORDS",
        "CLAUDE_CODE_OAUTH_TOKEN",
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


def test_load_fit_prompt_is_none_when_missing_or_blank(workdir) -> None:
    assert bot.load_fit_prompt() == FIT_PROMPT_TEXT
    bot.FIT_PROMPT_FILE.write_text(" \n\n")
    assert bot.load_fit_prompt() is None
    bot.FIT_PROMPT_FILE.unlink()
    assert bot.load_fit_prompt() is None


def _project(pid: str, title: str) -> dict[str, str]:
    return {"id": pid, "title": title, "url": f"https://www.workana.com/job/{pid}"}


@pytest.fixture
def sent(monkeypatch):
    messages: list[str] = []

    def fake_send(token: str, chat_id: str, text: str) -> None:
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


def test_exits_without_data_dir(workdir, sent, monkeypatch, capsys) -> None:
    bot.DATA_DIR.rmdir()

    def fetch_without_state() -> list[dict[str, str]]:
        raise AssertionError("read Workana without the state checkout")

    monkeypatch.setattr(bot, "fetch_projects", fetch_without_state)

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1
    assert sent == []
    assert "proposta/data" in capsys.readouterr().err
    # Nothing recreated it: a run without the checkout must not start a fresh seen.json
    assert not bot.DATA_DIR.exists()


@pytest.fixture
def with_triage(workdir, monkeypatch):
    """Triage on: OAuth token, fit_prompt.md (from workdir) and the claude CLI.

    Returns (token, fit prompt, job id, description) for each check_fit call. Pages of
    jobs whose id is "quebra" or "inesperado" fail to load.
    """
    with bot.ENV_FILE.open("a") as f:
        f.write("CLAUDE_CODE_OAUTH_TOKEN=oauth-test\n")
    calls: list[tuple[str, str, str, str]] = []

    def fake_fetch_description(url: str) -> str:
        if url.endswith("/quebra"):
            raise RuntimeError("boom")
        if url.endswith("/inesperado"):
            raise ValueError("página com texto sigiloso")
        return f"descrição de {url}"

    def fake_check_fit(token, fit_prompt, project, description):
        # Recorded, not asserted: triage swallows any exception from here
        calls.append((token, fit_prompt, project["id"], description))
        if "logo" in project["title"]:
            return bot.JobFit(is_match=False)
        if "vencido" in project["title"]:
            raise bot.ProposalError("claude saiu com código 1 (api_error_status=401)")
        if "triagem" in project["title"]:
            raise ValueError("saída do modelo com texto sigiloso")
        return bot.JobFit(is_match=True)

    monkeypatch.setattr(bot, "fetch_description", fake_fetch_description)
    monkeypatch.setattr(bot, "check_fit", fake_check_fit)
    monkeypatch.setattr(bot.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    return calls


def test_main_sends_alert_after_triage_approves(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_triage == [
        ("oauth-test", FIT_PROMPT_TEXT, "x", "descrição de https://www.workana.com/job/x")
    ]
    # Only the alert: no proposal until the agent takes over
    assert sent == ["🆕 <b>App novo</b>\nhttps://www.workana.com/job/x"]
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}


def test_main_defers_job_whose_page_fails_to_next_run(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("quebra", "App quebra")])

    bot.main()

    assert with_triage == []
    assert sent == []
    assert bot.load_seen() == set()
    assert bot.load_pending()["quebra"]["attempts"] == 1


def test_main_alerts_after_max_attempts_reading_the_page(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("quebra", "App quebra")])

    for _ in range(bot.MAX_PROPOSAL_ATTEMPTS):
        bot.main()

    assert with_triage == []
    [alert] = sent
    assert "App quebra" in alert
    assert "Proposta não gerada: erro ao ler a vaga" in alert
    assert bot.load_seen() == {"quebra"}
    assert bot.load_pending() == {}


def test_main_retries_pending_job_that_left_the_listing(with_triage, sent, monkeypatch) -> None:
    project = _project("x", "App antigo")
    bot.save_pending({"x": {"title": project["title"], "url": project["url"], "attempts": 1}})
    monkeypatch.setattr(bot, "fetch_projects", lambda: [])

    bot.main()

    assert len(with_triage) == 1
    [alert] = sent
    assert "App antigo" in alert
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}


def test_main_skips_rejected_job_silently(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App de logo")])

    bot.main()

    assert len(with_triage) == 1
    assert sent == []
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}
    [(pid, job)] = bot.load_rejected().items()
    assert pid == "x"
    assert "reason" not in job
    assert job["url"] == "https://www.workana.com/job/x" and job["date"]


def test_main_alerts_when_triage_fails(with_triage, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App triagem")])

    bot.main()

    assert len(sent) == 1
    err = capsys.readouterr().err
    assert "Triagem falhou" in err and "ValueError" in err
    assert "sigiloso" not in err
    assert bot.load_seen() == {"x"}


def test_main_logs_triage_error_details(with_triage, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App vencido")])

    bot.main()

    assert "api_error_status=401" in capsys.readouterr().err
    assert len(sent) == 1


def test_main_alerts_only_without_claude_cli(with_triage, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_triage == []
    assert len(sent) == 1
    assert "Claude Code não instalado" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, " \n"])
def test_main_alerts_only_without_fit_prompt(
    with_triage, sent, monkeypatch, capsys, content
) -> None:
    if content is None:
        bot.FIT_PROMPT_FILE.unlink()
    else:
        bot.FIT_PROMPT_FILE.write_text(content)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_triage == []
    assert len(sent) == 1
    assert "App novo" in sent[0]
    assert "fit_prompt.md" in capsys.readouterr().err
    assert "x" in bot.load_seen()


def test_main_survives_unexpected_error_without_leaking_it(
    with_triage, sent, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("inesperado", "App inesperado")])

    bot.main()

    assert bot.load_pending()["inesperado"]["attempts"] == 1
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "sigiloso" not in err


def test_main_keeps_job_pending_when_alert_fails(with_triage, monkeypatch) -> None:
    messages: list[str] = []

    def fake_send(_token, _chat_id, text):
        messages.append(text)
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(bot, "send_telegram", fake_send)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    with pytest.raises(SystemExit) as exc:
        bot.main()

    assert exc.value.code == 1
    assert len(messages) == bot.TELEGRAM_SEND_ATTEMPTS
    # Retried next run, whole alert, with nothing else stored
    assert bot.load_seen() == set()
    assert bot.load_pending() == {
        "x": {"title": "App novo", "url": "https://www.workana.com/job/x", "attempts": 0}
    }


def test_main_stops_triage_after_deadline(with_triage, sent, monkeypatch) -> None:
    clock = iter([0.0, bot.PROPOSAL_DEADLINE_SECONDS + 1])
    monkeypatch.setattr(bot.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_triage == []
    assert sent == []
    assert bot.load_pending()["x"]["attempts"] == 0


def test_split_message_respects_limit() -> None:
    text = "\n".join(["linha " * 10] * 20)
    chunks = bot.split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")
    assert bot.split_message("x" * 250, limit=100) == ["x" * 100, "x" * 100, "x" * 50]


def test_split_message_counts_emoji_as_two_units() -> None:
    # Telegram counts UTF-16 units: 60 emojis take 120 of the 100 allowed
    chunks = bot.split_message("🆕" * 60, limit=100)
    assert chunks == ["🆕" * 50, "🆕" * 10]
    # A limit smaller than one emoji still ends, one character per chunk
    assert bot.split_message("🆕🆕", limit=1) == ["🆕", "🆕"]


def test_split_message_never_cuts_an_html_entity() -> None:
    text = "a" * 95 + " &amp;" + " b" * 20
    chunks = bot.split_message(text, limit=100)
    assert chunks[0] == "a" * 95
    assert chunks[1].startswith("&amp;")


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(response=SimpleNamespace(status_code=status))


@pytest.mark.parametrize(
    "error", [requests.ConnectionError("offline"), _http_error(429), _http_error(502)]
)
def test_send_with_retry_tries_again_on_passing_errors(monkeypatch, error) -> None:
    monkeypatch.setattr(bot, "TELEGRAM_RETRY_SECONDS", 0)
    calls: list[str] = []

    def flaky(_token, _chat_id, text):
        calls.append(text)
        if len(calls) == 1:
            raise error

    monkeypatch.setattr(bot, "send_telegram", flaky)

    bot.send_with_retry("tok", "42", "oi")

    assert calls == ["oi", "oi"]


def test_send_with_retry_gives_up_at_once_on_bad_request(monkeypatch) -> None:
    calls: list[str] = []

    def bad(_token, _chat_id, text):
        calls.append(text)
        raise _http_error(400)

    monkeypatch.setattr(bot, "send_telegram", bad)

    with pytest.raises(requests.HTTPError):
        bot.send_with_retry("tok", "42", "oi")
    assert calls == ["oi"]


def test_format_notification_escapes_title_and_error() -> None:
    project = _project("x", "App <b>&</b>")

    assert bot.format_notification(project, None) == (
        "🆕 <b>App &lt;b&gt;&amp;&lt;/b&gt;</b>\nhttps://www.workana.com/job/x"
    )
    assert bot.format_notification(project, "falha <i>").endswith(
        "\n\n⚠️ Proposta não gerada: falha &lt;i&gt;"
    )


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


def _call_claude() -> dict:
    return bot.call_claude(
        "oauth-tok",
        "prompt",
        "<vaga>conteúdo</vaga>",
        model=bot.FIT_MODEL,
        schema=bot.JobFit,
        timeout=bot.FIT_TIMEOUT_SECONDS,
        effort="medium",
    )


def test_call_claude_runs_print_mode_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    calls = _fake_claude(monkeypatch, output={"structured_output": {"is_match": True}})

    assert _call_claude() == {"is_match": True}
    cmd = calls[0]["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == bot.FIT_MODEL
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--system-prompt") + 1] == "prompt"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == bot.JobFit.model_json_schema()
    assert calls[0]["input"] == "<vaga>conteúdo</vaga>"
    assert calls[0]["timeout"] == bot.FIT_TIMEOUT_SECONDS
    env = calls[0]["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert "ANTHROPIC_API_KEY" not in env
    # A plain answer: no agent flags, outside the repo
    for flag in ("--allowedTools", "--add-dir", "--permission-mode", "--append-system-prompt"):
        assert flag not in cmd
    assert calls[0]["cwd"] == bot.tempfile.gettempdir()


def test_call_claude_raises_on_cli_failure(monkeypatch) -> None:
    _fake_claude(
        monkeypatch, returncode=1, output={"is_error": True, "api_error_status": 401}
    )

    with pytest.raises(bot.ProposalError, match="código 1.*api_error_status=401"):
        _call_claude()


def test_call_claude_raises_on_non_json_output(monkeypatch) -> None:
    monkeypatch.setattr(
        bot.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="boom")
    )

    with pytest.raises(bot.ProposalError, match="código 1"):
        _call_claude()


def test_call_claude_raises_without_structured_output(monkeypatch) -> None:
    _fake_claude(monkeypatch, output={"is_error": True, "subtype": "error_max_turns"})

    with pytest.raises(bot.ProposalError, match="subtype=error_max_turns"):
        _call_claude()


SKILL_TEXT = "# proposta-freela de teste\nSiga os Passos 1 a 5."
AGENT_FILES = {bot.PROPOSAL_FILE: "Proposta.", bot.ANALYSIS_FILE: "Análise."}
AGENT_SUMMARY = {"price": "R$ 6 000", "deadline": "1 dia + 3 semanas"}


@pytest.fixture
def skill(workdir, monkeypatch):
    """The skill as on the runner: a ~/.claude/skills symlink into the dotfiles checkout."""
    target = workdir / "dotfiles" / ".claude" / "skills" / "proposta-freela"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(SKILL_TEXT)
    link = workdir / "home" / ".claude" / "skills" / "proposta-freela"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    monkeypatch.setattr(bot, "SKILL_DIR", link)
    monkeypatch.setattr(bot, "SKILL_FILE", link / "SKILL.md")
    return target


def _fake_agent(
    monkeypatch,
    files: dict[str, str] | None = None,
    returncode: int = 0,
    error: Exception | None = None,
) -> list[dict]:
    """Fakes the agent: writes `files` (AGENT_FILES by default) in its cwd, then answers
    AGENT_SUMMARY or raises `error`."""
    written = AGENT_FILES if files is None else files
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        for name, text in written.items():
            (kwargs["cwd"] / name).write_text(text)
        if error is not None:
            raise error
        return SimpleNamespace(
            returncode=returncode, stdout=json.dumps({"structured_output": AGENT_SUMMARY})
        )

    monkeypatch.setattr(bot.subprocess, "run", fake_run)
    return calls


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def _flag_values(cmd: list[str], name: str) -> list[str]:
    """The values of a flag that takes a list: everything up to the next flag."""
    start = cmd.index(name) + 1
    end = next((i for i in range(start, len(cmd)) if cmd[i].startswith("--")), len(cmd))
    return cmd[start:end]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("App de Agendamento — Clínica Estética!", "app-de-agendamento-clinica-estetica"),
        ("  Ação: São João / iOS & Android  ", "acao-sao-joao-ios-android"),
        ("", "vaga"),
        ("!!! ???", "vaga"),
        # Cut back to the last whole word under SLUG_MAX_CHARS
        ("palavra " * 20, "-".join(["palavra"] * 7)),
        # The hyphen right after the limit still counts as a word end
        ("a" * 60 + " b", "a" * 60),
        # A single word longer than the limit is cut at it
        ("a" * 80, "a" * 60),
    ],
)
def test_proposal_slug(title: str, expected: str) -> None:
    slug = bot.proposal_slug(title)

    assert slug == expected
    assert len(slug) <= bot.SLUG_MAX_CHARS
    assert not slug.startswith("-") and not slug.endswith("-")


def test_proposal_folder_uses_sao_paulo_time_and_suffix(workdir) -> None:
    # 01:30 UTC is still the day before in Brasília (UTC-3)
    now = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)

    first = bot.proposal_folder("App Novo", now)
    assert first == bot.GENERATED_DIR / "2026-10-06-22-30-workana-app-novo"
    # Only the path: generate_proposal creates the folder
    assert not first.exists()

    first.mkdir(parents=True)
    second = bot.proposal_folder("App Novo", now)
    assert second.name == "2026-10-06-22-30-workana-app-novo-2"
    second.mkdir()
    assert bot.proposal_folder("App Novo", now).name == "2026-10-06-22-30-workana-app-novo-3"


def test_generate_proposal_runs_agent_with_restricted_tools(skill, monkeypatch) -> None:
    calls = _fake_agent(monkeypatch)
    project = _project("x", "App Novo")

    proposal = bot.generate_proposal("oauth-tok", bot.SKILL_FILE.read_text(), project, "descrição")

    folder = bot.GENERATED_DIR / proposal.folder
    assert proposal == bot.Proposal(
        folder=folder.name, price="R$ 6 000", deadline="1 dia + 3 semanas"
    )
    assert folder.name.endswith("-workana-app-novo")
    assert (folder / bot.PROPOSAL_FILE).read_text() == "Proposta."
    [call] = calls
    cmd = call["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert _flag(cmd, "--model") == "claude-sonnet-5-5"
    assert _flag(cmd, "--effort") == "medium"
    # The skill as it is, as the system prompt; the bot's rules only say how it runs
    assert _flag(cmd, "--system-prompt") == SKILL_TEXT
    instructions = _flag(cmd, "--append-system-prompt")
    assert instructions == bot.BOT_INSTRUCTIONS.format(folder=folder)
    assert str(folder) in instructions and "Pule o Passo 5" in instructions
    assert _flag(cmd, "--tools") == "Read,Write,Edit,Bash"
    home_skill = bot.SKILL_DIR.as_posix().lstrip("/")
    real_skill = skill.resolve().as_posix().lstrip("/")
    assert home_skill != real_skill
    proposal_dir = folder.as_posix().lstrip("/")
    assert _flag_values(cmd, "--allowedTools") == [
        "Read(~/.claude/skills/proposta-freela/**)",
        f"Read(//{home_skill}/**)",
        f"Read(//{real_skill}/**)",
        # Bash in the ~ form the skill writes, plus the symlink and its target: the agent
        # expands ~ to the absolute path
        "Bash(uv run ~/.claude/skills/proposta-freela/scripts/preco.py:*)",
        "Bash(uv run ~/.claude/skills/proposta-freela/scripts/varredura.py:*)",
        f"Bash(uv run /{home_skill}/scripts/preco.py:*)",
        f"Bash(uv run /{home_skill}/scripts/varredura.py:*)",
        f"Bash(uv run /{real_skill}/scripts/preco.py:*)",
        f"Bash(uv run /{real_skill}/scripts/varredura.py:*)",
        f"Read(//{proposal_dir}/**)",
        f"Write(//{proposal_dir}/**)",
        f"Edit(//{proposal_dir}/**)",
        "Bash(date:*)",
    ]
    # The skill is the only extra dir: the folder is the cwd
    assert _flag_values(cmd, "--add-dir") == [str(bot.SKILL_DIR)]
    assert cmd.count("--add-dir") == 1
    assert _flag(cmd, "--permission-mode") == "dontAsk"
    assert _flag(cmd, "--setting-sources") == ""
    assert "--no-session-persistence" in cmd
    assert _flag(cmd, "--output-format") == "json"
    assert json.loads(_flag(cmd, "--json-schema")) == bot.ProposalSummary.model_json_schema()
    assert call["cwd"] == folder
    assert call["timeout"] == bot.PROPOSAL_TIMEOUT_SECONDS
    assert call["input"] == bot.job_content(project, "descrição")


def test_agent_runs_with_minimal_env(skill, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/runner")
    monkeypatch.setenv("UV_CACHE_DIR", "/home/runner/.cache/uv")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-environ")
    monkeypatch.setenv("TELEGRAM_TOKEN", "telegram-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    calls = _fake_agent(monkeypatch)

    bot.generate_proposal("oauth-tok", SKILL_TEXT, _project("x", "App novo"), "descrição")

    env = calls[0]["env"]
    assert {k for k in env if not k.startswith(bot.UV_ENV_PREFIX)} <= set(bot.AGENT_ENV_KEYS)
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["HOME"] == "/home/runner"
    assert env["UV_CACHE_DIR"] == "/home/runner/.cache/uv"
    # The token given wins over one already in the environment
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok"
    assert not any(k.startswith("TELEGRAM_") for k in env)
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env


@pytest.mark.parametrize(
    ("files", "returncode", "error", "match"),
    [
        ({}, 0, None, "não gravou"),
        ({bot.PROPOSAL_FILE: "Proposta."}, 0, None, "não gravou"),
        ({bot.PROPOSAL_FILE: "Proposta.", bot.ANALYSIS_FILE: " \n"}, 0, None, "não gravou"),
        (None, 1, None, "código 1"),
        (None, 0, subprocess.TimeoutExpired("claude", 1200), None),
    ],
    ids=["no-files", "only-proposal", "blank-analysis", "cli-error", "timeout"],
)
def test_generate_proposal_cleans_folder_on_failure(
    skill, monkeypatch, files, returncode, error, match
) -> None:
    _fake_agent(monkeypatch, files=files, returncode=returncode, error=error)
    expected = bot.ProposalError if error is None else subprocess.TimeoutExpired

    with pytest.raises(expected, match=match):
        bot.generate_proposal("oauth-tok", SKILL_TEXT, _project("x", "App novo"), "descrição")

    # The run's folder is gone, with whatever the agent left in it
    assert bot.GENERATED_DIR.is_dir()
    assert list(bot.GENERATED_DIR.iterdir()) == []


def test_check_fit_uses_fit_prompt_file(workdir, monkeypatch) -> None:
    calls = _fake_claude(
        monkeypatch, output={"structured_output": {"is_match": False}}
    )

    fit = bot.check_fit("tok", bot.load_fit_prompt(), _project("x", "Site novo"), "descrição")

    assert fit == bot.JobFit(is_match=False)
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == bot.FIT_MODEL
    assert "--effort" not in cmd
    # The file's text without its trailing newline, as the old literal was
    assert cmd[cmd.index("--system-prompt") + 1] == FIT_PROMPT_TEXT
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == bot.JobFit.model_json_schema()
    assert "Site novo" in calls[0]["input"] and "descrição" in calls[0]["input"]
    assert calls[0]["timeout"] == bot.FIT_TIMEOUT_SECONDS


def test_telegram_len_counts_text_after_parsing() -> None:
    assert bot.telegram_len("<b>Preço:</b> R$ 1 &amp; 2 🆕") == len("Preço: R$ 1 & 2 ") + 2


def test_split_message_keeps_message_whose_tags_pass_the_limit() -> None:
    # 100 raw characters, but only 90 once Telegram strips the tags
    text = "<b>x</b>" + "y" * 85 + "<b></b>"
    assert bot.split_message(text, limit=95) == [text]


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


def test_main_saves_seen_after_each_alert(workdir, monkeypatch) -> None:
    def send_then_die(_token, _chat_id, text):
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


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [("socks5://127.0.0.1:40000", {"server": "socks5://127.0.0.1:40000"}), (None, None)],
)
def test_open_browser_routes_through_browser_proxy(monkeypatch, proxy, expected):
    launches: list[dict] = []

    class FakeChromium:
        def launch(self, **kwargs):
            launches.append(kwargs)
            return SimpleNamespace(close=lambda: None)

    @contextmanager
    def fake_sync_playwright():
        yield SimpleNamespace(chromium=FakeChromium())

    monkeypatch.setattr(bot, "sync_playwright", fake_sync_playwright)
    if proxy:
        monkeypatch.setenv("BROWSER_PROXY", proxy)
    else:
        monkeypatch.delenv("BROWSER_PROXY", raising=False)

    with bot.open_browser():
        pass

    assert launches == [{"headless": True, "proxy": expected}]
