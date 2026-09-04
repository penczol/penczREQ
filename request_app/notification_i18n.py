from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from .changelog import update_notification_bodies
from .i18n import normalize_language, translate
from .titles import localized_title


# Privacy-preserving fingerprint of the one known pre-penczREQ push-test body.
# Keeping the old private brand literal out of the public source tree still lets
# the 0.4.3 migration recognize that exact historical notification.
LEGACY_PRIVATE_PUSH_TEST_SHA256 = (
    "2a2d24f112256969fc587f41f2a7d5ef31010d6d6c8094597dad36ca866532bc"  # pragma: allowlist secret
)


EVENT_TEMPLATES: dict[str, tuple[str, str]] = {
    "request.admin_new": (
        "Nowy request",
        "Użytkownik {username} dodał request „{label}”.",
    ),
    "request.own_liked": (
        "Nowe polubienie",
        "Ktoś polubił Twój request „{label}”.",
    ),
    "request.status_changed": (
        "Zmiana statusu",
        "Pozycja „{label}” ma teraz status: {status}.",
    ),
    "request.completed": (
        "Pozycja dostępna",
        "Pozycja „{label}” została zrealizowana i jest już dostępna.",
    ),
    "request.restored": (
        "Pozycja przywrócona",
        "Pozycja „{label}” wróciła do aktywnych requestów.",
    ),
    "request.deleted": (
        "Request usunięty",
        "Request „{label}” został usunięty. Powód: {reason}",
    ),
    "request.promoted": (
        "Pozycja po premierze",
        "Pozycja „{label}” miała premierę i została przeniesiona do aktywnych requestów.",
    ),
    "request.upcoming": (
        "Request przed premierą",
        "Pozycja „{label}” została sklasyfikowana jako przed premierą i umieszczona w karcie „Przed premierą”.",
    ),
    "system.push_test": (
        "Test powiadomień",
        "Powiadomienia systemowe penczREQ działają prawidłowo.",
    ),
}

_STATUS_SOURCES = (
    "Oczekujący",
    "W oczekiwaniu na premierę Blu-ray/VOD",
    "W trakcie realizacji",
    "Aktualnie brak źródła",
)
_STATUS_SOURCE_BY_LITERAL = {
    literal: source
    for source in _STATUS_SOURCES
    for literal in (source, translate(source, "en"))
}


