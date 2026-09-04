from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .database import Database, utc_now
from .i18n import normalize_language
from .notification_i18n import (
    decode_event_payload,
    encode_event_payload,
    localized_notification,
    render_event,
    request_event_payload,
)
from .security import (
    dummy_verify_password,
    hash_password,
    normalize_username,
    validate_password,
    validate_username,
    verify_password,
)
from .tmdb import MediaDetails, ReleaseMetadata


PREFERENCE_TYPES = (
    "own_request_liked",
    "request_changes",
    "admin_new_request",
    "admin_messages",
)

STATUS_LABELS = {
    "pending": "Oczekujący",
    "translation": "W oczekiwaniu na premierę Blu-ray/VOD",
    "in_progress": "W trakcie realizacji",
    "missing": "Aktualnie brak źródła",
}

LIKE_UNDO_SECONDS = 10


class RepositoryError(RuntimeError):
    pass


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def require_admin_exists(self) -> None:
        """Fail closed when the public service starts before Control bootstraps the database."""
        with self.db.connect() as conn:
            existing_admin = conn.execute(
                "SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1 LIMIT 1"
            ).fetchone()
        if not existing_admin:
            raise RepositoryError(
                "Brak aktywnego administratora publicznego. "
                "Uruchom najpierw penczREQ Control i dokończ inicjalizację."
            )

    def enforce_roles(self, preferred_admin: str = "admin", bootstrap_password: str = "") -> None:
        """Preserve database roles and create/promote an initial admin only when none exists."""
        with self.db.transaction() as conn:
            existing_admin = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
            if existing_admin:
                return
            preferred = normalize_username(preferred_admin)
            candidate = conn.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (preferred,)
            ).fetchone()
            if candidate:
                conn.execute("UPDATE users SET role = 'admin', is_active = 1 WHERE id = ?", (candidate["id"],))
                return
            if error := validate_username(preferred):
                raise RepositoryError(error)
            if error := validate_password(bootstrap_password, username=preferred):
                raise RepositoryError(
                    "Brak administratora publicznego. Ustaw prawidłowe PUBLIC_ADMIN_USERNAME "
                    "i PUBLIC_ADMIN_BOOTSTRAP_PASSWORD. " + error
                )
            now = utc_now()
            conn.execute(
                """
                INSERT INTO users
                (username, password_hash, role, is_active, must_change_password, created_at, password_changed_at)
                VALUES (?, ?, 'admin', 1, 1, ?, ?)
                """,
                (preferred, hash_password(bootstrap_password), now, now),
            )

    def require_password_policy_upgrade(self, policy_version: str = "v2") -> int:
        marker = f"password_policy_{policy_version}_enforced"
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (marker,)).fetchone():
                return 0
            count = int(conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_active = 1 AND must_change_password = 0"
            ).fetchone()[0])
            conn.execute("UPDATE users SET must_change_password = 1 WHERE is_active = 1")
            conn.execute("DELETE FROM sessions")
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, '1', ?)",
                (marker, utc_now()),
            )
        return count

    def user_by_username(self, username: str):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (normalize_username(username),)).fetchone()

    def user_by_id(self, user_id: int):
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def authenticate(self, username: str, password: str, ip_address: str) -> Any | None:
        user = self.user_by_username(username)
        if not user or not user["is_active"]:
            dummy_verify_password(password)
            return None
        if not verify_password(user["password_hash"], password):
            return None
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE users
                SET last_login_at = ?, last_login_ip = ?, failed_login_count = 0,
                    temporary_lock_until = NULL, lockout_cycles = 0,
                    security_locked = 0, security_locked_at = NULL
                WHERE id = ?
                """,
                (utc_now(), ip_address, user["id"]),
            )
        return self.user_by_id(user["id"])

    def create_user(self, username: str, temporary_password: str):
        username = normalize_username(username)
        if error := validate_username(username):
            raise RepositoryError(error)
        if error := validate_password(temporary_password, username=username):
            raise RepositoryError(error)
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, role, is_active, must_change_password, created_at) VALUES (?, ?, 'user', 1, 1, ?)",
                    (username, hash_password(temporary_password), utc_now()),
                )
                user_id = int(cursor.lastrowid)
                self._ensure_preferences(conn, user_id)
            return self.user_by_id(user_id)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError("Użytkownik o takim loginie już istnieje.") from exc

    def list_users(self):
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT id, username, role, is_active, must_change_password, language, created_at,
                       last_login_at, last_login_ip
                FROM users ORDER BY role DESC, username
                """
            ).fetchall()

    def set_user_language(self, user_id: int, language: str) -> None:
        normalized = normalize_language(language)
        if language not in {"en", "pl"}:
            raise RepositoryError("Nieprawidłowy język interfejsu.")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE users SET language = ? WHERE id = ?",
                (normalized, user_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("Nie znaleziono użytkownika.")

    def set_user_active(self, user_id: int, active: bool) -> None:
        user = self.user_by_id(user_id)
        if not user:
            raise RepositoryError("Nie znaleziono użytkownika.")
        with self.db.transaction() as conn:
            if user["role"] == "admin" and not active:
                other_admin = conn.execute(
                    "SELECT 1 FROM users WHERE role = 'admin' AND is_active = 1 AND id <> ? LIMIT 1",
                    (user_id,),
                ).fetchone()
                if not other_admin:
                    raise RepositoryError("Nie można zablokować ostatniego aktywnego administratora.")
            conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user_id))
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))

    def rename_user(self, user_id: int, new_username: str) -> None:
        username = normalize_username(new_username)
        if error := validate_username(username):
            raise RepositoryError(error)
        if not self.user_by_id(user_id):
            raise RepositoryError("Nie znaleziono użytkownika.")
        try:
            with self.db.transaction() as conn:
                conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
        except sqlite3.IntegrityError as exc:
            raise RepositoryError("Użytkownik o takim loginie już istnieje.") from exc

    def transfer_admin_role(self, user_id: int) -> None:
        user = self.user_by_id(user_id)
        if not user:
            raise RepositoryError("Nie znaleziono użytkownika.")
        if not user["is_active"]:
            raise RepositoryError("Najpierw odblokuj wybrane konto.")
        with self.db.transaction() as conn:
            conn.execute("UPDATE users SET role = 'user' WHERE role = 'admin' AND id <> ?", (user_id,))
            conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
            conn.execute("DELETE FROM sessions")

    def revoke_user_sessions(self, user_id: int) -> int:
        if not self.user_by_id(user_id):
            raise RepositoryError("Nie znaleziono użytkownika.")
        with self.db.transaction() as conn:
            session_cursor = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,))
        return int(session_cursor.rowcount)

    def delete_user(self, user_id: int) -> dict[str, Any]:
        """Atomically remove a non-admin account and its active participation."""
        with self.db.transaction() as conn:
            user = conn.execute(
                "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not user:
                raise RepositoryError("Nie znaleziono użytkownika.")
            if user["role"] == "admin":
                raise RepositoryError(
                    "Najpierw przekaż rolę administratora innemu aktywnemu kontu."
                )

            participations = conn.execute(
                """
                SELECT own.request_id,
                       (SELECT COUNT(*)
                        FROM likes AS other
                        WHERE other.request_id = own.request_id
                          AND other.user_id <> own.user_id) AS other_participants
                FROM likes AS own
                WHERE own.user_id = ?
                ORDER BY own.request_id
                """,
                (user_id,),
            ).fetchall()
            requests_to_delete = [
                int(row["request_id"])
                for row in participations
                if int(row["other_participants"]) == 0
            ]

            counts = {
                "sessions_revoked": int(conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
                ).fetchone()[0]),
                "push_subscriptions_removed": int(conn.execute(
                    "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user_id,)
                ).fetchone()[0]),
                "notifications_removed": int(conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE user_id = ?", (user_id,)
                ).fetchone()[0]),
                "participations_removed": len(participations) - len(requests_to_delete),
                "requests_deleted": len(requests_to_delete),
            }

            for request_id in requests_to_delete:
                conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))

            counts["requested_by_anonymized"] = int(conn.execute(
                "SELECT COUNT(*) FROM requests WHERE requested_by = ?", (user_id,)
            ).fetchone()[0])
            counts["completed_by_anonymized"] = int(conn.execute(
                "SELECT COUNT(*) FROM requests WHERE completed_by = ?", (user_id,)
            ).fetchone()[0])

            deleted = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            if deleted.rowcount != 1:
                raise RepositoryError("Nie udało się trwale usunąć użytkownika.")

            return {
                "target_user_id": int(user["id"]),
                "target_username": str(user["username"]),
                **counts,
            }

    def set_password(self, user_id: int, new_password: str, *, current_password: str | None = None, force_change: bool = False) -> None:
        user = self.user_by_id(user_id)
        if not user:
            raise RepositoryError("Nie znaleziono użytkownika.")
        if current_password is not None and not verify_password(user["password_hash"], current_password):
            raise RepositoryError("Obecne hasło jest nieprawidłowe.")
        if error := validate_password(new_password, username=user["username"], current=current_password):
            raise RepositoryError(error)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ?, password_changed_at = ? WHERE id = ?",
                (hash_password(new_password), int(force_change), utc_now(), user_id),
            )

    def _ensure_preferences(self, conn: sqlite3.Connection, user_id: int) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO notification_preferences (user_id, type, enabled) VALUES (?, ?, 1)",
            [(user_id, type_name) for type_name in PREFERENCE_TYPES],
        )

    def preferences(self, user_id: int) -> dict[str, bool]:
        with self.db.transaction() as conn:
            self._ensure_preferences(conn, user_id)
            rows = conn.execute("SELECT type, enabled FROM notification_preferences WHERE user_id = ?", (user_id,)).fetchall()
        return {row["type"]: bool(row["enabled"]) for row in rows if row["type"] in PREFERENCE_TYPES}

    def update_preferences(self, user_id: int, values: dict[str, bool]) -> None:
        with self.db.transaction() as conn:
            self._ensure_preferences(conn, user_id)
            for type_name in PREFERENCE_TYPES:
                if type_name in values:
                    conn.execute(
                        "UPDATE notification_preferences SET enabled = ? WHERE user_id = ? AND type = ?",
                        (int(values[type_name]), user_id, type_name),
                    )

    @staticmethod
    def _preference_enabled(conn: sqlite3.Connection, user_id: int, type_name: str) -> bool:
        row = conn.execute(
            "SELECT enabled FROM notification_preferences WHERE user_id = ? AND type = ?",
            (user_id, type_name),
        ).fetchone()
        return True if row is None else bool(row["enabled"])

    def _notify(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        type_name: str,
        title: str,
        body: str,
        request_id: int | None = None,
        *,
        event_key: str | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        if not self._preference_enabled(conn, user_id, type_name):
            return
        conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, event_key, event_payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                type_name,
                title,
                body,
                request_id,
                event_key,
                encode_event_payload(event_payload) if event_key else None,
                utc_now(),
            ),
        )

    @staticmethod
    def _user_language(conn: sqlite3.Connection, user_id: int) -> str:
        row = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()
        return normalize_language(row["language"] if row else "en")

    def _notify_event(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        type_name: str,
        event_key: str,
        *,
        request_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        language = self._user_language(conn, user_id)
        rendered = render_event(event_key, payload, language)
        if rendered is None:
            raise RuntimeError(f"Unknown system notification event: {event_key}")
        self._notify(
            conn,
            user_id,
            type_name,
            rendered[0],
            rendered[1],
            request_id,
            event_key=event_key,
            event_payload=payload,
        )

    @staticmethod
    def _interested_users(conn: sqlite3.Connection, request_id: int) -> list[int]:
        rows = conn.execute(
            """
            SELECT interested.user_id
            FROM (
                SELECT requested_by AS user_id FROM requests
                WHERE id = ? AND requested_by IS NOT NULL
                UNION
                SELECT user_id FROM likes WHERE request_id = ?
            ) AS interested
            WHERE NOT EXISTS (
                SELECT 1 FROM request_withdrawals w
                WHERE w.request_id = ? AND w.user_id = interested.user_id
            )
            """,
            (request_id, request_id, request_id),
        ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def _notify_request_change(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        event_key: str,
        *,
        row: Any,
        keep_link: bool = True,
        values: dict[str, Any] | None = None,
    ) -> None:
        payload = request_event_payload(dict(row), **(values or {}))
        for user_id in self._interested_users(conn, request_id):
            self._notify_event(
                conn,
                user_id,
                "request_changes",
                event_key,
                request_id=request_id if keep_link else None,
                payload=payload,
            )

    def create_request(self, media: MediaDetails, poster_filename: str | None, user_id: int) -> tuple[int, bool, str]:
        today = date.today().isoformat()
        state = "active" if media.release_date and media.release_date <= today else "upcoming"
        with self.db.transaction() as conn:
            if media.media_type == "tv":
                conn.execute(
                    """
                    UPDATE requests
                    SET title_pl = ?, title_en = ?, title_original = ?,
                        original_language = COALESCE(?, original_language)
                    WHERE media_type = 'tv' AND tmdb_id = ?
                    """,
                    (
                        media.title_pl,
                        media.title_en,
                        media.title_original,
                        media.original_language,
                        media.tmdb_id,
                    ),
                )
            existing = conn.execute(
                "SELECT id, state FROM requests WHERE media_type = ? AND tmdb_id = ? AND IFNULL(season_number, -1) = IFNULL(?, -1)",
                (media.media_type, media.tmdb_id, media.season_number),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE requests
                    SET title_pl = ?, title_en = ?, title_original = ?,
                        original_language = COALESCE(?, original_language)
                    WHERE id = ?
                    """,
                    (
                        media.title_pl,
                        media.title_en,
                        media.title_original,
                        media.original_language,
                        existing["id"],
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO likes (request_id, user_id, created_at) VALUES (?, ?, ?)",
                    (existing["id"], user_id, utc_now()),
                )
                return int(existing["id"]), True, str(existing["state"])

            cursor = conn.execute(
                """
                INSERT INTO requests
                (tmdb_id, media_type, season_number, imdb_id, title_pl, title_en, title_original,
                 release_year, series_start_year, series_end_year, series_status, release_date,
                 world_theatrical_date, world_digital_date, world_physical_date, pl_theatrical_date,
                 pl_digital_date, pl_physical_date, release_data_refreshed_at, original_language,
                 poster_path, state, status, requested_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    media.tmdb_id, media.media_type, media.season_number, media.imdb_id,
                    media.title_pl, media.title_en, media.title_original, media.release_year,
                    media.series_start_year, media.series_end_year, media.series_status, media.release_date,
                    media.world_theatrical_date, media.world_digital_date, media.world_physical_date,
                    media.pl_theatrical_date, media.pl_digital_date, media.pl_physical_date,
                    utc_now(), media.original_language, poster_filename, state, user_id, utc_now(),
                ),
            )
            request_id = int(cursor.lastrowid)
            conn.execute("INSERT INTO likes (request_id, user_id, created_at) VALUES (?, ?, ?)", (request_id, user_id, utc_now()))
            admins = conn.execute("SELECT id FROM users WHERE role = 'admin' AND is_active = 1 AND id <> ?", (user_id,)).fetchall()
            requester = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            for admin in admins:
                self._notify_event(
                    conn,
                    int(admin["id"]),
                    "admin_new_request",
                    "request.admin_new",
                    request_id=request_id,
                    payload=request_event_payload(
                        {
                            "title_pl": media.title_pl,
                            "title_en": media.title_en,
                            "title_original": media.title_original,
                            "season_number": media.season_number,
                        },
                        username=requester["username"],
                    ),
                )
            return request_id, False, state

    def list_requests(self, state: str, user_id: int, is_admin: bool) -> list[dict[str, Any]]:
        if state not in {"active", "upcoming", "completed"}:
            raise RepositoryError("Nieprawidłowy widok listy.")
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, u.username AS requester_username,
                       COUNT(DISTINCT l.user_id) AS like_count,
                       MAX(CASE WHEN l.user_id = ? THEN 1 ELSE 0 END) AS liked_by_me,
                       MAX(CASE WHEN l.user_id = ? THEN l.created_at END) AS my_like_created_at
                FROM requests r
                LEFT JOIN users u ON u.id = r.requested_by
                LEFT JOIN likes l ON l.request_id = r.id
                WHERE r.state = ?
                GROUP BY r.id
                ORDER BY r.created_at DESC, r.id DESC
                """,
                (user_id, user_id, state),
            ).fetchall()
            now = datetime.now(UTC)
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["like_count"] = int(item["like_count"] or 0)
                item["liked_by_me"] = bool(item["liked_by_me"])
                item["status_label"] = STATUS_LABELS[item["status"]]
                item["author_like"] = bool(item["liked_by_me"] and item["requested_by"] == user_id)
                item["can_withdraw"] = bool(
                    item["requested_by"] == user_id and item["liked_by_me"]
                )
                deadline = None
                if item["liked_by_me"] and item["my_like_created_at"] and not item["author_like"]:
                    deadline_dt = _parse_datetime(item["my_like_created_at"]) + timedelta(seconds=LIKE_UNDO_SECONDS)
                    deadline = deadline_dt.isoformat()
                    item["can_unlike"] = deadline_dt >= now
                else:
                    item["can_unlike"] = False
                item["like_removal_deadline"] = deadline
                if is_admin:
                    item["likers"] = [
                        liker["username"]
                        for liker in conn.execute(
                            "SELECT u.username FROM likes l JOIN users u ON u.id = l.user_id WHERE l.request_id = ? ORDER BY u.username",
                            (row["id"],),
                        ).fetchall()
                    ]
                else:
                    for private_field in ("requester_username", "requested_by", "completed_by"):
                        item.pop(private_field, None)
                result.append(item)
            return result

    def paginated_requests(
        self,
        state: str,
        user_id: int,
        is_admin: bool,
        *,
        page: int = 1,
        page_size: int = 25,
        sort: str = "newest",
        status_filter: str = "all",
    ) -> dict[str, Any]:
        if page_size not in {25, 50, 100}:
            raise RepositoryError("Dozwolony limit strony to 25, 50 albo 100.")
        if sort not in {"newest", "oldest", "likes_desc", "likes_asc", "status"}:
            raise RepositoryError("Nieprawidłowe sortowanie.")
        if status_filter not in {"all", "pending", "translation", "in_progress", "missing"}:
            raise RepositoryError("Nieprawidłowy filtr statusu.")
        items = self.list_requests(state, user_id, is_admin)

        def group_key(item: dict[str, Any]) -> tuple[str, int]:
            if item["media_type"] == "tv":
                return ("tv", int(item["tmdb_id"]))
            return ("request", int(item["id"]))

        total_all_groups = len(set(group_key(item) for item in items))
        if state == "active" and status_filter != "all":
            items = [item for item in items if item["status"] == status_filter]
        items.sort(key=lambda item: int(item["id"]), reverse=True)
        items.sort(key=lambda item: str(item["created_at"]), reverse=True)
        if sort == "oldest":
            items.sort(key=lambda item: (str(item["created_at"]), int(item["id"])))
        elif sort == "likes_desc":
            items.sort(key=lambda item: int(item["like_count"]), reverse=True)
        elif sort == "likes_asc":
            items.sort(key=lambda item: int(item["like_count"]))
        elif sort == "status":
            order = {"in_progress": 0, "pending": 1, "translation": 2, "missing": 3}
            items.sort(key=lambda item: order.get(str(item["status"]), 9))

        ordered_groups = list(dict.fromkeys(group_key(item) for item in items))
        total_groups = len(ordered_groups)
        total_pages = max(1, (total_groups + page_size - 1) // page_size)
        current_page = min(max(int(page), 1), total_pages)
        start = (current_page - 1) * page_size
        selected_groups = set(ordered_groups[start:start + page_size])
        selected_items = [item for item in items if group_key(item) in selected_groups]
        return {
            "items": selected_items,
            "pagination": {
                "page": current_page,
                "page_size": page_size,
                "total_items": total_groups,
                "total_all_items": total_all_groups,
                "total_pages": total_pages,
            },
        }

    def toggle_like(self, request_id: int, user_id: int) -> dict[str, Any]:
        with self.db.transaction() as conn:
            request_row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
            if not request_row:
                raise RepositoryError("Request nie istnieje.")
            existing = conn.execute("SELECT created_at FROM likes WHERE request_id = ? AND user_id = ?", (request_id, user_id)).fetchone()
            if existing:
                if request_row["requested_by"] == user_id:
                    raise RepositoryError("Automatycznego lajka autora requestu nie można wycofać.")
                created = _parse_datetime(existing["created_at"])
                if not created or datetime.now(UTC) > created + timedelta(seconds=LIKE_UNDO_SECONDS):
                    raise RepositoryError("Lajk można wycofać tylko przez 10 sekund od jego dodania.")
                conn.execute("DELETE FROM likes WHERE request_id = ? AND user_id = ?", (request_id, user_id))
                liked = False
                deadline = None
            else:
                withdrawn = conn.execute(
                    "SELECT 1 FROM request_withdrawals WHERE request_id = ? AND user_id = ?",
                    (request_id, user_id),
                ).fetchone()
                if withdrawn:
                    raise RepositoryError("Wycofanego udziału autora nie można dodać ponownie.")
                created_at = utc_now()
                conn.execute("INSERT INTO likes (request_id, user_id, created_at) VALUES (?, ?, ?)", (request_id, user_id, created_at))
                liked = True
                deadline = (_parse_datetime(created_at) + timedelta(seconds=LIKE_UNDO_SECONDS)).isoformat()
                author_id = request_row["requested_by"]
                author_withdrew = bool(author_id and conn.execute(
                    "SELECT 1 FROM request_withdrawals WHERE request_id = ? AND user_id = ?",
                    (request_id, author_id),
                ).fetchone())
                if author_id and author_id != user_id and not author_withdrew:
                    notified = conn.execute(
                        "SELECT 1 FROM like_notification_history WHERE request_id = ? AND liker_user_id = ?",
                        (request_id, user_id),
                    ).fetchone()
                    if not notified:
                        self._notify_event(
                            conn,
                            int(author_id),
                            "own_request_liked",
                            "request.own_liked",
                            request_id=request_id,
                            payload=request_event_payload(dict(request_row)),
                        )
                        conn.execute(
                            "INSERT INTO like_notification_history (request_id, liker_user_id, created_at) VALUES (?, ?, ?)",
                            (request_id, user_id, utc_now()),
                        )
            count = conn.execute("SELECT COUNT(*) AS value FROM likes WHERE request_id = ?", (request_id,)).fetchone()["value"]
            return {"liked": liked, "count": int(count), "can_unlike": liked, "like_removal_deadline": deadline}

    def withdraw_request(self, request_id: int, user_id: int) -> str:
        """Atomically remove an author's participation without revealing identity."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id, requested_by FROM requests WHERE id = ? AND state IN ('active', 'upcoming')",
                (request_id,),
            ).fetchone()
            if not row:
                raise RepositoryError("Nie znaleziono requestu możliwego do wycofania.")
            if int(row["requested_by"] or 0) != user_id:
                raise RepositoryError("Możesz wycofać wyłącznie własny request.")
            own_like = conn.execute(
                "SELECT 1 FROM likes WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            ).fetchone()
            if not own_like:
                raise RepositoryError("Ten request został już wycofany.")
            others = int(conn.execute(
                "SELECT COUNT(*) FROM likes WHERE request_id = ? AND user_id <> ?",
                (request_id, user_id),
            ).fetchone()[0])
            if others == 0:
                conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))
                return "deleted"
            conn.execute(
                "DELETE FROM likes WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO request_withdrawals (request_id, user_id, withdrawn_at) VALUES (?, ?, ?)",
                (request_id, user_id, utc_now()),
            )
            return "participation_removed"

    def set_status(self, request_id: int, status: str) -> None:
        if status not in STATUS_LABELS:
            raise RepositoryError("Nieprawidłowy status.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ? AND state = 'active'", (request_id,)).fetchone()
            if not row:
                raise RepositoryError("Nie znaleziono aktywnego requestu.")
            if row["status"] == status:
                return
            conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
            self._notify_request_change(
                conn,
                request_id,
                "request.status_changed",
                row=row,
                values={"status_source": STATUS_LABELS[status]},
            )

    def complete_request(self, request_id: int, admin_id: int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ? AND state = 'active'", (request_id,)).fetchone()
            if not row:
                raise RepositoryError("Nie znaleziono aktywnego requestu.")
            conn.execute(
                "UPDATE requests SET state = 'completed', completed_at = ?, completed_by = ? WHERE id = ?",
                (utc_now(), admin_id, request_id),
            )
            self._notify_request_change(
                conn,
                request_id,
                "request.completed",
                row=row,
            )

    def restore_request(self, request_id: int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ? AND state = 'completed'", (request_id,)).fetchone()
            if not row:
                raise RepositoryError("Nie znaleziono zrealizowanej pozycji.")
            conn.execute(
                "UPDATE requests SET state = 'active', status = 'pending', completed_at = NULL, completed_by = NULL WHERE id = ?",
                (request_id,),
            )
            self._notify_request_change(
                conn,
                request_id,
                "request.restored",
                row=row,
            )

    def delete_request(self, request_id: int, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise RepositoryError("Powód usunięcia jest wymagany.")
        if len(reason) > 500:
            raise RepositoryError("Powód usunięcia może mieć maksymalnie 500 znaków.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM requests WHERE id = ? AND state = 'active'", (request_id,)).fetchone()
            if not row:
                raise RepositoryError("Nie znaleziono aktywnego requestu.")
            self._notify_request_change(
                conn,
                request_id,
                "request.deleted",
                row=row,
                keep_link=False,
                values={"reason": reason},
            )
            conn.execute("DELETE FROM requests WHERE id = ?", (request_id,))

    def notification_counts(self, user_id: int) -> dict[str, int]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread,
                    SUM(CASE WHEN read_at IS NOT NULL THEN 1 ELSE 0 END) AS read
                FROM notifications
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return {"unread": int(row["unread"] or 0), "read": int(row["read"] or 0)}

    def notifications(self, user_id: int, bucket: str = "all", limit: int = 100) -> dict[str, Any]:
        if bucket not in {"all", "unread", "read"}:
            raise RepositoryError("Nieprawidłowa karta powiadomień.")
        condition = "" if bucket == "all" else "AND read_at IS NULL" if bucket == "unread" else "AND read_at IS NOT NULL"
        with self.db.connect() as conn:
            user = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()
            language = normalize_language(user["language"] if user else "en")
            rows = conn.execute(
                f"SELECT * FROM notifications WHERE user_id = ? {condition} ORDER BY created_at DESC LIMIT ?",
                (user_id, min(max(limit, 1), 200)),
            ).fetchall()
            unread = conn.execute("SELECT COUNT(*) AS value FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)).fetchone()["value"]
            read = conn.execute("SELECT COUNT(*) AS value FROM notifications WHERE user_id = ? AND read_at IS NOT NULL", (user_id,)).fetchone()["value"]
        return {
            "items": [localized_notification(dict(row), language) for row in rows],
            "unread": int(unread),
            "read": int(read),
        }

    def mark_notifications_read(self, user_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL", (utc_now(), user_id))

    def mark_notification_read(self, user_id: int, notification_id: int) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ? AND user_id = ?",
                (utc_now(), notification_id, user_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("Nie znaleziono powiadomienia.")

    def delete_notification(self, user_id: int, notification_id: int) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM notifications WHERE id = ? AND user_id = ? AND read_at IS NOT NULL", (notification_id, user_id))
            if cursor.rowcount != 1:
                raise RepositoryError("Usunąć można tylko odczytane powiadomienie.")

    def delete_read_notifications(self, user_id: int) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM notifications WHERE user_id = ? AND read_at IS NOT NULL", (user_id,))
            return cursor.rowcount

    def broadcast(self, sender_id: int, title: str, body: str) -> int:
        title, body = title.strip(), body.strip()
        if not title or not body:
            raise RepositoryError("Tytuł i treść powiadomienia są wymagane.")
        if len(title) > 100 or len(body) > 1000:
            raise RepositoryError("Tytuł może mieć 100 znaków, a treść 1000 znaków.")
        with self.db.transaction() as conn:
            users = conn.execute("SELECT id FROM users WHERE is_active = 1").fetchall()
            for user in users:
                self._notify(conn, user["id"], "admin_messages", title, body, None)
            return len(users)

    def broadcast_localized(
        self,
        sender_id: int,
        *,
        title_en: str,
        body_en: str,
        title_pl: str,
        body_pl: str,
    ) -> int:
        values = {
            "en": (title_en.strip(), body_en.strip()),
            "pl": (title_pl.strip(), body_pl.strip()),
        }
        if any(not title or not body for title, body in values.values()):
            raise RepositoryError("Tytuł i treść powiadomienia są wymagane w obu językach.")
        if any(len(title) > 100 or len(body) > 1000 for title, body in values.values()):
            raise RepositoryError("Tytuł może mieć 100 znaków, a treść 1000 znaków.")
        with self.db.transaction() as conn:
            users = conn.execute(
                "SELECT id, language FROM users WHERE is_active = 1"
            ).fetchall()
            for user in users:
                language = normalize_language(user["language"])
                title, body = values[language]
                self._notify(
                    conn,
                    user["id"],
                    "admin_messages",
                    title,
                    body,
                    None,
                    event_key="admin.broadcast.bilingual",
                    event_payload={
                        "title_en": values["en"][0],
                        "body_en": values["en"][1],
                        "title_pl": values["pl"][0],
                        "body_pl": values["pl"][1],
                    },
                )
            return len(users)

    def promote_due_requests(self, today: date | None = None) -> int:
        value = (today or date.today()).isoformat()
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM requests WHERE state = 'upcoming' AND release_date IS NOT NULL AND release_date <= ?",
                (value,),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE requests SET state = 'active', status = 'pending' WHERE id = ?", (row["id"],))
                self._notify_request_change(
                    conn,
                    row["id"],
                    "request.promoted",
                    row=row,
                )
            return len(rows)

    def release_refresh_targets(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, tmdb_id, media_type, season_number FROM requests WHERE state IN ('active', 'upcoming') OR media_type = 'tv' ORDER BY release_data_refreshed_at IS NOT NULL, release_data_refreshed_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def title_backfill_targets(self) -> list[dict[str, Any]]:
        """Return one target per TMDB title without mutating the database."""

        with self.db.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
            title_en = "title_en" if "title_en" in columns else "NULL AS title_en"
            rows = conn.execute(
                f"""
                SELECT media_type, tmdb_id, {title_en}, COUNT(*) AS request_count
                FROM requests
                WHERE {"title_en IS NULL OR trim(title_en) = ''" if "title_en" in columns else "1 = 1"}
                GROUP BY media_type, tmdb_id
                ORDER BY media_type, tmdb_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_english_title_backfill(
        self, updates: list[tuple[str, int, str]]
    ) -> int:
        validated: list[tuple[str, int, str]] = []
        for media_type, tmdb_id, title_en in updates:
            value = str(title_en).strip()
            if media_type not in {"movie", "tv"} or not value:
                raise RepositoryError("Nieprawidłowe dane lokalizacji tytułu.")
            validated.append((value, media_type, int(tmdb_id)))
        affected = 0
        with self.db.transaction() as conn:
            for value, media_type, tmdb_id in validated:
                request_ids = [
                    int(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM requests WHERE media_type = ? AND tmdb_id = ?",
                        (media_type, tmdb_id),
                    ).fetchall()
                ]
                cursor = conn.execute(
                    """
                    UPDATE requests SET title_en = ?
                    WHERE media_type = ? AND tmdb_id = ?
                      AND (title_en IS NULL OR trim(title_en) = '')
                    """,
                    (value, media_type, tmdb_id),
                )
                affected += int(cursor.rowcount)
                for request_id in request_ids:
                    notifications = conn.execute(
                        """
                        SELECT id, event_payload_json FROM notifications
                        WHERE request_id = ? AND event_key LIKE 'request.%'
                          AND event_payload_json IS NOT NULL
                        """,
                        (request_id,),
                    ).fetchall()
                    for notification in notifications:
                        payload = decode_event_payload(notification["event_payload_json"])
                        payload["title_en"] = value
                        conn.execute(
                            "UPDATE notifications SET event_payload_json = ? WHERE id = ?",
                            (encode_event_payload(payload), notification["id"]),
                        )
        return affected

    def update_release_metadata(self, request_id: int, metadata: ReleaseMetadata) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE requests SET release_date = COALESCE(?, release_date),
                    release_year = COALESCE(?, release_year),
                    series_start_year = COALESCE(?, series_start_year),
                    series_end_year = ?, series_status = COALESCE(?, series_status),
                    world_theatrical_date = ?, world_digital_date = ?, world_physical_date = ?,
                    pl_theatrical_date = ?, pl_digital_date = ?, pl_physical_date = ?,
                    release_data_refreshed_at = ?
                WHERE id = ?
                """,
                (
                    metadata.release_date, metadata.release_year,
                    metadata.series_start_year, metadata.series_end_year, metadata.series_status,
                    metadata.world_theatrical_date, metadata.world_digital_date, metadata.world_physical_date,
                    metadata.pl_theatrical_date, metadata.pl_digital_date, metadata.pl_physical_date,
                    utc_now(), request_id,
                ),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, utc_now()),
            )
