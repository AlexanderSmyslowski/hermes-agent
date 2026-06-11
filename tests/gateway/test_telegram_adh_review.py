from __future__ import annotations

import sys
from types import SimpleNamespace
import uuid

import pytest

from gateway.platforms import adh_review


def _env(reviewer: str = "alice") -> dict[str, str]:
    return {"HERMES_ADH_REVIEWERS_JSON": f'{{"123456789":"{reviewer}"}}'}


def _draft(
    *,
    draft_id: str = "10000000-0000-4000-8000-000000000701",
    reviewer: str = "alice",
) -> dict[str, object]:
    return {
        "id": uuid.UUID(draft_id),
        "project": "central-agent-data-hub-demo",
        "type": "fact",
        "statement": "Small reviewed-memory draft for a local adapter test.",
        "source": "test",
        "status": "draft",
        "metadata": {"assigned_reviewer": reviewer},
        "responsible_reviewer": reviewer,
        "resolution_reason": "item metadata assigned_reviewer",
    }


@pytest.fixture(autouse=True)
def _fake_adh_review_api(monkeypatch):
    monkeypatch.setattr(adh_review, "_ADH_IMPORT_ERROR", None)
    monkeypatch.setattr(
        adh_review,
        "validate_reviewer_handle",
        lambda value: str(value).strip().lower(),
    )


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


def test_known_sender_maps_to_reviewer_and_unknown_sender_is_rejected() -> None:
    identity = adh_review.reviewer_for_sender(
        chat_id="chat-1",
        user_id="123456789",
        env=_env(),
    )

    assert identity.reviewer == "alice"
    assert identity.source_id == "123456789"

    with pytest.raises(adh_review.AdhReviewUnauthorized):
        adh_review.reviewer_for_sender(chat_id="chat-1", user_id="999", env=_env())


def test_missing_adh_review_api_reports_dependency_gap(monkeypatch) -> None:
    monkeypatch.setattr(adh_review, "_ADH_IMPORT_ERROR", ImportError("no review_api"))

    with pytest.raises(adh_review.AdhReviewUnavailable, match="agent_hub.review_api"):
        adh_review.fetch_cards_for_sender(
            chat_id=None,
            user_id="123456789",
            env=_env(),
            connect_fn=lambda: (_ for _ in ()).throw(AssertionError("no fallback")),
        )


def test_invalid_reviewer_is_rejected_before_connect(monkeypatch) -> None:
    def reject(_value):
        raise ValueError("reviewer handle is not allowed: charlie")

    monkeypatch.setattr(adh_review, "validate_reviewer_handle", reject)

    with pytest.raises(ValueError, match="not allowed"):
        adh_review.fetch_cards_for_sender(
            chat_id="chat-1",
            user_id="123456789",
            env=_env("charlie"),
            connect_fn=lambda: (_ for _ in ()).throw(AssertionError("no ADH read")),
        )


def test_fetch_cards_requests_assigned_drafts_and_filters_unassigned() -> None:
    calls = []

    def fake_fetch(_cur, *, for_reviewer=None, limit=None, project_slug=None):
        calls.append(
            {
                "for_reviewer": for_reviewer,
                "limit": limit,
                "project_slug": project_slug,
            }
        )
        return [_draft(reviewer="alice"), _draft(reviewer="unassigned")]

    cards = adh_review.fetch_cards_for_sender(
        chat_id=None,
        user_id="123456789",
        env=_env() | {"HERMES_ADH_REVIEW_MAX_CARDS": "5"},
        connect_fn=FakeConnection,
        fetch_drafts_fn=fake_fetch,
    )

    assert calls == [{"for_reviewer": "alice", "limit": 5, "project_slug": None}]
    assert len(cards) == 1
    assert "Zustaendig: alice" in cards[0].text
    assert "unassigned" not in cards[0].text


def test_empty_inbox_returns_no_cards() -> None:
    cards = adh_review.fetch_cards_for_sender(
        chat_id=None,
        user_id="123456789",
        env=_env(),
        connect_fn=FakeConnection,
        fetch_drafts_fn=lambda *_args, **_kwargs: [],
    )

    assert cards == []


def test_malformed_callback_does_not_connect() -> None:
    with pytest.raises(ValueError, match="invalid ADH review"):
        adh_review.review_callback_for_sender(
            chat_id=None,
            user_id="123456789",
            data="adhrev:nope",
            env=_env(),
            connect_fn=lambda: (_ for _ in ()).throw(AssertionError("no write")),
        )


