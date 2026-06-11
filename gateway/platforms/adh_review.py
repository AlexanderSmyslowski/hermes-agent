"""ADH draft review helpers for trusted gateway adapters.

The only supported Agent Data Hub imports here are from ``agent_hub.review_api``.
Telegram-specific network code stays in the Telegram adapter; this module keeps
the ADH review behavior stateless and easy to test without Telegram API calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

try:
    from agent_hub.review_api import (  # ty: ignore[unresolved-import]
        connect,
        fetch_drafts,
        review_draft_by_id,
        validate_reviewer_handle,
    )
except ImportError as exc:  # pragma: no cover - exercised through dependency checks
    connect = None
    fetch_drafts = None
    review_draft_by_id = None
    validate_reviewer_handle = None
    _ADH_IMPORT_ERROR: ImportError | None = exc
else:
    _ADH_IMPORT_ERROR = None


CALLBACK_PREFIX = "adhrev"
DEFAULT_MAX_CARDS = 5
MAX_CARD_TEXT_CHARS = 360
MAX_CARDS_LIMIT = 20

TYPE_TO_CODE = {
    "fact": "f",
    "decision": "d",
    "risk": "r",
    "open_question": "q",
    "report": "p",
}
CODE_TO_TYPE = {value: key for key, value in TYPE_TO_CODE.items()}
TYPE_LABELS = {
    "fact": "Fakt",
    "decision": "Entscheidung",
    "risk": "Risiko",
    "open_question": "Offene Frage",
    "report": "Bericht",
}


class AdhReviewUnavailable(RuntimeError):
    """Raised when the Agent Data Hub review facade is not importable."""


class AdhReviewUnauthorized(PermissionError):
    """Raised before any ADH read/write when a Telegram sender is not mapped."""


class AdhReviewConfigError(ValueError):
    """Raised for invalid local adapter configuration."""


@dataclass(frozen=True)
class ReviewIdentity:
    reviewer: str
    source_id: str


@dataclass(frozen=True)
class ReviewCard:
    draft_id: str
    item_type: str
    text: str
    accept_callback: str
    reject_callback: str


@dataclass(frozen=True)
class ReviewResult:
    status: str
    message: str
    result: dict[str, Any] | None = None


def ensure_adh_review_api_available() -> None:
    if _ADH_IMPORT_ERROR is not None:
        raise AdhReviewUnavailable(
            "Agent Data Hub review API is not importable. Install an ADH version "
            "that provides agent_hub.review_api before enabling Telegram review."
        ) from _ADH_IMPORT_ERROR


def reviewer_mapping_from_env(
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    raw = (env or os.environ).get("HERMES_ADH_REVIEWERS_JSON", "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdhReviewConfigError("HERMES_ADH_REVIEWERS_JSON must be a JSON object") from exc
    if not isinstance(loaded, dict):
        raise AdhReviewConfigError("HERMES_ADH_REVIEWERS_JSON must map ids to reviewer handles")
    return {str(key).strip(): str(value).strip() for key, value in loaded.items()}


def max_cards_from_env(env: dict[str, str] | None = None) -> int:
    raw = (env or os.environ).get("HERMES_ADH_REVIEW_MAX_CARDS", "").strip()
    if not raw:
        return DEFAULT_MAX_CARDS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MAX_CARDS
    return max(1, min(parsed, MAX_CARDS_LIMIT))


def reviewer_for_sender(
    *,
    chat_id: object | None,
    user_id: object | None,
    env: dict[str, str] | None = None,
) -> ReviewIdentity:
    ensure_adh_review_api_available()
    mapping = reviewer_mapping_from_env(env)
    candidates = [
        str(user_id).strip() if user_id not in (None, "") else "",
        str(chat_id).strip() if chat_id not in (None, "") else "",
    ]
    for source_id in candidates:
        if source_id and source_id in mapping:
            reviewer = validate_reviewer_handle(mapping[source_id])  # type: ignore[misc]
            return ReviewIdentity(reviewer=reviewer, source_id=source_id)
    raise AdhReviewUnauthorized("Dieser Telegram-Absender ist nicht fuer ADH Review freigegeben.")


def callback_data(*, decision: str, item_type: str, draft_id: object) -> str:
    action = {"accept": "a", "reject": "r"}[decision]
    type_code = TYPE_TO_CODE[item_type]
    return f"{CALLBACK_PREFIX}:{action}:{type_code}:{draft_id}"


def parse_callback_data(data: str) -> tuple[str, str, str]:
    parts = str(data or "").split(":", 3)
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        raise ValueError("invalid ADH review callback")
    action_code, type_code, draft_id = parts[1], parts[2], parts[3]
    if action_code not in {"a", "r"}:
        raise ValueError("invalid ADH review action")
    if type_code not in CODE_TO_TYPE:
        raise ValueError("invalid ADH review memory type")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", draft_id):
        raise ValueError("invalid ADH review draft id")
    decision = "accept" if action_code == "a" else "reject"
    return decision, CODE_TO_TYPE[type_code], draft_id


def _primary_text(row: dict[str, Any]) -> str:
    for key in (
        "statement",
        "decision",
        "title",
        "question",
        "summary",
        "body",
        "impact",
    ):
        value = row.get(key)
        if value:
            return " ".join(str(value).split())
    return "Kein Kartentext vorhanden."


def _shorten(text: str, limit: int = MAX_CARD_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def card_text(row: dict[str, Any]) -> str:
    item_type = str(row.get("type") or "")
    label = TYPE_LABELS.get(item_type, item_type or "Memory")
    draft_id = str(row.get("id") or "")
    responsible = str(row.get("responsible_reviewer") or "unassigned")
    reason = str(row.get("resolution_reason") or "no reviewer assigned")
    project = str(row.get("project") or row.get("project_name") or "unknown")
    text = _shorten(_primary_text(row))
    return "\n".join(
        [
            f"Projekt: {project}",
            f"Typ: {label}",
            f"Entwurf: {draft_id}",
            f"Zustaendig: {responsible}",
            f"Grund: {reason}",
            f"Text: {text}",
        ]
    )


def card_from_row(row: dict[str, Any]) -> ReviewCard:
    item_type = str(row["type"])
    draft_id = str(row["id"])
    return ReviewCard(
        draft_id=draft_id,
        item_type=item_type,
        text=card_text(row),
        accept_callback=callback_data(
            decision="accept",
            item_type=item_type,
            draft_id=draft_id,
        ),
        reject_callback=callback_data(
            decision="reject",
            item_type=item_type,
            draft_id=draft_id,
        ),
    )


def fetch_cards_for_sender(
    *,
    chat_id: object | None,
    user_id: object | None,
    env: dict[str, str] | None = None,
    connect_fn: Callable[[], Any] | None = None,
    fetch_drafts_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> list[ReviewCard]:
    identity = reviewer_for_sender(chat_id=chat_id, user_id=user_id, env=env)
    max_cards = max_cards_from_env(env)
    connect_callable = connect_fn or connect
    fetch_callable = fetch_drafts_fn or fetch_drafts
    if connect_callable is None or fetch_callable is None:
        ensure_adh_review_api_available()
        raise AdhReviewUnavailable("Agent Data Hub review API is not available.")

    with connect_callable() as conn:
        with conn.cursor() as cur:
            rows = fetch_callable(
                cur,
                for_reviewer=identity.reviewer,
                limit=max_cards,
            )
    rows = [
        row
        for row in rows
        if str(row.get("responsible_reviewer") or "") == identity.reviewer
    ]
    logger.info(
        "ADH Telegram review inbox fetched reviewer=%s count=%d",
        identity.reviewer,
        len(rows),
    )
    return [card_from_row(row) for row in rows[:max_cards]]


def review_callback_for_sender(
    *,
    chat_id: object | None,
    user_id: object | None,
    data: str,
    env: dict[str, str] | None = None,
    connect_fn: Callable[[], Any] | None = None,
    review_draft_by_id_fn: Callable[..., dict[str, Any] | None] | None = None,
) -> ReviewResult:
    identity = reviewer_for_sender(chat_id=chat_id, user_id=user_id, env=env)
    decision, item_type, draft_id = parse_callback_data(data)
    connect_callable = connect_fn or connect
    review_callable = review_draft_by_id_fn or review_draft_by_id
    if connect_callable is None or review_callable is None:
        ensure_adh_review_api_available()
        raise AdhReviewUnavailable("Agent Data Hub review API is not available.")

    with connect_callable() as conn:
        with conn.cursor() as cur:
            result = review_callable(
                cur,
                draft_id,
                decision=decision,
                item_type=item_type,
                agent_slug="telegram-review",
                agent_name="Telegram Review",
                reviewed_by=identity.reviewer,
                review_source="telegram",
            )
        if not result:
            return ReviewResult(
                status="missing",
                message="Diese Karte ist nicht mehr offen.",
                result=None,
            )
        commit = getattr(conn, "commit", None)
        if callable(commit):
            commit()

    logger.info(
        "ADH Telegram review resolved draft_id=%s action=%s reviewer=%s outcome=%s",
        draft_id,
        decision,
        identity.reviewer,
        result.get("status"),
    )
    label = "Gemerkt" if decision == "accept" else "Verworfen"
    return ReviewResult(
        status="ok",
        message=f"{label}.",
        result=result,
    )
