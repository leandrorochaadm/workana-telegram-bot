import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import bot


KEYWORDS = ["aplicativo", "app"]

# bot.py's only argument, as the workflows pass it
MONITOR_ARGV = ["monitor"]
PROPOSE_ARGV = ["propose"]


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
    # Never the developer's own ~/.claude skill: missing unless the skill fixture sets it up
    skill_dir = tmp_path / "home" / ".claude" / "skills" / "proposta-freela"
    monkeypatch.setattr(bot, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(bot, "SKILL_FILE", skill_dir / "SKILL.md")
    monkeypatch.setattr(bot, "TELEGRAM_RETRY_SECONDS", 0)
    for var in (
        "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KEYWORDS", "EXCLUDE_KEYWORDS",
        "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_OUTPUT",
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


def _queued(pid: str, title: str, status: bot.JobStatus, attempts: int = 0, **extra) -> dict:
    """A pending.json entry as load_pending returns it."""
    return {
        "title": title,
        "url": _project(pid, title)["url"],
        "status": status,
        "attempts": attempts,
        **extra,
    }


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

    bot.main(MONITOR_ARGV)

    assert len(sent) == 1
    assert "App novo" in sent[0]
    assert bot.load_seen() == {"old", "new", "site"}


def test_main_escapes_html_in_title(workdir, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App <b>&</b>")])

    bot.main(MONITOR_ARGV)

    assert "App &lt;b&gt;&amp;&lt;/b&gt;" in sent[0]


def test_main_retries_failed_send_on_next_run(workdir, sent, monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [_project("ok", "App ok"), _project("fail", "App FALHA")],
    )

    with pytest.raises(SystemExit) as exc:
        bot.main(MONITOR_ARGV)

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
        bot.main(MONITOR_ARGV)

    assert bot.load_seen() == {"site"}


def test_main_skips_excluded_jobs_including_queued_ones(workdir, sent, monkeypatch) -> None:
    with bot.ENV_FILE.open("a") as f:
        f.write("EXCLUDE_KEYWORDS=jogo, wordpress ,\n")
    bot.save_pending(
        {
            "old": _queued("old", "App de jogo", bot.JobStatus.TRIAGE, attempts=1),
            # Proposal already given up on: its alert still goes out from proposal.yml
            "pronto": _queued("pronto", "App de jogo pronto", bot.JobStatus.ALERT),
        }
    )
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [_project("wp", "App WordPress"), _project("ok", "App de delivery")],
    )

    bot.main(MONITOR_ARGV)

    assert len(sent) == 1
    assert "App de delivery" in sent[0]
    assert bot.load_seen() == {"old", "wp", "ok"}
    assert set(bot.load_pending()) == {"pronto"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "false"),
        # Without the triage the monitor alerts it at once: nothing left for proposal.yml
        (bot.JobStatus.TRIAGE, "false"),
        (bot.JobStatus.PROPOSAL, "true"),
        (bot.JobStatus.ALERT, "true"),
    ],
    ids=["empty", "triage", "proposal", "alert"],
)
def test_monitor_writes_has_queue_output(workdir, sent, monkeypatch, status, expected) -> None:
    output = workdir / "github_output"
    output.write_text("earlier=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if status is not None:
        bot.save_pending({"x": _queued("x", "App novo", status)})
    monkeypatch.setattr(bot, "fetch_projects", lambda: [])

    bot.main(MONITOR_ARGV)

    # Appended: the file holds the other steps' outputs too
    assert output.read_text() == f"earlier=1\nhas_queue={expected}\n"


def test_monitor_writes_has_queue_when_listing_fails(workdir, monkeypatch) -> None:
    output = workdir / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    bot.save_pending({"x": _queued("x", "App novo", bot.JobStatus.PROPOSAL)})

    def blocked_listing() -> list[dict[str, str]]:
        raise RuntimeError("Cloudflare")

    monkeypatch.setattr(bot, "fetch_projects", blocked_listing)

    with pytest.raises(RuntimeError):
        bot.main(MONITOR_ARGV)

    # The queue from earlier runs still gets its proposal.yml dispatch
    assert output.read_text() == "has_queue=true\n"
    assert set(bot.load_pending()) == {"x"}


def test_main_exits_without_credentials(workdir) -> None:
    bot.ENV_FILE.write_text("KEYWORDS=app\n")

    with pytest.raises(SystemExit) as exc:
        bot.main(MONITOR_ARGV)

    assert exc.value.code == 1


def test_main_exits_without_keywords(workdir) -> None:
    bot.ENV_FILE.write_text("TELEGRAM_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n")

    with pytest.raises(SystemExit) as exc:
        bot.main(MONITOR_ARGV)

    assert exc.value.code == 1


@pytest.mark.parametrize(
    "argv", [[], ["outro"], ["Monitor"]], ids=["missing", "unknown", "wrong-case"]
)
def test_main_rejects_unknown_mode(workdir, sent, monkeypatch, argv) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: pytest.fail("read Workana"))

    with pytest.raises(SystemExit) as exc:
        bot.main(argv)

    # argparse's usage error, before any setting or state is read
    assert exc.value.code == 2
    assert sent == []


