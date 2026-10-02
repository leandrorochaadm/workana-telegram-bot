from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import usage_report

NOW = datetime(2026, 10, 2, 17, 0, tzinfo=usage_report.LOCAL_TZ)
RESET_5H = int(datetime(2026, 10, 2, 19, 0, tzinfo=usage_report.LOCAL_TZ).timestamp())
RESET_7D = int(datetime(2026, 10, 6, 9, 0, tzinfo=usage_report.LOCAL_TZ).timestamp())


def fake_response(status_code: int = 200, headers: dict[str, str] | None = None):
    return SimpleNamespace(status_code=status_code, headers=headers or {}, text="body")


def usage_headers() -> dict[str, str]:
    prefix = usage_report.HEADER_PREFIX
    return {
        f"{prefix}5h-utilization": "0.32",
        f"{prefix}5h-reset": str(RESET_5H),
        f"{prefix}7d-utilization": "0.48",
        f"{prefix}7d-reset": str(RESET_7D),
    }


@pytest.fixture
def api(monkeypatch):
    calls: list[dict] = []
    state = {"response": fake_response(headers=usage_headers())}

    def fake_post(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return state["response"]

    monkeypatch.setattr(usage_report.requests, "post", fake_post)
    return SimpleNamespace(calls=calls, state=state)


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0, "░" * 20),
        (0.32, "█" * 6 + "░" * 14),
        (1, "█" * 20),
        (1.5, "█" * 20),
        (-0.2, "░" * 20),
    ],
)
def test_progress_bar(fraction: float, expected: str) -> None:
    assert usage_report.progress_bar(fraction) == expected


def test_progress_bar_custom_width() -> None:
    assert usage_report.progress_bar(0.5, width=4) == "██░░"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (str(RESET_5H), datetime(2026, 10, 2, 19, 0, tzinfo=usage_report.LOCAL_TZ)),
        ("2026-10-02T22:00:00Z", datetime(2026, 10, 2, 19, 0, tzinfo=usage_report.LOCAL_TZ)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_reset(value: str | None, expected: datetime | None) -> None:
    assert usage_report.parse_reset(value) == expected


def test_parse_reset_uses_local_timezone() -> None:
    assert usage_report.parse_reset(str(RESET_5H)).tzinfo == usage_report.LOCAL_TZ


@pytest.mark.parametrize(
    ("key", "time_left", "expected"),
    [
        ("5h", timedelta(hours=2), 0.6),
        ("5h", timedelta(hours=5), 0),
        ("5h", timedelta(0), 1),
        ("7d", timedelta(days=3.5), 0.5),
        # Out-of-range resets are clamped instead of producing negative or >100% values
        ("5h", timedelta(hours=9), 0),
        ("7d", timedelta(hours=-1), 1),
    ],
)
def test_elapsed_fraction(key: str, time_left: timedelta, expected: float) -> None:
    assert usage_report.elapsed_fraction(key, NOW + time_left, NOW) == pytest.approx(expected)


def test_format_message_shows_usage_and_time_for_each_window() -> None:
    usage = {
        "5h": (0.32, usage_report.parse_reset(str(RESET_5H))),
        "7d": (0.48, usage_report.parse_reset(str(RESET_7D))),
    }

    assert usage_report.format_message(usage, NOW) == "\n".join(
        [
            "<b>Uso do Claude</b>",
            "",
            "<b>Janela de 5h</b>",
            "█" * 6 + "░" * 14 + " 32% usado",
            "█" * 12 + "░" * 8 + " 60% do tempo",
            "Reinicia em 02/10 às 19:00",
            "",
            "<b>Limite semanal</b>",
            "█" * 10 + "░" * 10 + " 48% usado",
            "█" * 10 + "░" * 10 + " 48% do tempo",
            "Reinicia em 06/10 às 09:00",
        ]
    )


def test_format_message_without_reset_omits_time_bar() -> None:
    message = usage_report.format_message({"5h": (0.32, None), "7d": (0.48, None)}, NOW)

    assert "32% usado" in message
    assert "do tempo" not in message
    assert "Reinicia" not in message


def test_format_message_marks_missing_window() -> None:
    message = usage_report.format_message({"5h": (0.1, None)}, NOW)

    assert message.endswith("<b>Limite semanal</b>\nSem dados.")


def test_fetch_usage_reads_rate_limit_headers(api) -> None:
    usage = usage_report.fetch_usage("oauth")

    assert usage == {
        "5h": (0.32, usage_report.parse_reset(str(RESET_5H))),
        "7d": (0.48, usage_report.parse_reset(str(RESET_7D))),
    }
    (call,) = api.calls
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["Authorization"] == "Bearer oauth"
    assert call["json"]["model"] == usage_report.PROBE_MODEL
    assert call["json"]["max_tokens"] == 1


def test_fetch_usage_reads_headers_when_limit_is_reached(api) -> None:
    api.state["response"] = fake_response(429, usage_headers())

    assert usage_report.fetch_usage("oauth")["5h"][0] == 0.32


def test_fetch_usage_keeps_window_without_reset(api) -> None:
    prefix = usage_report.HEADER_PREFIX
    api.state["response"] = fake_response(headers={f"{prefix}7d-utilization": "0.9"})

    assert usage_report.fetch_usage("oauth") == {"7d": (0.9, None)}


def test_fetch_usage_rejected_token_exits(api) -> None:
    api.state["response"] = fake_response(401)

    with pytest.raises(SystemExit, match="401"):
        usage_report.fetch_usage("oauth")


def test_fetch_usage_without_headers_exits(api) -> None:
    api.state["response"] = fake_response(500)

    with pytest.raises(SystemExit, match="HTTP 500"):
        usage_report.fetch_usage("oauth")


def test_main_requires_all_variables(monkeypatch) -> None:
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok")

    with pytest.raises(SystemExit, match="CLAUDE_CODE_OAUTH_TOKEN, TELEGRAM_CHAT_ID"):
        usage_report.main()


def test_main_sends_report_without_printing_it(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(usage_report, "fetch_usage", lambda token: {"5h": (0.32, None)})
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(usage_report, "send_telegram", lambda *args: sent.append(args))

    usage_report.main()

    ((token, chat_id, text),) = sent
    assert (token, chat_id) == ("tok", "42")
    assert "32% usado" in text
    # Actions logs are public: the report itself must never reach stdout
    assert "32%" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("5h", "Janela de 5h"),
        ("7d", "Limite semanal"),
        ("7d_fable", "Limite semanal do Fable"),
        ("7d-fable", "Limite semanal do Fable"),
        ("5h_opus", "Janela de 5h do Opus"),
        ("7d_oauth_apps", "Limite 7d_oauth_apps"),
        ("overage", "Limite overage"),
    ],
)
def test_window_label(key: str, expected: str) -> None:
    assert usage_report.window_label(key) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("5h", timedelta(hours=5)),
        ("7d_fable", timedelta(days=7)),
        ("overage", None),
    ],
)
def test_window_length(key: str, expected: timedelta | None) -> None:
    assert usage_report.window_length(key) == expected


