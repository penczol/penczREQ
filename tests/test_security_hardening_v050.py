from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def isolated_login_probe(tmp_path: Path, component: str) -> dict[str, object]:
    password = "Validation" + "LoginPass123!"  # pragma: allowlist secret
    root = tmp_path / component
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "APP_COMPONENT": "combined" if component == "public" else "control",
            "DATA_DIR": str(root / "data"),
            "CONTROL_DATA_DIR": str(root / "control"),
            "BACKUP_DIR": str(root / "backups"),
            "LOG_DIR": str(root / "logs"),
            "SESSION_SECRET": "S" * 48,
            "CONTROL_SESSION_SECRET": "C" * 48,
            "CONFIG_ENCRYPTION_KEY": "E" * 48,
            "COOKIE_SECURE": "false",
            "PUBLIC_ADMIN_USERNAME": "validation-admin",
            "PUBLIC_ADMIN_BOOTSTRAP_PASSWORD": password,
            "CONTROL_ADMIN_USERNAME": "validation-control",
            "CONTROL_BOOTSTRAP_PASSWORD": password,
            "CONTROL_ALLOWED_NETWORKS": "127.0.0.0/8",
            "CONTROL_ALLOWED_HOSTS": "testserver",
            "SECURITY_TEST_COMPONENT": component,
            "SECURITY_TEST_PASSWORD": password,
        }
    )
    script = r'''
import html
import json
import os
import re
from fastapi.testclient import TestClient

component = os.environ["SECURITY_TEST_COMPONENT"]
password = os.environ["SECURITY_TEST_PASSWORD"]
if component == "public":
    from request_app import main as target
    client = TestClient(target.app, follow_redirects=False)
    username = "validation-admin"
    session_cookie = target.COOKIE_NAME
else:
    from request_app import control as target
    client = TestClient(
        target.app,
        follow_redirects=False,
        client=("127.0.0.1", 50000),
    )
    username = "validation-control"
    session_cookie = target.settings.control_cookie_name

with client:
    page = client.get("/login")
    matched = re.search(
        r'name="login_csrf_token" value="([^"]+)"',
        page.text,
    )
    token = html.unescape(matched.group(1)) if matched else ""
    cookie_token = client.cookies.get(target.LOGIN_CSRF_COOKIE)
    missing = client.post(
        "/login",
        data={"username": username, "password": password},
    )
    mismatch = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "login_csrf_token": "mismatched-token",
        },
    )
    success = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "login_csrf_token": token,
        },
    )
    print(json.dumps({
        "page_status": page.status_code,
        "token_matches_cookie": bool(token) and token == cookie_token,
        "cookie_header": page.headers.get("set-cookie", ""),
        "missing_status": missing.status_code,
        "mismatch_status": mismatch.status_code,
        "success_status": success.status_code,
        "session_cookie_set": session_cookie in client.cookies,
        "login_cookie_cleared": target.LOGIN_CSRF_COOKIE not in client.cookies,
    }))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("component", ("public", "control"))
def test_login_requires_matching_short_lived_pre_auth_csrf(
    tmp_path: Path, component: str
):
    result = isolated_login_probe(tmp_path, component)

    assert result["page_status"] == 200
    assert result["token_matches_cookie"] is True
    assert "HttpOnly" in result["cookie_header"]
    assert "Max-Age=600" in result["cookie_header"]
    assert "Path=/login" in result["cookie_header"]
    assert "SameSite=strict" in result["cookie_header"]
    assert result["missing_status"] == 403
    assert result["mismatch_status"] == 403
    assert result["success_status"] == 303
    assert result["session_cookie_set"] is True
    assert result["login_cookie_cleared"] is True
