import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace
import zlib

import pytest
from pywebpush import WebPushException

from request_app.database import Database, utc_now
from request_app.pwa import manifest_document
from request_app.push import MAX_SUBSCRIPTIONS_PER_USER, PushService, validate_subscription
from request_app.repository import Repository, RepositoryError


ROOT = Path(__file__).resolve().parents[1]
P256DH = "B" + ("A" * 86)
AUTH = "A" * 22


def make_service(tmp_path):
    db = Database(tmp_path / "push.db")
    db.initialize()
    repo = Repository(db)
    user = repo.create_user("anna", "DobreHasloTest123")
    service = PushService(db, tmp_path / ".vapid-private.pem", "mailto:test@example.invalid")
    service.initialize()
    return db, user, service


def test_manifest_service_worker_and_favicon_are_wired():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    login = (ROOT / "request_app" / "templates" / "login.html").read_text(encoding="utf-8")
    force_password = (ROOT / "request_app" / "templates" / "force_password.html").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "request_app" / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
    )
    worker = (ROOT / "request_app" / "static" / "service-worker.js").read_text(encoding="utf-8")
    pwa = (ROOT / "request_app" / "static" / "pwa.js").read_text(encoding="utf-8")
    css = (ROOT / "request_app" / "static" / "app.css").read_text(encoding="utf-8")
    public_server = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    assert all(
        'rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"' in template
        for template in (base, login, force_password)
    )
    assert all('rel="icon"' in template for template in (base, login, force_password))
    assert manifest == manifest_document("en")
    assert manifest_document("pl")["description"] == "Prywatna lista requestów filmów i seriali."
    assert manifest_document("pl")["lang"] == "pl"
    assert manifest["id"] == manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} >= {"192x192", "512x512"}
    assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])
    assert 'self.addEventListener("push"' in worker
    assert 'self.addEventListener("notificationclick"' in worker
    assert 'const ACTIVATE_UPDATE = "ACTIVATE_UPDATE"' in worker
    assert "event.data?.type === ACTIVATE_UPDATE" in worker
    assert "event.waitUntil(self.skipWaiting())" in worker
    assert "self.clients.claim" not in worker
    assert 'self.addEventListener("fetch"' not in worker
    assert "caches.open(" not in worker
    assert 'pl: "Masz nowe powiadomienie."' in worker
    assert "defaultNotification(payload?.language)" in worker
    assert 'icon: "/static/icons/pwa-192.png"' in worker
    assert 'badge: "/static/icons/notification-badge-96.png"' in worker
    assert 'badge: "/static/icons/badge-96.png"' not in worker
    assert "Notification.requestPermission()" in pwa
    assert 'Notification.permission === "denied"' in pwa
    assert '!("serviceWorker" in navigator)' in pwa
    assert '!("PushManager" in window)' in pwa
    assert '!("Notification" in window)' in pwa
    assert "userVisibleOnly: true" in pwa
    assert "applicationServerKey: base64UrlToBytes(config.public_key)" in pwa
    assert "beforeinstallprompt" in pwa
    assert 'registration.addEventListener("updatefound"' in pwa
    assert "registration.update().catch" in pwa
    assert "WORKER_UPDATE_INTERVAL_MS" in pwa
    assert 'navigator.serviceWorker?.addEventListener("controllerchange"' in pwa
    assert 'registration.waiting.postMessage({ type: "ACTIVATE_UPDATE" })' in pwa
    assert "window.__penczreqPwaInitialized" in pwa
    assert "if (updateReloadStarted) return" in pwa
    assert "@media (max-width: 600px)" in css
    assert ".pwa-update-banner > div { display: grid; grid-template-columns: 1fr 1fr; }" in css
    assert '@app.get("/manifest.webmanifest"' in public_server
    assert "manifest_document(request.state.language)" in public_server


@pytest.mark.parametrize(
    ("filename", "expected_size"),
    (
        ("pwa-192.png", (192, 192)),
        ("pwa-512.png", (512, 512)),
        ("pwa-maskable-512.png", (512, 512)),
        ("notification-badge-96.png", (96, 96)),
    ),
)
def test_pwa_png_assets_match_their_declared_dimensions(filename, expected_size):
    payload = (ROOT / "request_app" / "static" / "icons" / filename).read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", payload[16:24]) == expected_size


