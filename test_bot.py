import json

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


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bot, "SEEN_FILE", tmp_path / "seen.json")
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "KEYWORDS"):
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
