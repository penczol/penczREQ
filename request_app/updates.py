from __future__ import annotations

from .database import Database, utc_now
from .i18n import normalize_language
from .notification_i18n import encode_event_payload, render_event, request_event_payload


def notify_users_about_update(
    db: Database,
    version: str,
    public_body: str,
    admin_body: str,
    public_body_en: str | None = None,
    admin_body_en: str | None = None,
) -> int:
    inserted = 0
    with db.transaction() as conn:
        users = conn.execute(
            "SELECT id, role, language FROM users WHERE is_active = 1"
        ).fetchall()
        for user in users:
            exists = conn.execute(
                """
                SELECT 1 FROM notifications
                WHERE user_id = ? AND type = 'app_update' AND release_version = ?
                LIMIT 1
                """,
                (user["id"], version),
            ).fetchone()
            if exists:
                continue
            language = normalize_language(user["language"])
            payload = {
                "version": version,
                "role": user["role"],
                "body_pl": admin_body if user["role"] == "admin" else public_body,
            }
            selected_body_en = admin_body_en if user["role"] == "admin" else public_body_en
            if selected_body_en is not None:
                payload["body_en"] = selected_body_en
            title, body = render_event("app.update", payload, language) or ("", "")
            conn.execute(
                """
                INSERT INTO notifications
                (user_id, type, title, body, request_id, release_version,
                 event_key, event_payload_json, created_at)
                VALUES (?, 'app_update', ?, ?, NULL, ?, 'app.update', ?, ?)
                """,
                (user["id"], title, body, version, encode_event_payload(payload), utc_now()),
            )
            inserted += 1
    return inserted


def notify_upcoming_request(
    db: Database,
    user_id: int,
    title: str,
    season_number: int | None,
    original_title: str | None = None,
    english_title: str | None = None,
) -> bool:
    with db.transaction() as conn:
        user = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()
        language = normalize_language(user["language"] if user else "en")
        payload = request_event_payload(
            {
                "title_pl": title,
                "title_en": english_title or original_title or "",
                "title_original": original_title or "",
                "season_number": season_number,
            }
        )
        notification_title, body = render_event("request.upcoming", payload, language) or ("", "")
        preference = conn.execute(
            """
            SELECT enabled FROM notification_preferences
            WHERE user_id = ? AND type = 'request_changes'
            """,
            (user_id,),
        ).fetchone()
        if preference is not None and not bool(preference["enabled"]):
            return False
        duplicate = conn.execute(
            """
            SELECT 1 FROM notifications
            WHERE user_id = ? AND type = 'request_changes'
              AND event_key = 'request.upcoming' AND event_payload_json = ?
            LIMIT 1
            """,
            (user_id, encode_event_payload(payload)),
        ).fetchone()
        if duplicate:
            return False
        conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, event_key, event_payload_json, created_at)
            VALUES (?, 'request_changes', ?, ?, NULL, 'request.upcoming', ?, ?)
            """,
            (user_id, notification_title, body, encode_event_payload(payload), utc_now()),
        )
    return True