def _decode_rgba_png(payload: bytes) -> tuple[int, int, bytes]:
    width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[16:26])
    assert (bit_depth, color_type) == (8, 6)
    position = 8
    compressed = bytearray()
    while position < len(payload):
        length = struct.unpack(">I", payload[position : position + 4])[0]
        name = payload[position + 4 : position + 8]
        data = payload[position + 8 : position + 8 + length]
        if name == b"IDAT":
            compressed.extend(data)
        position += 12 + length
    encoded = zlib.decompress(bytes(compressed))
    stride = width * 4
    previous = bytearray(stride)
    decoded = bytearray()
    offset = 0
    for _ in range(height):
        filter_type = encoded[offset]
        raw = encoded[offset + 1 : offset + 1 + stride]
        offset += stride + 1
        row = bytearray(stride)
        for index, value in enumerate(raw):
            left = row[index - 4] if index >= 4 else 0
            above = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                predictor = (left, above, upper_left)[distances.index(min(distances))]
            else:
                raise AssertionError(f"Unsupported PNG filter: {filter_type}")
            row[index] = (value + predictor) & 0xFF
        decoded.extend(row)
        previous = row
    return width, height, bytes(decoded)


def test_android_notification_badge_is_a_transparent_monochrome_mark():
    payload = (
        ROOT / "request_app" / "static" / "icons" / "notification-badge-96.png"
    ).read_bytes()
    width, height, pixels = _decode_rgba_png(payload)
    alphas = pixels[3::4]
    opaque_pixels = sum(alpha == 255 for alpha in alphas)
    transparent_pixels = sum(alpha == 0 for alpha in alphas)

    assert (width, height) == (96, 96)
    assert pixels[3] == 0  # transparent top-left corner, not a solid square
    assert opaque_pixels > 1_000
    assert transparent_pixels > (width * height) // 2
    assert all(
        red == green == blue == 255
        for red, green, blue, alpha in zip(
            pixels[0::4], pixels[1::4], pixels[2::4], alphas, strict=True
        )
        if alpha
    )


def test_vapid_key_is_stable_and_not_stored_in_source(tmp_path):
    _, _, service = make_service(tmp_path)
    first_public = service.public_key
    private_key = (tmp_path / ".vapid-private.pem").read_bytes()
    assert len(first_public) > 80
    second = PushService(
        service.db,
        tmp_path / ".vapid-private.pem",
        "mailto:test@example.invalid",
    )
    second.initialize()
    assert second.public_key == first_public
    assert private_key not in service.db.path.read_bytes()
    assert not (ROOT / ".vapid-private.pem").exists()
    if os.name == "posix":
        metadata = (tmp_path / ".vapid-private.pem").stat()
        assert metadata.st_mode & 0o777 == 0o600
        assert metadata.st_uid == os.getuid()
        assert metadata.st_gid == os.getgid()


def test_saving_the_same_subscription_twice_is_idempotent(tmp_path):
    db, user, service = make_service(tmp_path)
    endpoint = "https://fcm.googleapis.com/fcm/send/reused-browser-endpoint"

    first_id = service.save_subscription(user["id"], endpoint, P256DH, AUTH)
    second_id = service.save_subscription(user["id"], endpoint, P256DH, AUTH)

    assert second_id == first_id
    assert service.subscription_count(user["id"]) == 1
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()[0] == 1