def test_elapsed_fraction_unknown_window_is_none() -> None:
    assert usage_report.elapsed_fraction("overage", NOW, NOW) is None


def test_fetch_usage_reads_per_model_limits(api) -> None:
    prefix = usage_report.HEADER_PREFIX
    headers = usage_headers() | {
        f"{prefix}7d_fable-utilization": "0.25",
        f"{prefix}7d_fable-reset": str(RESET_7D),
        f"{prefix}status": "allowed",
    }
    api.state["response"] = fake_response(headers=headers)

    usage = usage_report.fetch_usage("oauth")

    assert set(usage) == {"5h", "7d", "7d_fable"}
    assert usage["7d_fable"] == (0.25, usage_report.parse_reset(str(RESET_7D)))


def test_format_message_lists_fable_after_main_windows() -> None:
    reset_7d = usage_report.parse_reset(str(RESET_7D))
    usage = {"7d_fable": (0.25, reset_7d), "5h": (0.32, None), "7d": (0.48, reset_7d)}

    message = usage_report.format_message(usage, NOW)

    assert message.endswith(
        "\n".join(
            [
                "<b>Limite semanal do Fable</b>",
                "█" * 5 + "░" * 15 + " 25% usado",
                "█" * 10 + "░" * 10 + " 48% do tempo",
                "Reinicia em 06/10 às 09:00",
            ]
        )
    )
    assert message.index("Janela de 5h") < message.index("Limite semanal<") < message.index("Fable")


def test_format_message_unknown_window_has_no_time_bar() -> None:
    message = usage_report.format_message({"overage": (0.1, NOW + timedelta(hours=1))}, NOW)

    assert "<b>Limite overage</b>" in message
    assert "do tempo" not in message
    assert "Reinicia em 02/10 às 18:00" in message


def test_main_logs_only_limit_names(monkeypatch, capsys) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    usage = {"7d_fable": (0.25, None), "5h": (0.32, None)}
    monkeypatch.setattr(usage_report, "fetch_usage", lambda token: usage)
    monkeypatch.setattr(usage_report, "send_telegram", lambda *args: None)

    usage_report.main()

    out = capsys.readouterr().out
    assert "Limites encontrados: 5h, 7d_fable" in out
    assert "%" not in out
