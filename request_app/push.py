from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .database import Database, utc_now
from .i18n import normalize_language
from .notification_i18n import encode_event_payload, localized_notification, render_event
from .repository import RepositoryError


MAX_DELIVERY_ATTEMPTS = 3
MAX_ENDPOINT_LENGTH = 4096
MAX_KEY_LENGTH = 512
MAX_SUBSCRIPTIONS_PER_USER = 16


def _decode_base64url(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise RepositoryError("Subskrypcja zawiera nieprawidłowe klucze.") from exc


def _allowed_push_host(hostname: str) -> bool:
    host = hostname.rstrip(".").lower()
    return (
        host == "fcm.googleapis.com"
        or host == "updates.push.services.mozilla.com"
        or host.endswith(".push.apple.com")
        or host.endswith(".notify.windows.com")
    )


def validate_subscription(endpoint: str, p256dh: str, auth: str) -> tuple[str, str, str]:
    endpoint = endpoint.strip()
    p256dh = p256dh.strip()
    auth = auth.strip()
    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise RepositoryError("Nieprawidłowy adres subskrypcji Web Push.")
    if not p256dh or not auth or len(p256dh) > MAX_KEY_LENGTH or len(auth) > MAX_KEY_LENGTH:
        raise RepositoryError("Subskrypcja zawiera nieprawidłowe klucze.")

    parsed = urlsplit(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RepositoryError("Nieprawidłowy adres subskrypcji Web Push.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not _allowed_push_host(parsed.hostname)
    ):
        raise RepositoryError("Ten dostawca Web Push nie jest dozwolony.")

    public_key = _decode_base64url(p256dh)
    auth_secret = _decode_base64url(auth)
    if len(public_key) != 65 or public_key[0] != 4 or len(auth_secret) != 16:
        raise RepositoryError("Subskrypcja zawiera nieprawidłowe klucze.")
    return endpoint, p256dh, auth


class PushService:
    def __init__(self, db: Database, private_key_path: Path, subject: str):
        self.db = db
        self.private_key_path = private_key_path
        self.subject = subject
        self._public_key = ""

    @property
    def public_key(self) -> str:
        if not self._public_key:
            raise RuntimeError("Usługa Web Push nie została zainicjalizowana.")
        return self._public_key

    def initialize(self) -> None:
        self.private_key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.private_key_path.exists():
            private_key = serialization.load_pem_private_key(
                self.private_key_path.read_bytes(),
                password=None,
            )
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            try:
                descriptor = os.open(
                    self.private_key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                private_key = serialization.load_pem_private_key(
                    self.private_key_path.read_bytes(),
                    password=None,
                )
            else:
                with os.fdopen(descriptor, "wb") as key_file:
                    key_file.write(pem)

        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise RuntimeError("Klucz VAPID musi używać krzywej P-256.")

        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        self._public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")

    def subscription_count(self, user_id: int) -> int:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS value FROM push_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["value"])

    def save_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str) -> int:
        endpoint, p256dh, auth = validate_subscription(endpoint, p256dh, auth)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT id, user_id FROM push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            ).fetchone()
            if existing:
                transferred = int(existing["user_id"]) != user_id
                if transferred:
                    destination_count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS value FROM push_subscriptions WHERE user_id = ?",
                            (user_id,),
                        ).fetchone()["value"]
                    )
                    if destination_count >= MAX_SUBSCRIPTIONS_PER_USER:
                        raise RepositoryError("Osiągnięto limit urządzeń Web Push dla tego konta.")
                    start_notification_id = int(
                        conn.execute(
                            "SELECT COALESCE(MAX(id), 0) AS value FROM notifications WHERE user_id = ?",
                            (user_id,),
                        ).fetchone()["value"]
                    )
                    conn.execute(
                        "DELETE FROM push_deliveries WHERE subscription_id = ?",
                        (existing["id"],),
                    )
                    conn.execute(
                        """
                        UPDATE push_subscriptions
                        SET user_id = ?, p256dh = ?, auth = ?, start_notification_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            user_id,
                            p256dh,
                            auth,
                            start_notification_id,
                            utc_now(),
                            existing["id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE push_subscriptions SET p256dh = ?, auth = ?, updated_at = ? WHERE id = ?",
                        (p256dh, auth, utc_now(), existing["id"]),
                    )
                return int(existing["id"])

            subscription_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS value FROM push_subscriptions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["value"]
            )
            if subscription_count >= MAX_SUBSCRIPTIONS_PER_USER:
                raise RepositoryError("Osiągnięto limit urządzeń Web Push dla tego konta.")
            start_notification_id = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS value FROM notifications WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["value"]
            )
            cursor = conn.execute(
                """
                INSERT INTO push_subscriptions
                (user_id, endpoint, p256dh, auth, start_notification_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    endpoint,
                    p256dh,
                    auth,
                    start_notification_id,
                    utc_now(),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def remove_subscription(self, user_id: int, endpoint: str) -> bool:
        endpoint = endpoint.strip()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (user_id, endpoint),
            )
            return cursor.rowcount == 1

    def create_test_notification(self, user_id: int) -> int:
        with self.db.transaction() as conn:
            user = conn.execute("SELECT language FROM users WHERE id = ?", (user_id,)).fetchone()
            language = normalize_language(user["language"] if user else "en")
            payload: dict[str, Any] = {}
            title, body = render_event("system.push_test", payload, language) or ("", "")
            cursor = conn.execute(
                """
                INSERT INTO notifications
                (user_id, type, title, body, request_id, event_key, event_payload_json, created_at)
                VALUES (?, 'system', ?, ?, NULL, 'system.push_test', ?, ?)
                """,
                (
                    user_id,
                    title,
                    body,
                    encode_event_payload(payload),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _retry_ready(attempts: int, last_attempt_at: str | None) -> bool:
        if not last_attempt_at:
            return True
        try:
            last_attempt = datetime.fromisoformat(last_attempt_at)
            if last_attempt.tzinfo is None:
                last_attempt = last_attempt.replace(tzinfo=UTC)
        except ValueError:
            return True
        delay = min(60 * (2 ** max(0, attempts - 1)), 900)
        return datetime.now(UTC) >= last_attempt + timedelta(seconds=delay)

    def _pending_deliveries(self, limit: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT n.id AS notification_id, n.title, n.body, n.created_at,
                       n.event_key, n.event_payload_json, u.language,
                       s.id AS subscription_id, s.endpoint, s.p256dh, s.auth,
                       d.attempts, d.last_attempt_at
                FROM notifications AS n
                JOIN push_subscriptions AS s
                  ON s.user_id = n.user_id
                 AND n.id > s.start_notification_id
                JOIN users AS u
                  ON u.id = n.user_id
                 AND u.is_active = 1
                LEFT JOIN push_deliveries AS d
                  ON d.notification_id = n.id
                 AND d.subscription_id = s.id
                WHERE d.delivered_at IS NULL
                  AND COALESCE(d.attempts, 0) < ?
                ORDER BY n.id, s.id
                LIMIT ?
                """,
                (MAX_DELIVERY_ATTEMPTS, min(max(limit, 1), 500)),
            ).fetchall()
        return [
            localized_notification(dict(row), row["language"])
            for row in rows
            if self._retry_ready(int(row["attempts"] or 0), row["last_attempt_at"])
        ]

    def _send(self, item: dict[str, Any], payload: str) -> None:
        webpush(
            subscription_info={
                "endpoint": item["endpoint"],
                "keys": {
                    "p256dh": item["p256dh"],
                    "auth": item["auth"],
                },
            },
            data=payload,
            vapid_private_key=str(self.private_key_path),
            vapid_claims={"sub": self.subject},
            ttl=86400,
            timeout=10,
        )

    def _record_attempt(
        self,
        notification_id: int,
        subscription_id: int,
        *,
        delivered: bool,
        error: str | None,
    ) -> None:
        timestamp = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO push_deliveries
                (notification_id, subscription_id, attempts, last_attempt_at, delivered_at, last_error)
                VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(notification_id, subscription_id) DO UPDATE SET
                    attempts = push_deliveries.attempts + 1,
                    last_attempt_at = excluded.last_attempt_at,
                    delivered_at = excluded.delivered_at,
                    last_error = excluded.last_error
                """,
                (
                    notification_id,
                    subscription_id,
                    timestamp,
                    timestamp if delivered else None,
                    error,
                ),
            )

    def _record_terminal_failure(
        self,
        notification_id: int,
        subscription_id: int,
        error: str,
    ) -> None:
        timestamp = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO push_deliveries
                (notification_id, subscription_id, attempts, last_attempt_at, delivered_at, last_error)
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(notification_id, subscription_id) DO UPDATE SET
                    attempts = excluded.attempts,
                    last_attempt_at = excluded.last_attempt_at,
                    delivered_at = NULL,
                    last_error = excluded.last_error
                """,
                (
                    notification_id,
                    subscription_id,
                    MAX_DELIVERY_ATTEMPTS,
                    timestamp,
                    error,
                ),
            )

    def _drop_subscription(self, subscription_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (subscription_id,))

    def deliver_pending(
        self,
        *,
        limit: int = 100,
        sender: Callable[[dict[str, Any], str], None] | None = None,
    ) -> int:
        if not self._public_key:
            return 0
        send = sender or self._send
        delivered = 0
        for item in self._pending_deliveries(limit):
            payload = json.dumps(
                {
                    "title": str(item["title"])[:100],
                    "body": str(item["body"])[:1800],
                    "url": "/",
                    "tag": f"notification-{item['notification_id']}",
                    "notification_id": item["notification_id"],
                    "language": normalize_language(item.get("language")),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            try:
                send(item, payload)
            except WebPushException as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in {404, 410}:
                    self._drop_subscription(int(item["subscription_id"]))
                    continue
                error = f"WebPushException:{status or 'unknown'}"
                if status is not None and 400 <= status < 500 and status not in {408, 425, 429}:
                    self._record_terminal_failure(
                        int(item["notification_id"]),
                        int(item["subscription_id"]),
                        error,
                    )
                else:
                    self._record_attempt(
                        int(item["notification_id"]),
                        int(item["subscription_id"]),
                        delivered=False,
                        error=error,
                    )
            except Exception as exc:
                self._record_attempt(
                    int(item["notification_id"]),
                    int(item["subscription_id"]),
                    delivered=False,
                    error=type(exc).__name__[:80],
                )
            else:
                self._record_attempt(
                    int(item["notification_id"]),
                    int(item["subscription_id"]),
                    delivered=True,
                    error=None,
                )
                delivered += 1
        return delivered