def test_push_subscriptions_are_capped_per_user_without_breaking_refresh(tmp_path):
    db, user, service = make_service(tmp_path)
    endpoints = [
        f"https://fcm.googleapis.com/fcm/send/capped-browser-{index}"
        for index in range(MAX_SUBSCRIPTIONS_PER_USER)
    ]
    subscription_ids = [
        service.save_subscription(user["id"], endpoint, P256DH, AUTH)
        for endpoint in endpoints
    ]

    assert service.subscription_count(user["id"]) == MAX_SUBSCRIPTIONS_PER_USER
    assert service.save_subscription(user["id"], endpoints[0], P256DH, AUTH) == subscription_ids[0]
    with pytest.raises(RepositoryError, match="limit urządzeń"):
        service.save_subscription(
            user["id"],
            "https://fcm.googleapis.com/fcm/send/one-device-too-many",
            P256DH,
            AUTH,
        )

    _create_notification(db, user["id"], title="Limit")
    calls = []
    assert service.deliver_pending(
        limit=500, sender=lambda item, _payload: calls.append(item["subscription_id"])
    ) == MAX_SUBSCRIPTIONS_PER_USER
    assert len(calls) == MAX_SUBSCRIPTIONS_PER_USER


def test_subscription_transfer_cannot_exceed_destination_user_cap(tmp_path):
    db, first_user, service = make_service(tmp_path)
    repo = Repository(db)
    second_user = repo.create_user("bartek", "JeszczeLepszeHaslo123")
    transferred_endpoint = "https://fcm.googleapis.com/fcm/send/owned-by-first-user"
    subscription_id = service.save_subscription(
        first_user["id"], transferred_endpoint, P256DH, AUTH
    )
    for index in range(MAX_SUBSCRIPTIONS_PER_USER):
        service.save_subscription(
            second_user["id"],
            f"https://fcm.googleapis.com/fcm/send/full-destination-{index}",
            P256DH,
            AUTH,
        )

    with pytest.raises(RepositoryError, match="limit urządzeń"):
        service.save_subscription(
            second_user["id"], transferred_endpoint, P256DH, AUTH
        )

    assert service.subscription_count(first_user["id"]) == 1
    assert service.subscription_count(second_user["id"]) == MAX_SUBSCRIPTIONS_PER_USER
    with db.connect() as conn:
        owner = conn.execute(
            "SELECT user_id FROM push_subscriptions WHERE id = ?", (subscription_id,)
        ).fetchone()["user_id"]
    assert int(owner) == first_user["id"]


def test_subscription_validation_blocks_arbitrary_endpoints():
    with pytest.raises(RepositoryError):
        validate_subscription("https://192.168.1.10/push", P256DH, AUTH)
    with pytest.raises(RepositoryError):
        validate_subscription("http://fcm.googleapis.com/push", P256DH, AUTH)
    with pytest.raises(RepositoryError):
        validate_subscription("https://fcm.googleapis.com:bad/push", P256DH, AUTH)


def test_new_internal_notification_is_delivered_once(tmp_path):
    db, user, service = make_service(tmp_path)
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/test-endpoint",
        P256DH,
        AUTH,
    )
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, created_at)
            VALUES (?, 'system', 'Tytuł', 'Treść', NULL, ?)
            """,
            (user["id"], utc_now()),
        )
    payloads = []
    assert service.deliver_pending(sender=lambda item, payload: payloads.append(payload)) == 1
    assert service.deliver_pending(sender=lambda item, payload: payloads.append(payload)) == 0
    assert json.loads(payloads[0])["title"] == "Tytuł"
    assert json.loads(payloads[0])["language"] == "en"


def test_existing_notifications_are_not_replayed_on_new_subscription(tmp_path):
    db, user, service = make_service(tmp_path)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, request_id, created_at)
            VALUES (?, 'system', 'Stare', 'Nie wysyłaj', NULL, ?)
            """,
            (user["id"], utc_now()),
        )
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/new-endpoint",
        P256DH,
        AUTH,
    )
    assert service.deliver_pending(sender=lambda item, payload: None) == 0


def _create_notification(db, user_id, *, title="Tytuł", body="Treść"):
    with db.transaction() as conn:
        return int(
            conn.execute(
                """
                INSERT INTO notifications
                    (user_id, type, title, body, request_id, created_at)
                VALUES (?, 'system', ?, ?, NULL, ?)
                """,
                (user_id, title, body, utc_now()),
            ).lastrowid
        )


