from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from request_app.i18n import translate
from request_app.security import validate_password


ROOT = Path(__file__).resolve().parents[1]


def test_control_user_action_dialogs_have_action_specific_input_contracts():
    javascript = (ROOT / "request_app" / "static" / "control.js").read_text(
        encoding="utf-8"
    )
    template = (
        ROOT / "request_app" / "templates" / "control_index.html"
    ).read_text(encoding="utf-8")
    css = (ROOT / "request_app" / "static" / "control.css").read_text(
        encoding="utf-8"
    )

    assert "Nowa wartość" not in javascript
    assert "Nowa wartość" not in template
    assert '.dialog-card [hidden] { display: none; }' in css
    assert "field.disabled = !hasInput" in javascript
    assert "field.required = hasInput" in javascript
    assert "hasInput ? field.value : true" in javascript
    assert 'field.pattern = input?.pattern || ""' not in javascript
    assert "field.removeAttribute(name)" in javascript
    assert "field.setAttribute(name, String(value))" in javascript
    assert 'type: "password", minLength: 15, maxLength: 128' in javascript
    assert "pattern: PASSWORD_PATTERN" in javascript
    assert 'autocomplete: "new-password"' in javascript
    assert 'temporary_password: value' in javascript
    assert 'body: { active: !user.is_active, current_password: reauth.value }' in javascript
    assert 'body: { current_password: reauth.value }' in javascript
    assert 'data-user-action="delete"' in javascript
    assert 'method: "DELETE"' in javascript
    assert 'destructive: true' in javascript
    assert 'data-user-dialog-confirm' in template
    assert "Wymagane przy blokowaniu lub trwałym usuwaniu konta" in template
    assert 'confirmButton.classList.toggle("danger", destructive)' in javascript
    assert 'control-action-note' in css
    assert 'class="control-button danger protected"' in javascript
    assert '.control-button.protected:disabled { cursor: not-allowed; }' in css
    for title in (
        "Zablokuj użytkownika",
        "Odblokuj użytkownika",
        "Zakończ sesje użytkownika",
        "Przekaż administratora",
        "Trwale usuń użytkownika",
    ):
        assert f'tr("{title}")' in javascript


def test_control_user_action_copy_is_complete_in_polish_and_english():
    sources = (
        "Zablokuj użytkownika",
        "Odblokuj użytkownika",
        "Zakończ sesje użytkownika",
        "Przekaż administratora",
        "Nowe hasło tymczasowe",
        "Konto użytkownika zostało zablokowane.",
        "Konto użytkownika zostało odblokowane.",
        "Sesje i subskrypcje użytkownika zostały zakończone.",
        "Rola administratora publicznego została przekazana.",
        "Usuń użytkownika",
        "Trwale usuń użytkownika",
        "Trwale usunąć konto {username}? Tej operacji nie można cofnąć tak jak blokady. Sesje, subskrypcje push, powiadomienia i aktywne uczestnictwo zostaną usunięte.",
        "Najpierw przekaż rolę administratora innemu aktywnemu kontu.",
        "Użytkownik został trwale usunięty.",
        "Wymagane przy blokowaniu lub trwałym usuwaniu konta, resecie hasła, zmianie loginu, sesji i przekazaniu administratora.",
    )
    for source in sources:
        assert translate(source, "pl") == source
        english = translate(source, "en")
        assert english != source
        assert not any(character in english for character in "ąćęłńóśźż")


def test_reset_password_browser_pattern_matches_the_declared_backend_policy():
    javascript = (ROOT / "request_app" / "static" / "control.js").read_text(
        encoding="utf-8"
    )
    matched = re.search(r'const PASSWORD_PATTERN = "([^"]+)";', javascript)
    assert matched
    browser_pattern = matched.group(1).replace("\\\\", "\\")

    candidates = {
        "ValidTemporaryPassword9": True,
        "ShortPassword9": False,
        "alllowercasepassword9": False,
        "ALLUPPERCASEPASSWORD9": False,
        "PasswordWithoutDigit": False,
        "HasłoTemporaryPassword9": False,
        "A" * 127 + "a9": False,
    }
    for password, expected in candidates.items():
        assert bool(re.fullmatch(browser_pattern, password)) is expected
        assert (validate_password(password) is None) is expected