def test_propose_runs_without_keywords(workdir, sent, monkeypatch) -> None:
    # proposal.yml passes no KEYWORDS: only the monitor reads the listing
    bot.ENV_FILE.write_text("TELEGRAM_TOKEN=tok\nTELEGRAM_CHAT_ID=42\n")
    monkeypatch.setattr(bot, "fetch_projects", lambda: pytest.fail("propose read Workana"))

    bot.main(PROPOSE_ARGV)

    assert sent == []


@pytest.mark.parametrize("argv", [MONITOR_ARGV, PROPOSE_ARGV], ids=["monitor", "propose"])
def test_exits_without_data_dir(workdir, sent, monkeypatch, capsys, argv) -> None:
    bot.DATA_DIR.rmdir()

    def fetch_without_state() -> list[dict[str, str]]:
        raise AssertionError("read Workana without the state checkout")

    monkeypatch.setattr(bot, "fetch_projects", fetch_without_state)

    with pytest.raises(SystemExit) as exc:
        bot.main(argv)

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


def _drop_token(monkeypatch) -> None:
    bot.ENV_FILE.write_text(
        bot.ENV_FILE.read_text().replace("CLAUDE_CODE_OAUTH_TOKEN=oauth-test\n", "")
    )


def _drop_claude(monkeypatch) -> None:
    monkeypatch.setattr(bot.shutil, "which", lambda _cmd: None)


def _drop_fit_prompt(monkeypatch) -> None:
    bot.FIT_PROMPT_FILE.unlink()


def _blank_fit_prompt(monkeypatch) -> None:
    bot.FIT_PROMPT_FILE.write_text(" \n")


def _drop_skill(monkeypatch) -> None:
    bot.SKILL_FILE.unlink()


def test_monitor_queues_approved_job_with_description(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(
        bot,
        "fetch_projects",
        lambda: [_project("x", "App novo"), _project("logo", "App de logo")],
    )

    bot.main(MONITOR_ARGV)

    assert with_triage == [
        ("oauth-test", FIT_PROMPT_TEXT, "x", "descrição de https://www.workana.com/job/x"),
        ("oauth-test", FIT_PROMPT_TEXT, "logo", "descrição de https://www.workana.com/job/logo"),
    ]
    # The alert goes out from proposal.yml, with the proposal
    assert sent == []
    assert bot.load_pending() == {
        "x": _queued(
            "x",
            "App novo",
            bot.JobStatus.PROPOSAL,
            description="descrição de https://www.workana.com/job/x",
        )
    }
    assert set(bot.load_rejected()) == {"logo"}
    assert bot.load_seen() == {"logo"}


def test_main_defers_job_whose_page_fails_to_next_run(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("quebra", "App quebra")])

    bot.main(MONITOR_ARGV)

    assert with_triage == []
    assert sent == []
    assert bot.load_seen() == set()
    assert bot.load_pending() == {
        "quebra": _queued("quebra", "App quebra", bot.JobStatus.TRIAGE, attempts=1)
    }