@pytest.mark.parametrize(
    ("callback", "decision", "item_type"),
    [
        ("adhrev:a:f:10000000-0000-4000-8000-000000000701", "accept", "fact"),
        ("adhrev:r:q:10000000-0000-4000-8000-000000000701", "reject", "open_question"),
    ],
)
def test_accept_and_reject_call_adh_with_reviewer_and_telegram_source(
    callback: str,
    decision: str,
    item_type: str,
) -> None:
    conn = FakeConnection()
    calls = []

    def fake_review(_cur, draft_id, **kwargs):
        calls.append({"draft_id": draft_id, **kwargs})
        return {"id": draft_id, "status": "verified" if decision == "accept" else "archived"}

    result = adh_review.review_callback_for_sender(
        chat_id=None,
        user_id="123456789",
        data=callback,
        env=_env(),
        connect_fn=lambda: conn,
        review_draft_by_id_fn=fake_review,
    )

    assert result.status == "ok"
    assert conn.commits == 1
    assert calls == [
        {
            "draft_id": "10000000-0000-4000-8000-000000000701",
            "decision": decision,
            "item_type": item_type,
            "agent_slug": "telegram-review",
            "agent_name": "Telegram Review",
            "reviewed_by": "alice",
            "review_source": "telegram",
        }
    ]


def test_missing_or_already_reviewed_draft_does_not_commit() -> None:
    conn = FakeConnection()

    result = adh_review.review_callback_for_sender(
        chat_id=None,
        user_id="123456789",
        data="adhrev:a:f:10000000-0000-4000-8000-000000000701",
        env=_env(),
        connect_fn=lambda: conn,
        review_draft_by_id_fn=lambda *_args, **_kwargs: None,
    )

    assert result.status == "missing"
    assert result.message == "Diese Karte ist nicht mehr offen."
    assert conn.commits == 0


def test_card_text_is_short_clear_and_contains_no_chat_ids_or_secrets() -> None:
    card = adh_review.card_from_row(_draft())

    assert len(card.text) < 900
    assert "Projekt: central-agent-data-hub-demo" in card.text
    assert "Typ: Fakt" in card.text
    assert "Entwurf: 10000000-0000-4000-8000-000000000701" in card.text
    assert "123456789" not in card.text
    assert "token" not in card.text.lower()
    assert card.accept_callback == "adhrev:a:f:10000000-0000-4000-8000-000000000701"
    assert card.reject_callback == "adhrev:r:f:10000000-0000-4000-8000-000000000701"


def _install_telegram_mock(monkeypatch):
    telegram = SimpleNamespace()
    telegram.Update = object
    telegram.Bot = object
    telegram.Message = object
    telegram.LinkPreviewOptions = None

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    ext = SimpleNamespace(
        Application=object,
        CommandHandler=object,
        CallbackQueryHandler=object,
        MessageHandler=object,
        ContextTypes=SimpleNamespace(DEFAULT_TYPE=object),
        filters=SimpleNamespace(),
    )
    constants = SimpleNamespace(
        ParseMode=SimpleNamespace(MARKDOWN_V2="MarkdownV2", HTML="HTML"),
        ChatType=SimpleNamespace(PRIVATE="private"),
    )
    request = SimpleNamespace(HTTPXRequest=object)
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants)
    monkeypatch.setitem(sys.modules, "telegram.request", request)


def _patch_telegram_buttons(monkeypatch, telegram_mod):
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    monkeypatch.setattr(telegram_mod, "InlineKeyboardButton", InlineKeyboardButton)
    monkeypatch.setattr(telegram_mod, "InlineKeyboardMarkup", InlineKeyboardMarkup)


class FakeMessage:
    def __init__(self, *, chat_type: str | None = "private") -> None:
        self.chat = SimpleNamespace(id=111, type=chat_type)
        self.from_user = SimpleNamespace(id=123456789)
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})