def test_control_user_management_endpoints_enforce_contracts(tmp_path: Path):
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "APP_COMPONENT": "control",
            "DATA_DIR": str(tmp_path / "data"),
            "CONTROL_DATA_DIR": str(tmp_path / "control"),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "LOG_DIR": str(tmp_path / "logs"),
            "SESSION_SECRET": "S" * 48,
            "CONTROL_SESSION_SECRET": "C" * 48,
            "CONFIG_ENCRYPTION_KEY": "E" * 48,
            "COOKIE_SECURE": "false",
            "PUBLIC_ADMIN_USERNAME": "contract-admin",
            "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": "PublicContractPassword99Z",  # pragma: allowlist secret
            "CONTROL_ADMIN_USERNAME": "contract-control",
            "CONTROL_BOOTSTRAP_PASSWORD": "ControlStartPassword99Z",  # pragma: allowlist secret
            "CONTROL_ALLOWED_NETWORKS": "127.0.0.0/8",
            "CONTROL_ALLOWED_HOSTS": "testserver",
        }
    )
    script = r'''
import json
import re
from fastapi.testclient import TestClient

from request_app import control
from request_app.database import utc_now
from request_app.security import create_session, verify_password

START = "ControlStartPassword99Z"
FINAL = "ControlFinalPassword99Z"
TEMPORARY = "TemporaryUserPassword99Z"
RESET = "ResetUserPassword99Z"

def token(html, name):
    matched = re.search(fr'name="{name}" value="([^"]+)"', html)
    assert matched
    return matched.group(1)

def counts(user_id):
    with control.db.connect() as conn:
        return {
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0],
            "push": conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchone()[0],
        }

def seed_runtime_state(user_id, suffix):
    create_session(control.db, user_id, secret=control.settings.session_secret, days=1)
    with control.db.transaction() as conn:
        conn.execute(
            """INSERT INTO push_subscriptions
               (user_id, endpoint, p256dh, auth, start_notification_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (user_id, f"https://fcm.googleapis.com/fcm/send/{suffix}", "B" + "A" * 86, "A" * 22, utc_now(), utc_now()),
        )

with TestClient(control.app, base_url="http://testserver", follow_redirects=False, client=("127.0.0.1", 50000)) as panel:
    login_page = panel.get("/login")
    logged = panel.post("/login", data={
        "username": "contract-control",
        "password": START,
        "login_csrf_token": token(login_page.text, "login_csrf_token"),
    })
    assert logged.status_code == 303
    forced = panel.get("/force-password")
    changed = panel.post("/force-password", data={
        "current_password": START,
        "new_password": FINAL,
        "confirm_password": FINAL,
        "csrf_token": token(forced.text, "csrf_token"),
    })
    assert changed.status_code == 303
    login_page = panel.get("/login")
    logged = panel.post("/login", data={
        "username": "contract-control",
        "password": FINAL,
        "login_csrf_token": token(login_page.text, "login_csrf_token"),
    })
    assert logged.status_code == 303
    csrf = token(panel.get("/").text, "csrf_token")
    headers = {"X-CSRF-Token": csrf}

    created = panel.post("/api/control/users", json={
        "username": "contract-user",
        "temporary_password": TEMPORARY,  # pragma: allowlist secret
        "current_password": FINAL,
    }, headers=headers)
    assert created.status_code == 200
    assert "password_hash" not in created.text and TEMPORARY not in created.text
    user_id = created.json()["item"]["id"]

    delete_created = panel.post("/api/control/users", json={
        "username": "delete-user",
        "temporary_password": TEMPORARY,  # pragma: allowlist secret
        "current_password": FINAL,
    }, headers=headers)
    assert delete_created.status_code == 200
    delete_user_id = delete_created.json()["item"]["id"]

    delete_csrf_denied = panel.request(
        "DELETE", f"/api/control/users/{delete_user_id}",
        json={"current_password": FINAL}, headers={"X-CSRF-Token": "wrong"},
    )
    delete_reauth_denied = panel.request(
        "DELETE", f"/api/control/users/{delete_user_id}",
        json={"current_password": "wrong-password"},  # pragma: allowlist secret
        headers=headers,
    )
    assert delete_csrf_denied.status_code == 403
    assert delete_reauth_denied.status_code == 400

    protected_admin = control.repo.user_by_username("contract-admin")
    admin_delete_denied = panel.request(
        "DELETE", f"/api/control/users/{protected_admin['id']}",
        json={"current_password": FINAL}, headers=headers,
    )
    assert admin_delete_denied.status_code == 400
    assert admin_delete_denied.json()["detail"] == (
        "Transfer the administrator role to another active account first."
    )

    seed_runtime_state(delete_user_id, "delete")
    deleted = panel.request(
        "DELETE", f"/api/control/users/{delete_user_id}",
        json={"current_password": FINAL}, headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["result"]["target_username"] == "delete-user"
    assert "password" not in deleted.text.casefold()
    assert control.repo.user_by_id(delete_user_id) is None
    assert counts(delete_user_id) == {"sessions": 0, "push": 0}
    listed_ids = {item["id"] for item in panel.get("/api/control/users").json()["items"]}
    assert delete_user_id not in listed_ids

    csrf_denied = panel.put(f"/api/control/users/{user_id}/active", json={
        "active": False, "current_password": FINAL,
    }, headers={"X-CSRF-Token": "wrong"})
    reauth_denied = panel.put(f"/api/control/users/{user_id}/active", json={
        "active": False, "current_password": "wrong-password",  # pragma: allowlist secret
    }, headers=headers)
    assert csrf_denied.status_code == 403
    assert reauth_denied.status_code == 400

    seed_runtime_state(user_id, "disable")
    disabled = panel.put(f"/api/control/users/{user_id}/active", json={
        "active": False, "current_password": FINAL,
    }, headers=headers)
    assert disabled.status_code == 200
    assert control.repo.user_by_id(user_id)["is_active"] == 0
    assert counts(user_id) == {"sessions": 0, "push": 0}
    restored = panel.put(f"/api/control/users/{user_id}/active", json={
        "active": True, "current_password": FINAL,
    }, headers=headers)
    assert restored.status_code == 200
    assert control.repo.user_by_id(user_id)["is_active"] == 1

    seed_runtime_state(user_id, "password")
    reset_csrf_denied = panel.put(f"/api/control/users/{user_id}/password", json={
        "temporary_password": RESET, "current_password": FINAL,  # pragma: allowlist secret
    }, headers={"X-CSRF-Token": "wrong"})
    reset_reauth_denied = panel.put(f"/api/control/users/{user_id}/password", json={
        "temporary_password": RESET, "current_password": "wrong-password",  # pragma: allowlist secret
    }, headers=headers)
    reset_policy_denied = panel.put(f"/api/control/users/{user_id}/password", json={
        "temporary_password": "lowercasepassword99", "current_password": FINAL,  # pragma: allowlist secret
    }, headers=headers)
    assert reset_csrf_denied.status_code == 403
    assert reset_reauth_denied.status_code == 400
    assert reset_policy_denied.status_code == 400
    assert counts(user_id) == {"sessions": 1, "push": 1}
    reset = panel.put(f"/api/control/users/{user_id}/password", json={
        "temporary_password": RESET, "current_password": FINAL,  # pragma: allowlist secret
    }, headers=headers)
    assert reset.status_code == 200
    reset_user = control.repo.user_by_id(user_id)
    assert reset_user["must_change_password"] == 1
    assert verify_password(reset_user["password_hash"], RESET)
    assert counts(user_id) == {"sessions": 0, "push": 0}

    renamed = panel.put(f"/api/control/users/{user_id}/username", json={
        "username": "renamed-user", "current_password": FINAL,
    }, headers=headers)
    assert renamed.status_code == 200
    assert control.repo.user_by_id(user_id)["username"] == "renamed-user"

    seed_runtime_state(user_id, "sessions")
    revoked = panel.post(f"/api/control/users/{user_id}/revoke-sessions", json={
        "current_password": FINAL,
    }, headers=headers)
    assert revoked.status_code == 200 and revoked.json()["revoked"] == 1
    assert counts(user_id) == {"sessions": 0, "push": 0}

    old_admin = control.repo.user_by_username("contract-admin")
    create_session(control.db, old_admin["id"], secret=control.settings.session_secret, days=1)
    create_session(control.db, user_id, secret=control.settings.session_secret, days=1)
    transferred = panel.put(f"/api/control/users/{user_id}/admin", json={
        "current_password": FINAL,
    }, headers=headers)
    assert transferred.status_code == 200
    assert control.repo.user_by_id(user_id)["role"] == "admin"
    assert control.repo.user_by_id(old_admin["id"])["role"] == "user"
    with control.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        events = {row[0] for row in conn.execute("SELECT event_type FROM security_events")}
        delete_event = conn.execute(
            """SELECT details_json FROM security_events
               WHERE event_type = 'public_user_deleted'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        delete_details = json.loads(delete_event[0])
        assert delete_details["target_user_id"] == delete_user_id
        assert delete_details["target_username"] == "delete-user"
        assert delete_details["sessions_revoked"] == 1
        assert delete_details["push_subscriptions_removed"] == 1
        assert not any(
            sensitive in delete_event[0].casefold()
            for sensitive in ("password", "token", "secret", "authorization")
        )

expected_events = {
    "public_user_created",
    "public_user_active_changed",
    "public_user_password_reset",
    "public_user_renamed",
    "public_sessions_revoked",
    "public_admin_transferred",
    "public_user_deleted",
}
print(json.dumps({"ok": True, "events": sorted(events & expected_events), "expected": sorted(expected_events)}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["ok"] is True
    assert result["events"] == result["expected"]