def test_main_alerts_after_max_attempts_reading_the_page(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("quebra", "App quebra")])

    for _ in range(bot.MAX_JOB_ATTEMPTS):
        bot.main(MONITOR_ARGV)

    assert with_triage == []
    [alert] = sent
    assert "App quebra" in alert
    assert "Proposta não gerada: erro ao ler a vaga" in alert
    assert bot.load_seen() == {"quebra"}
    assert bot.load_pending() == {}


def test_main_retries_pending_job_that_left_the_listing(with_triage, sent, monkeypatch) -> None:
    bot.save_pending({"x": _queued("x", "App antigo", bot.JobStatus.TRIAGE, attempts=1)})
    monkeypatch.setattr(bot, "fetch_projects", lambda: [])

    bot.main(MONITOR_ARGV)

    assert len(with_triage) == 1
    assert sent == []
    assert bot.load_pending() == {
        "x": _queued(
            "x",
            "App antigo",
            bot.JobStatus.PROPOSAL,
            attempts=1,
            description="descrição de https://www.workana.com/job/x",
        )
    }


def test_main_skips_rejected_job_silently(with_triage, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App de logo")])

    bot.main(MONITOR_ARGV)

    assert len(with_triage) == 1
    assert sent == []
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}
    [(pid, job)] = bot.load_rejected().items()
    assert pid == "x"
    assert "reason" not in job
    assert job["url"] == "https://www.workana.com/job/x" and job["date"]


def test_main_queues_job_when_triage_fails(with_triage, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App triagem")])

    bot.main(MONITOR_ARGV)

    # Fails open: the job goes on to the proposal
    assert sent == []
    assert bot.load_pending()["x"]["status"] is bot.JobStatus.PROPOSAL
    err = capsys.readouterr().err
    assert "Triagem falhou" in err and "ValueError" in err
    assert "sigiloso" not in err
    assert bot.load_seen() == set()


def test_main_logs_triage_error_details(with_triage, sent, monkeypatch, capsys) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App vencido")])

    bot.main(MONITOR_ARGV)

    assert "api_error_status=401" in capsys.readouterr().err
    assert sent == []
    assert bot.load_pending()["x"]["status"] is bot.JobStatus.PROPOSAL


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        (_drop_token, "CLAUDE_CODE_OAUTH_TOKEN"),
        (_drop_claude, "Claude Code não instalado"),
        (_drop_fit_prompt, "fit_prompt.md"),
        (_blank_fit_prompt, "fit_prompt.md"),
    ],
    ids=["no-token", "no-claude", "no-fit-prompt", "blank-fit-prompt"],
)
def test_monitor_alerts_only_without_triage_requirements(
    with_triage, sent, monkeypatch, capsys, drop, message
) -> None:
    drop(monkeypatch)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main(MONITOR_ARGV)

    # No triage, so no Claude call and no proposal: the alert goes out at once
    assert with_triage == []
    assert sent == ["🆕 <b>App novo</b>\nhttps://www.workana.com/job/x"]
    assert message in capsys.readouterr().err
    assert bot.load_seen() == {"x"}
    assert bot.load_pending() == {}


def test_main_survives_unexpected_error_without_leaking_it(
    with_triage, sent, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("inesperado", "App inesperado")])

    bot.main(MONITOR_ARGV)

    assert bot.load_pending()["inesperado"]["attempts"] == 1
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "sigiloso" not in err


def test_main_keeps_job_pending_when_alert_fails(workdir, monkeypatch) -> None:
    messages: list[str] = []

    def fake_send(_token, _chat_id, text):
        messages.append(text)
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(bot, "send_telegram", fake_send)
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    with pytest.raises(SystemExit) as exc:
        bot.main(MONITOR_ARGV)

    assert exc.value.code == 1
    assert len(messages) == bot.TELEGRAM_SEND_ATTEMPTS
    # Retried next run, whole alert, with nothing else stored
    assert bot.load_seen() == set()
    assert bot.load_pending() == {"x": _queued("x", "App novo", bot.JobStatus.TRIAGE)}