def encode_event_payload(payload: Mapping[str, Any] | None = None) -> str:
    return json.dumps(
        dict(payload or {}),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_event_payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def request_event_payload(
    source: Mapping[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "title_pl": source.get("title_pl") or source.get("title_original") or "",
        "title_en": source.get("title_en") or source.get("title_original") or "",
        "title_original": source.get("title_original") or "",
        "season_number": source.get("season_number"),
    }
    payload.update(extra)
    return payload


def _localized_label(payload: Mapping[str, Any], language: str) -> str:
    normalized = normalize_language(language)
    literal = str(payload.get("label") or "").strip()
    title = localized_title(payload, normalized)
    if title == "—" and literal:
        title = literal
    season_number = payload.get("season_number")
    if season_number is None or season_number == "":
        return title
    season = "sezon" if normalized == "pl" else "season"
    return f"{title} — {season} {season_number}"


def render_event(
    event_key: str,
    payload: Mapping[str, Any] | None,
    language: str,
) -> tuple[str, str] | None:
    normalized = normalize_language(language)
    values = dict(payload or {})
    if event_key == "app.update":
        version = str(values.get("version") or "").strip()
        if not version:
            return None
        body = str(values.get(f"body_{normalized}") or "")
        if not body:
            public_body, admin_body = update_notification_bodies(version, normalized)
            body = admin_body if values.get("role") == "admin" else public_body
        return translate("Aktualizacja {version}", normalized, version=version), body
    if event_key == "admin.broadcast.bilingual":
        title = str(values.get(f"title_{normalized}") or "")
        body = str(values.get(f"body_{normalized}") or "")
        return (title, body) if title and body else None
    templates = EVENT_TEMPLATES.get(event_key)
    if templates is None:
        return None
    if "title_pl" in values or "title_en" in values or "title_original" in values or "label" in values:
        values["label"] = _localized_label(values, normalized)
    if "status_source" in values:
        values["status"] = translate(str(values["status_source"]), normalized)
    title_source, body_source = templates
    try:
        return (
            translate(title_source, normalized, **values),
            translate(body_source, normalized, **values),
        )
    except (KeyError, ValueError):
        return None


def localized_notification(row: Mapping[str, Any], language: str) -> dict[str, Any]:
    item = dict(row)
    rendered = render_event(
        str(item.get("event_key") or ""),
        decode_event_payload(item.get("event_payload_json")),
        language,
    )
    if rendered is not None:
        item["title"], item["body"] = rendered
    item.pop("user_id", None)
    item.pop("event_key", None)
    item.pop("event_payload_json", None)
    return item


def _split_legacy_label(value: str, language: str) -> dict[str, Any]:
    suffix = re.match(r"^(?P<label>.*) — (?:sezon|season) (?P<season>\d+)$", value)
    if suffix:
        return {
            "label": suffix.group("label"),
            "season_number": int(suffix.group("season")),
        }
    return {"label": value}


def _request_payload(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    legacy_label: str,
    language: str,
) -> dict[str, Any]:
    request_id = row.get("request_id")
    if request_id is not None:
        request_row = conn.execute(
            "SELECT title_pl, title_en, title_original, season_number FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()
        if request_row is not None:
            return request_event_payload(dict(request_row))
    return _split_legacy_label(legacy_label, language)


def _match_body(body: str, patterns: tuple[tuple[str, str], ...]):
    for language, pattern in patterns:
        matched = re.fullmatch(pattern, body, flags=re.DOTALL)
        if matched:
            return language, matched.groupdict()
    return None


def classify_legacy_notification(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    type_name = str(row.get("type") or "")
    title = str(row.get("title") or "")
    body = str(row.get("body") or "")

    if type_name == "app_update":
        version = str(row.get("release_version") or "").strip()
        if not version:
            title_match = re.fullmatch(r"(?:Aktualizacja|Update) (.+)", title)
            version = title_match.group(1) if title_match else ""
        if version:
            return "app.update", {"version": version, "role": row.get("user_role") or "user"}

    if type_name == "system" and title in {"Test powiadomień", "Notification test"}:
        known_bodies = {
            "Powiadomienia systemowe penczREQ działają prawidłowo.",
            "penczREQ system notifications are working correctly.",
        }
        legacy_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if body in known_bodies or legacy_digest == LEGACY_PRIVATE_PUSH_TEST_SHA256:
            return "system.push_test", {}

    definitions = (
        (
            "admin_new_request",
            {"Nowy request", "New request"},
            "request.admin_new",
            (
                ("pl", r"Użytkownik (?P<username>.+?) dodał request „(?P<label>.*)”\."),
                ("en", r"User (?P<username>.+?) added the request “(?P<label>.*)”\."),
            ),
        ),
        (
            "own_request_liked",
            {"Nowe polubienie", "New like"},
            "request.own_liked",
            (
                ("pl", r"Ktoś polubił Twój request „(?P<label>.*)”\."),
                ("en", r"Someone liked your request “(?P<label>.*)”\."),
            ),
        ),
        (
            "request_changes",
            {"Zmiana statusu", "Status change"},
            "request.status_changed",
            (
                ("pl", r"Pozycja „(?P<label>.*)” ma teraz status: (?P<status>.*)\."),
                ("en", r"The title “(?P<label>.*)” now has status: (?P<status>.*)\."),
            ),
        ),
        (
            "request_changes",
            {"Pozycja dostępna", "Title available"},
            "request.completed",
            (
                ("pl", r"Pozycja „(?P<label>.*)” została zrealizowana i jest już dostępna\."),
                ("en", r"The title “(?P<label>.*)” was completed and is now available\."),
            ),
        ),
        (
            "request_changes",
            {"Pozycja przywrócona", "Title restored"},
            "request.restored",
            (
                ("pl", r"Pozycja „(?P<label>.*)” wróciła do aktywnych requestów\."),
                ("en", r"The title “(?P<label>.*)” returned to active requests\."),
            ),
        ),
        (
            "request_changes",
            {"Request usunięty", "Request deleted"},
            "request.deleted",
            (
                ("pl", r"Request „(?P<label>.*)” został usunięty\. Powód: (?P<reason>.*)"),
                ("en", r"The request “(?P<label>.*)” was deleted\. Reason: (?P<reason>.*)"),
            ),
        ),
        (
            "request_changes",
            {"Pozycja po premierze", "Title released"},
            "request.promoted",
            (
                ("pl", r"Pozycja „(?P<label>.*)” miała premierę i została przeniesiona do aktywnych requestów\."),
                ("en", r"The title “(?P<label>.*)” was released and moved to active requests\."),
            ),
        ),
        (
            "request_changes",
            {"Request przed premierą", "Upcoming request"},
            "request.upcoming",
            (
                ("pl", r"Pozycja „(?P<label>.*)” została sklasyfikowana jako przed premierą i umieszczona w karcie „Przed premierą”\."),
                ("en", r"The title “(?P<label>.*)” was classified as upcoming and placed on the Upcoming tab\."),
            ),
        ),
    )
    for expected_type, titles, event_key, patterns in definitions:
        if type_name != expected_type or title not in titles:
            continue
        matched = _match_body(body, patterns)
        if matched is None:
            return None
        language, values = matched
        label = values.pop("label", "")
        payload = _request_payload(conn, row, label, language)
        payload.update(values)
        if "status" in payload:
            status_source = _STATUS_SOURCE_BY_LITERAL.get(str(payload.pop("status")))
            if status_source is None:
                return None
            payload["status_source"] = status_source
        return event_key, payload
    return None


def migrate_legacy_notifications(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT n.*, u.role AS user_role
        FROM notifications AS n
        JOIN users AS u ON u.id = n.user_id
        WHERE n.event_key IS NULL
        ORDER BY n.id
        """
    ).fetchall()
    migrated = 0
    for raw_row in rows:
        row = dict(raw_row)
        classified = classify_legacy_notification(conn, row)
        if classified is None:
            continue
        event_key, payload = classified
        release_version = None
        if event_key == "app.update" and not row.get("release_version"):
            candidate = str(payload.get("version") or "").strip()
            collision = conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE user_id = ? AND release_version = ? AND id <> ?
                LIMIT 1
                """,
                (row["user_id"], candidate, row["id"]),
            ).fetchone()
            if candidate and collision is None:
                release_version = candidate
        cursor = conn.execute(
            """
            UPDATE notifications
            SET event_key = ?, event_payload_json = ?,
                release_version = COALESCE(release_version, ?)
            WHERE id = ? AND event_key IS NULL
            """,
            (event_key, encode_event_payload(payload), release_version, row["id"]),
        )
        migrated += int(cursor.rowcount)
    return migrated