@pytest.mark.parametrize("status_code", (404, 410))
def test_expired_push_endpoint_is_removed_without_retry(tmp_path, status_code):
    db, user, service = make_service(tmp_path)
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/expired-endpoint",
        P256DH,
        AUTH,
    )
    _create_notification(db, user["id"])

    def expired(_item, _payload):
        raise WebPushException(
            "expired",
            response=SimpleNamespace(status_code=status_code, text="endpoint expired"),
        )

    assert service.deliver_pending(sender=expired) == 0
    assert service.subscription_count(user["id"]) == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("status_code", (400, 401, 403, 413))
def test_permanent_push_failure_is_not_retried(tmp_path, status_code):
    db, user, service = make_service(tmp_path)
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/permanent-failure-endpoint",
        P256DH,
        AUTH,
    )
    notification_id = _create_notification(db, user["id"])
    calls = 0

    def rejected(_item, _payload):
        nonlocal calls
        calls += 1
        raise WebPushException(
            "rejected",
            response=SimpleNamespace(status_code=status_code, text="request rejected"),
        )

    assert service.deliver_pending(sender=rejected) == 0
    assert service.deliver_pending(sender=rejected) == 0
    assert calls == 1
    assert service.subscription_count(user["id"]) == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT attempts, last_error FROM push_deliveries WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
    assert dict(row) == {
        "attempts": 3,
        "last_error": f"WebPushException:{status_code}",
    }


@pytest.mark.parametrize("status_code", (408, 425, 429, 500, 503))
def test_transient_http_push_failure_remains_retryable(tmp_path, status_code):
    db, user, service = make_service(tmp_path)
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/transient-failure-endpoint",
        P256DH,
        AUTH,
    )
    notification_id = _create_notification(db, user["id"])

    def unavailable(_item, _payload):
        raise WebPushException(
            "temporarily unavailable",
            response=SimpleNamespace(status_code=status_code, text="retry later"),
        )

    assert service.deliver_pending(sender=unavailable) == 0
    with db.connect() as conn:
        row = conn.execute(
            "SELECT attempts, last_error FROM push_deliveries WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
    assert dict(row) == {
        "attempts": 1,
        "last_error": f"WebPushException:{status_code}",
    }


def test_transient_push_failure_is_bounded_and_does_not_store_exception_text(tmp_path):
    db, user, service = make_service(tmp_path)
    service.save_subscription(
        user["id"],
        "https://fcm.googleapis.com/fcm/send/retry-endpoint",
        P256DH,
        AUTH,
    )
    notification_id = _create_notification(db, user["id"])
    service._retry_ready = lambda _attempts, _last_attempt_at: True

    def failing_sender(_item, _payload):
        raise RuntimeError("sensitive diagnostic text")

    assert service.deliver_pending(sender=failing_sender) == 0
    assert service.deliver_pending(sender=failing_sender) == 0
    assert service.deliver_pending(sender=failing_sender) == 0
    assert service.deliver_pending(sender=failing_sender) == 0

    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT attempts, delivered_at, last_error
            FROM push_deliveries
            WHERE notification_id = ?
            """,
            (notification_id,),
        ).fetchone()
    assert dict(row) == {
        "attempts": 3,
        "delivered_at": None,
        "last_error": "RuntimeError",
    }


def test_push_endpoint_can_move_between_accounts_without_replaying_history(tmp_path):
    db, first_user, service = make_service(tmp_path)
    repo = Repository(db)
    second_user = repo.create_user("ewa", "DrugieDobreHaslo123")
    endpoint = "https://fcm.googleapis.com/fcm/send/shared-browser-endpoint"
    subscription_id = service.save_subscription(first_user["id"], endpoint, P256DH, AUTH)
    _create_notification(db, second_user["id"], title="Historyczne")

    transferred_id = service.save_subscription(second_user["id"], endpoint, P256DH, AUTH)
    assert transferred_id == subscription_id
    assert service.subscription_count(first_user["id"]) == 0
    assert service.subscription_count(second_user["id"]) == 1
    assert service.deliver_pending(sender=lambda _item, _payload: None) == 0

    _create_notification(db, second_user["id"], title="Nowe")
    payloads = []
    assert service.deliver_pending(sender=lambda _item, payload: payloads.append(payload)) == 1
    assert json.loads(payloads[0])["title"] == "Nowe"