@pytest.mark.asyncio
async def test_telegram_command_sends_empty_inbox_message(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    _patch_telegram_buttons(monkeypatch, telegram_mod)
    monkeypatch.setattr(telegram_mod, "fetch_cards_for_sender", lambda **_kwargs: [])
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    message = FakeMessage()

    await adapter._handle_adh_inbox_command(message)

    assert message.replies == [{"text": "Keine offenen Karten."}]


@pytest.mark.asyncio
async def test_telegram_command_rejects_group_before_fetch(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    def fail_fetch(**_kwargs):
        raise AssertionError("group /adh_inbox must not read ADH")

    monkeypatch.setattr(telegram_mod, "fetch_cards_for_sender", fail_fetch)
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    message = FakeMessage(chat_type="group")

    await adapter._handle_adh_inbox_command(message)

    assert message.replies == [
        {"text": "ADH Review ist nur im privaten Chat freigegeben."}
    ]


@pytest.mark.asyncio
async def test_telegram_command_rejects_missing_chat_type_before_fetch(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    def fail_fetch(**_kwargs):
        raise AssertionError("unknown chat type must not read ADH")

    monkeypatch.setattr(telegram_mod, "fetch_cards_for_sender", fail_fetch)
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    message = FakeMessage(chat_type=None)

    await adapter._handle_adh_inbox_command(message)

    assert message.replies == [
        {"text": "ADH Review ist nur im privaten Chat freigegeben."}
    ]


@pytest.mark.asyncio
async def test_telegram_command_sends_cards_with_review_buttons(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    _patch_telegram_buttons(monkeypatch, telegram_mod)
    card = adh_review.ReviewCard(
        draft_id="10000000-0000-4000-8000-000000000701",
        item_type="fact",
        text="Projekt: demo\nTyp: Fakt",
        accept_callback="adhrev:a:f:10000000-0000-4000-8000-000000000701",
        reject_callback="adhrev:r:f:10000000-0000-4000-8000-000000000701",
    )
    monkeypatch.setattr(telegram_mod, "fetch_cards_for_sender", lambda **_kwargs: [card])
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    message = FakeMessage()

    await adapter._handle_adh_inbox_command(message)

    assert message.replies[0]["text"] == card.text
    keyboard = message.replies[0]["reply_markup"].inline_keyboard
    assert keyboard[0][0].text == "Merken"
    assert keyboard[0][0].callback_data == card.accept_callback
    assert keyboard[0][1].text == "Verwerfen"
    assert keyboard[0][1].callback_data == card.reject_callback


class FakeQuery:
    def __init__(self, *, chat_type: str | None = "private") -> None:
        self.from_user = SimpleNamespace(id=123456789)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=111, type=chat_type))
        self.answers = []
        self.edits = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


@pytest.mark.asyncio
async def test_telegram_callback_resolves_and_disables_buttons(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    monkeypatch.setattr(
        telegram_mod,
        "review_callback_for_sender",
        lambda **_kwargs: adh_review.ReviewResult(status="ok", message="Gemerkt."),
    )
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    query = FakeQuery()

    await adapter._handle_adh_review_callback(
        query,
        "adhrev:a:f:10000000-0000-4000-8000-000000000701",
        query_chat_id=111,
    )

    assert query.answers == ["Gemerkt."]
    assert query.edits == [{"reply_markup": None}]


@pytest.mark.asyncio
async def test_telegram_callback_rejects_group_before_review(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    def fail_review(**_kwargs):
        raise AssertionError("group callback must not write ADH")

    monkeypatch.setattr(telegram_mod, "review_callback_for_sender", fail_review)
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    query = FakeQuery(chat_type="supergroup")

    await adapter._handle_adh_review_callback(
        query,
        "adhrev:a:f:10000000-0000-4000-8000-000000000701",
        query_chat_id=111,
    )

    assert query.answers == ["ADH Review ist nur im privaten Chat freigegeben."]
    assert query.edits == []


@pytest.mark.asyncio
async def test_telegram_callback_rejects_missing_chat_type_before_review(monkeypatch) -> None:
    _install_telegram_mock(monkeypatch)
    from gateway.config import PlatformConfig
    from gateway.platforms import telegram as telegram_mod

    def fail_review(**_kwargs):
        raise AssertionError("unknown chat type callback must not write ADH")

    monkeypatch.setattr(telegram_mod, "review_callback_for_sender", fail_review)
    adapter = telegram_mod.TelegramAdapter(PlatformConfig(enabled=True, token="test"))
    query = FakeQuery(chat_type=None)

    await adapter._handle_adh_review_callback(
        query,
        "adhrev:a:f:10000000-0000-4000-8000-000000000701",
        query_chat_id=111,
    )

    assert query.answers == ["ADH Review ist nur im privaten Chat freigegeben."]
    assert query.edits == []