def test_main_stops_triage_after_deadline(with_triage, sent, monkeypatch) -> None:
    clock = iter([0.0, bot.TRIAGE_DEADLINE_SECONDS + 1])
    monkeypatch.setattr(bot.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main(MONITOR_ARGV)

    assert with_triage == []
    assert sent == []
    assert bot.load_pending() == {"x": _queued("x", "App novo", bot.JobStatus.TRIAGE)}


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

    assert bot.format_notification(project, None, None) == (
        "🆕 <b>App &lt;b&gt;&amp;&lt;/b&gt;</b>\nhttps://www.workana.com/job/x"
    )
    assert bot.format_notification(project, None, "falha <i>").endswith(
        "\n\n⚠️ Proposta não gerada: falha &lt;i&gt;"
    )


def test_format_notification_shows_price_deadline_and_link() -> None:
    proposal = bot.Proposal(
        folder="2026-10-07-10-00-workana-app", price="R$ <6 000>", deadline="1 dia & 3 semanas"
    )

    text = bot.format_notification(_project("x", "App"), proposal, "ignorado", published=True)

    # The proposal wins over an error
    assert text == (
        "🆕 <b>App</b>\nhttps://www.workana.com/job/x\n\n"
        "💰 <b>Preço:</b> R$ &lt;6 000&gt;\n⏱ <b>Prazo:</b> 1 dia &amp; 3 semanas\n"
        '📄 <a href="https://github.com/leandrorochaadm/proposta/blob/main/generated/'
        '2026-10-07-10-00-workana-app/proposta.md">Proposta</a>'
    )
    # Not on GitHub yet: a warning in place of a link that would not open
    unpublished = bot.format_notification(_project("x", "App"), proposal, None)
    assert unpublished.endswith(
        "⏱ <b>Prazo:</b> 1 dia &amp; 3 semanas\n⚠️ Proposta gravada, mas não subiu para o GitHub."
    )
    assert "github.com" not in unpublished


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


AGENT_PROPOSAL = bot.Proposal(
    folder="2026-10-07-10-00-workana-app-a", price="R$ 6 000", deadline="1 dia + 3 semanas"
)


def _alert_tail(proposal: bot.Proposal) -> str:
    """The alert's lines after the job link, for a proposal that reached GitHub."""
    link = (
        "https://github.com/leandrorochaadm/proposta/blob/main/generated/"
        f"{proposal.folder}/proposta.md"
    )
    return (
        f"\n\n💰 <b>Preço:</b> {proposal.price}\n⏱ <b>Prazo:</b> {proposal.deadline}"
        f'\n📄 <a href="{link}">Proposta</a>'
    )


AGENT_ALERT_TAIL = _alert_tail(AGENT_PROPOSAL)


def _write_folder(proposal: bot.Proposal) -> Path:
    """The proposal's folder in GENERATED_DIR, as the agent leaves it."""
    folder = bot.GENERATED_DIR / proposal.folder
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in AGENT_FILES.items():
        (folder / name).write_text(text)
    return folder


@pytest.fixture
def published(monkeypatch):
    """Fakes publish_proposal, so no test runs git on its own.

    `calls` gets (folder, pending.json as on disk at that moment) per call; the push
    fails while `ok` is False.
    """
    state = SimpleNamespace(calls=[], ok=True)

    def fake_publish(folder: str) -> bool:
        state.calls.append((folder, json.loads(bot.PENDING_FILE.read_text())))
        return state.ok

    monkeypatch.setattr(bot, "publish_proposal", fake_publish)
    return state


@pytest.fixture
def with_agent(skill, published, monkeypatch):
    """proposal.yml's setup: OAuth token, the skill (from the skill fixture), the claude
    CLI and, through the published fixture, a fake publish_proposal.

    Returns (token, skill text, job id, description) for each generate_proposal call. The
    agent writes AGENT_PROPOSAL's folder, or fails on jobs whose title has "erro".
    """
    with bot.ENV_FILE.open("a") as f:
        f.write("CLAUDE_CODE_OAUTH_TOKEN=oauth-test\n")
    calls: list[tuple[str, str, str, str]] = []

    def fake_generate_proposal(oauth_token, skill_text, project, description):
        calls.append((oauth_token, skill_text, project["id"], description))
        if "erro" in project["title"]:
            raise ValueError("saída do agente com texto sigiloso")
        _write_folder(AGENT_PROPOSAL)
        return AGENT_PROPOSAL

    monkeypatch.setattr(bot, "generate_proposal", fake_generate_proposal)
    monkeypatch.setattr(bot.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    return calls


def test_pending_roundtrip_keeps_status_description_and_proposal(workdir) -> None:
    pending = {
        "a": _queued(
            "a",
            "App a",
            bot.JobStatus.ALERT,
            attempts=1,
            description="descrição",
            proposal=AGENT_PROPOSAL,
        ),
        "b": _queued("b", "App b", bot.JobStatus.TRIAGE),
    }

    bot.save_pending(pending)

    # On disk, the outside spelling: status as text, proposal as an object
    stored = json.loads(bot.PENDING_FILE.read_text())
    assert stored["a"]["status"] == "alert"
    assert stored["a"]["proposal"] == {
        "folder": "2026-10-07-10-00-workana-app-a",
        "price": "R$ 6 000",
        "deadline": "1 dia + 3 semanas",
    }
    assert stored["b"] == {
        "title": "App b",
        "url": "https://www.workana.com/job/b",
        "status": "triage",
        "attempts": 0,
    }
    # save_pending left the queue in memory typed
    assert pending["a"]["status"] is bot.JobStatus.ALERT
    loaded = bot.load_pending()
    assert loaded == pending
    assert loaded["a"]["status"] is bot.JobStatus.ALERT
    assert loaded["a"]["proposal"] == AGENT_PROPOSAL


def test_job_status_from_api_falls_back_to_triage(workdir, capsys) -> None:
    for status in bot.JobStatus:
        assert bot.JobStatus.from_api(status.value, "job-1") is status
    assert capsys.readouterr().err == ""
    bot.PENDING_FILE.write_text(
        json.dumps(
            {
                "job-42": {"title": "App", "url": "u", "status": "revisao-secreta", "attempts": 1},
                "job-43": {"title": "App", "url": "u", "attempts": 0},
            }
        )
    )

    pending = bot.load_pending()

    assert pending["job-42"]["status"] is bot.JobStatus.TRIAGE
    assert pending["job-43"]["status"] is bot.JobStatus.TRIAGE
    err = capsys.readouterr().err
    assert "job-42" in err and "job-43" in err
    # Only the id: the raw value may be anything a hand edit put there
    assert "revisao-secreta" not in err


def test_propose_handles_one_job_per_run(with_agent, sent) -> None:
    bot.save_pending(
        {
            "a": _queued("a", "App a", bot.JobStatus.PROPOSAL, description="descrição a"),
            "b": _queued("b", "App b", bot.JobStatus.PROPOSAL, description="descrição b"),
        }
    )

    bot.main(PROPOSE_ARGV)

    # Oldest first, one agent call per run
    assert with_agent == [("oauth-test", SKILL_TEXT, "a", "descrição a")]
    assert sent == [f"🆕 <b>App a</b>\nhttps://www.workana.com/job/a{AGENT_ALERT_TAIL}"]
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {
        "b": _queued("b", "App b", bot.JobStatus.PROPOSAL, description="descrição b")
    }


def test_propose_sends_alert_status_first_without_agent(with_agent, published, sent) -> None:
    written = bot.Proposal(
        folder="2026-10-06-09-00-workana-app-b", price="R$ 3 000", deadline="2 semanas"
    )
    _write_folder(written)
    bot.save_pending(
        {
            "a": _queued("a", "App a", bot.JobStatus.PROPOSAL, description="descrição a"),
            "b": _queued(
                "b", "App b", bot.JobStatus.ALERT, description="descrição b", proposal=written
            ),
        }
    )

    bot.main(PROPOSE_ARGV)

    # b only missed its alert: it goes first, with no agent call and no new commit,
    # though a is older
    assert sent == [
        f"🆕 <b>App b</b>\nhttps://www.workana.com/job/b{_alert_tail(written)}",
        f"🆕 <b>App a</b>\nhttps://www.workana.com/job/a{AGENT_ALERT_TAIL}",
    ]
    assert with_agent == [("oauth-test", SKILL_TEXT, "a", "descrição a")]
    assert [folder for folder, _ in published.calls] == [AGENT_PROPOSAL.folder]
    assert bot.load_seen() == {"a", "b"}
    assert bot.load_pending() == {}


def test_alert_status_is_resent_without_regenerating(
    with_agent, published, sent, monkeypatch
) -> None:
    bot.save_pending(
        {"a": _queued("a", "App FALHA", bot.JobStatus.PROPOSAL, description="descrição a")}
    )

    with pytest.raises(SystemExit) as exc:
        bot.main(PROPOSE_ARGV)

    assert exc.value.code == 1
    assert sent == []
    # Written and published; only the alert is missing
    assert bot.load_pending() == {
        "a": _queued(
            "a",
            "App FALHA",
            bot.JobStatus.ALERT,
            description="descrição a",
            proposal=AGENT_PROPOSAL,
        )
    }
    assert [folder for folder, _ in published.calls] == [AGENT_PROPOSAL.folder]
    assert bot.load_seen() == set()

    # Telegram is back; the next checkout has the folder
    monkeypatch.setattr(bot, "send_telegram", lambda _token, _chat_id, text: sent.append(text))
    bot.main(PROPOSE_ARGV)

    # Only the alert: no second agent call and no second commit
    assert len(with_agent) == 1
    assert len(published.calls) == 1
    assert sent == [f"🆕 <b>App FALHA</b>\nhttps://www.workana.com/job/a{AGENT_ALERT_TAIL}"]
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {}


def test_alert_has_proposal_link(with_agent, published, sent) -> None:
    bot.save_pending(
        {"a": _queued("a", "App a", bot.JobStatus.PROPOSAL, description="descrição a")}
    )

    bot.main(PROPOSE_ARGV)

    # Published with pending.json already holding the job in ALERT and its proposal: one
    # commit for both
    assert published.calls == [
        (
            "2026-10-07-10-00-workana-app-a",
            {
                "a": {
                    "title": "App a",
                    "url": "https://www.workana.com/job/a",
                    "status": "alert",
                    "attempts": 0,
                    "description": "descrição a",
                    "proposal": {
                        "folder": "2026-10-07-10-00-workana-app-a",
                        "price": "R$ 6 000",
                        "deadline": "1 dia + 3 semanas",
                    },
                }
            },
        )
    ]
    # The link only goes out because the publish came first and said it pushed
    assert sent == [
        "🆕 <b>App a</b>\nhttps://www.workana.com/job/a\n\n"
        "💰 <b>Preço:</b> R$ 6 000\n⏱ <b>Prazo:</b> 1 dia + 3 semanas\n"
        '📄 <a href="https://github.com/leandrorochaadm/proposta/blob/main/generated/'
        '2026-10-07-10-00-workana-app-a/proposta.md">Proposta</a>'
    ]
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {}


def test_alert_without_link_when_push_fails(with_agent, published, sent) -> None:
    published.ok = False
    bot.save_pending(
        {"a": _queued("a", "App a", bot.JobStatus.PROPOSAL, description="descrição a")}
    )

    bot.main(PROPOSE_ARGV)

    assert sent == [
        "🆕 <b>App a</b>\nhttps://www.workana.com/job/a\n\n"
        "💰 <b>Preço:</b> R$ 6 000\n⏱ <b>Prazo:</b> 1 dia + 3 semanas\n"
        "⚠️ Proposta gravada, mas não subiu para o GitHub."
    ]
    # Alerted, so done: the workflow's Save state pushes the folder's commit later
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {}
    assert (bot.GENERATED_DIR / AGENT_PROPOSAL.folder / bot.PROPOSAL_FILE).is_file()


def test_alert_status_with_missing_folder_regenerates(with_agent, published, sent, capsys) -> None:
    lost = bot.Proposal(folder="2026-10-06-09-00-workana-app-a", price="R$ 1 000", deadline="1 dia")
    bot.save_pending(
        {
            "a": _queued(
                "a",
                "App a",
                bot.JobStatus.ALERT,
                attempts=1,
                description="descrição a",
                proposal=lost,
            )
        }
    )

    bot.main(PROPOSE_ARGV)

    # Its commit never reached origin: written again, published and alerted with the
    # new folder, never with the lost one
    assert with_agent == [("oauth-test", SKILL_TEXT, "a", "descrição a")]
    assert [folder for folder, _ in published.calls] == [AGENT_PROPOSAL.folder]
    assert sent == [f"🆕 <b>App a</b>\nhttps://www.workana.com/job/a{AGENT_ALERT_TAIL}"]
    err = capsys.readouterr().err
    assert "https://www.workana.com/job/a" in err and "gerando de novo" in err
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {}


def test_propose_alerts_without_proposal_after_max_attempts(with_agent, sent, capsys) -> None:
    bot.save_pending(
        {"a": _queued("a", "App com erro", bot.JobStatus.PROPOSAL, description="descrição a")}
    )

    bot.main(PROPOSE_ARGV)

    assert sent == []
    assert bot.load_pending() == {
        "a": _queued(
            "a", "App com erro", bot.JobStatus.PROPOSAL, attempts=1, description="descrição a"
        )
    }

    for _ in range(bot.MAX_JOB_ATTEMPTS - 1):
        bot.main(PROPOSE_ARGV)

    assert len(with_agent) == bot.MAX_JOB_ATTEMPTS
    assert sent == [
        "🆕 <b>App com erro</b>\nhttps://www.workana.com/job/a\n\n"
        "⚠️ Proposta não gerada: erro ao gerar a proposta"
    ]
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {}
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "sigiloso" not in err


@pytest.mark.parametrize(
    ("drop", "message"),
    [
        (_drop_skill, "Skill proposta-freela não encontrada"),
        (_drop_token, "Falta CLAUDE_CODE_OAUTH_TOKEN"),
        (_drop_claude, "Claude Code não instalado"),
    ],
    ids=["no-skill", "no-token", "no-claude"],
)
def test_propose_without_skill_counts_attempt(
    with_agent, sent, monkeypatch, capsys, drop, message
) -> None:
    drop(monkeypatch)
    bot.save_pending(
        {
            "a": _queued("a", "App a", bot.JobStatus.PROPOSAL, description="descrição a"),
            "b": _queued("b", "App b", bot.JobStatus.PROPOSAL, description="descrição b"),
        }
    )

    bot.main(PROPOSE_ARGV)

    assert with_agent == []
    assert sent == []
    assert message in capsys.readouterr().err
    # Only the oldest job pays the try, so the queue still moves
    pending = bot.load_pending()
    assert (pending["a"]["attempts"], pending["b"]["attempts"]) == (1, 0)

    for _ in range(bot.MAX_JOB_ATTEMPTS - 1):
        bot.main(PROPOSE_ARGV)

    assert with_agent == []
    assert sent == [
        "🆕 <b>App a</b>\nhttps://www.workana.com/job/a\n\n"
        "⚠️ Proposta não gerada: erro ao gerar a proposta"
    ]
    assert bot.load_seen() == {"a"}
    assert bot.load_pending() == {
        "b": _queued("b", "App b", bot.JobStatus.PROPOSAL, description="descrição b")
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def proposta_repo(workdir, monkeypatch):
    """The proposta checkout as a real clone of a local bare origin, which it returns.

    The developer's global and system git settings (signing, hooks, default branch)
    stay out, for these commands and for publish_proposal's.
    """
    if shutil.which("git") is None:
        pytest.skip("git não instalado")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = workdir / "origin.git"
    _git(workdir, "init", "-q", "--bare", "-b", "main", str(origin))
    bot.save_seen(set())
    bot.save_pending({})
    _git(bot.PROPOSTA_DIR, "init", "-q", "-b", "main")
    # What proposal.yml's "Configure git author" step sets on the runner
    _git(bot.PROPOSTA_DIR, "config", "user.name", "github-actions[bot]")
    _git(bot.PROPOSTA_DIR, "config", "user.email", "bot@example.com")
    _git(bot.PROPOSTA_DIR, "add", "-A")
    _git(bot.PROPOSTA_DIR, "commit", "-q", "-m", "chore: import state")
    _git(bot.PROPOSTA_DIR, "remote", "add", "origin", str(origin))
    _git(bot.PROPOSTA_DIR, "push", "-q", "-u", "origin", "main")
    return origin


def test_publish_proposal_commits_folder_and_pending(proposta_repo, workdir) -> None:
    # A proposal written by hand reached origin meanwhile: the publish rebases on it
    other = workdir / "other"
    _git(workdir, "clone", "-q", str(proposta_repo), str(other))
    _git(other, "config", "user.name", "Leandro")
    _git(other, "config", "user.email", "leandro@example.com")
    (other / "generated" / "manual").mkdir(parents=True)
    (other / "generated" / "manual" / "proposta.md").write_text("Feita à mão.")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "chore: add manual proposal")
    _git(other, "push", "-q")
    folder = _write_folder(AGENT_PROPOSAL)
    bot.save_pending(
        {
            "a": _queued(
                "a", "App a", bot.JobStatus.ALERT, description="descrição a", proposal=AGENT_PROPOSAL
            )
        }
    )
    # Alerts sent earlier in the run left seen.json dirty, outside the commit
    bot.save_seen({"old"})

    assert bot.publish_proposal(folder.name) is True

    assert _git(proposta_repo, "log", "--format=%s", "main").splitlines() == [
        "chore: add workana proposal 2026-10-07-10-00-workana-app-a",
        "chore: add manual proposal",
        "chore: import state",
    ]
    # The folder and pending.json in one commit, nothing else
    assert _git(proposta_repo, "show", "--name-only", "--format=", "main").splitlines() == [
        "data/pending.json",
        "generated/2026-10-07-10-00-workana-app-a/analise.md",
        "generated/2026-10-07-10-00-workana-app-a/proposta.md",
    ]
    stored = json.loads(_git(proposta_repo, "show", "main:data/pending.json"))
    assert stored["a"]["status"] == "alert"
    assert stored["a"]["proposal"]["folder"] == "2026-10-07-10-00-workana-app-a"
    # The autostash put seen.json back, still dirty, for the workflow's Save state
    assert _git(bot.PROPOSTA_DIR, "status", "--porcelain") == " M data/seen.json\n"


def test_publish_proposal_logs_only_the_exit_code(proposta_repo, workdir, capsys) -> None:
    _git(bot.PROPOSTA_DIR, "remote", "set-url", "origin", str(workdir / "sumiu.git"))
    folder = _write_folder(AGENT_PROPOSAL)
    bot.save_pending(
        {
            "a": _queued(
                "a", "App a", bot.JobStatus.ALERT, description="descrição a", proposal=AGENT_PROPOSAL
            )
        }
    )

    assert bot.publish_proposal(folder.name) is False

    err = capsys.readouterr().err
    assert err.startswith("Proposta não publicada: git pull saiu com código ")
    # One line, none of git's own output (it names the remote and the files)
    assert err.count("\n") == 1
    assert "sumiu" not in err and "fatal" not in err
    # The commit stays in the clone, for the workflow's Save state to push
    assert _git(bot.PROPOSTA_DIR, "log", "-1", "--format=%s") == (
        "chore: add workana proposal 2026-10-07-10-00-workana-app-a\n"
    )


def test_publish_proposal_needs_proposta_clone(workdir, monkeypatch, capsys) -> None:
    # Without its own .git, `git -C proposta` would commit to an enclosing repo
    monkeypatch.setattr(bot.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("ran git"))

    assert bot.publish_proposal(AGENT_PROPOSAL.folder) is False

    assert "proposta/ não é um clone do git" in capsys.readouterr().err


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
        bot.main(MONITOR_ARGV)

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
