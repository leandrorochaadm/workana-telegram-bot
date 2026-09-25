from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import requests
from cryptography.fernet import Fernet

import bot
import encrypt_prompt

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


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bot, "SEEN_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(bot, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bot, "PROMPT_FILE", tmp_path / "proposal_prompt.enc")
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KEYWORDS", "ANTHROPIC_API_KEY", "PROMPT_KEY"):
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
        f.write(f"ANTHROPIC_API_KEY=sk-test\nPROMPT_KEY={key}\n")
    calls: list[tuple[str, str]] = []

    def fake_generate(_client, system, project, description):
        calls.append((system, description))
        if "quebra" in project["title"]:
            raise RuntimeError("boom")
        if "inesperado" in project["title"]:
            raise ValueError("saída do modelo com texto sigiloso")
        return PROPOSAL

    monkeypatch.setattr(bot, "fetch_description", lambda url: f"descrição de {url}")
    monkeypatch.setattr(bot, "generate_proposal", fake_generate)
    return calls


def test_main_sends_proposal_after_notification(with_proposals, sent, monkeypatch) -> None:
    monkeypatch.setattr(bot, "fetch_projects", lambda: [_project("x", "App novo")])

    bot.main()

    assert with_proposals == [("prompt secreto", "descrição de https://www.workana.com/job/x")]
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


class FakeMessages:
    def __init__(self, parsed_output) -> None:
        self.parsed_output = parsed_output
        self.kwargs: dict = {}

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(parsed_output=self.parsed_output, stop_reason="end_turn")


def test_generate_proposal_sends_prompt_and_job() -> None:
    messages = FakeMessages(PROPOSAL)
    client = SimpleNamespace(messages=messages)

    result = bot.generate_proposal(client, "prompt", _project("x", "App novo"), "descrição")

    assert result is PROPOSAL
    kwargs = messages.kwargs
    assert kwargs["model"] == bot.PROPOSAL_MODEL
    assert kwargs["output_format"] is bot.Proposal
    assert kwargs["system"] == [
        {"type": "text", "text": "prompt", "cache_control": {"type": "ephemeral"}}
    ]
    content = kwargs["messages"][0]["content"]
    assert content.startswith("<vaga>") and content.endswith("</vaga>")
    assert "App novo" in content and "https://www.workana.com/job/x" in content
    assert "descrição" in content


def test_generate_proposal_raises_without_parsed_output() -> None:
    client = SimpleNamespace(messages=FakeMessages(None))

    with pytest.raises(RuntimeError, match="stop_reason=end_turn"):
        bot.generate_proposal(client, "prompt", _project("x", "App"), "descrição")


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
