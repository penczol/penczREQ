from pathlib import Path

from request_app.database import Database, utc_now
from request_app.repository import Repository


ROOT = Path(__file__).resolve().parents[1]


def test_lightweight_notification_counts_query(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        user_id = int(
            connection.execute(
                """
                INSERT INTO users
                (username, password_hash, role, is_active, must_change_password, created_at)
                VALUES ('jan', 'test', 'user', 1, 0, ?)
                """,
                (utc_now(),),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, created_at)
            VALUES (?, 'test', 'Nowe', 'Treść', ?)
            """,
            (user_id, utc_now()),
        )
        connection.execute(
            """
            INSERT INTO notifications
            (user_id, type, title, body, created_at, read_at)
            VALUES (?, 'test', 'Stare', 'Treść', ?, ?)
            """,
            (user_id, utc_now(), utc_now()),
        )
    assert repository.notification_counts(user_id) == {"unread": 1, "read": 1}


def test_notification_badge_sync_is_lightweight_visibility_aware_and_frequent():
    common = (ROOT / "request_app" / "static" / "common.js").read_text(encoding="utf-8")
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/notifications/counts")' in main
    assert 'api("/api/notifications/counts")' in common
    assert "const NOTIFICATION_SYNC_INTERVAL_MS = 15000;" in common
    assert "if (!notificationList || document.hidden)" in common
    assert 'document.addEventListener("visibilitychange"' in common
    assert 'window.addEventListener("focus"' in common


def test_request_sync_preserves_view_and_only_renders_changed_payloads():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const REQUEST_SYNC_INTERVAL_MS = 30000;" in script
    assert "let stateFingerprint = \"\";" in script
    assert "function requestSyncBlocked()" in script
    assert "async function loadAll({ silent = false } = {})" in script
    assert "const changed = fingerprint !== stateFingerprint;" in script
    assert "if (changed || !silent) render();" in script
    assert "loadAll({ silent: true })" in script
    assert "window.location.reload" not in script


def test_request_sync_defers_during_user_and_admin_interactions():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    blocked_function = script[
        script.index("function requestSyncBlocked"):
        script.index("async function loadAll")
    ]
    assert 'document.querySelector("dialog[open]")' in blocked_function
    assert '".inline-confirm, .confirm-status:not([hidden]), details[open]"' in blocked_function
    assert '"button:disabled:not(.locked)"' in blocked_function
    assert "list.contains(focused)" in blocked_function


def test_sync_keeps_existing_tab_filter_sort_and_season_state_variables():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'let currentState = "active";' in script
    assert "const activeSeasonByGroup = new Map();" in script
    assert 'statusFilter.addEventListener("change", async () =>' in script
    assert "sortSelect.addEventListener(\"change\", async () =>" in script
    assert "const pageByState = { active: 1, upcoming: 1, completed: 1 };" in script
    assert "const paginationByState" in script
    load_function = script[
        script.index("async function loadAll"):
        script.index("document.querySelectorAll(\"[data-state]\")")
    ]
    assert 'currentState = "active"' not in load_function
    assert "activeSeasonByGroup.clear" not in load_function


def test_all_three_request_tabs_use_server_side_pagination():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    template = (ROOT / "request_app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'data-request-pagination' in template
    assert 'data-completed-pagination' not in template
    assert "page: String(pageByState[state])" in script
    assert 'if (state === "active") query.set("status_filter", statusFilter.value);' in script
    assert "pageByState[currentState] +=" in script
    endpoint = main[main.index('@app.get("/api/requests")'):main.index('@app.post("/api/requests")')]
    assert 'if state == "completed"' not in endpoint
    assert "return repo.paginated_requests(" in endpoint


def test_request_payloads_are_validated_before_pagination_state_is_committed():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function normalizeRequestResult(state, result)" in script
    assert 'const pagination = result.pagination;' in script
    assert 'const fields = ["page", "page_size", "total_items", "total_all_items", "total_pages"];' in script
    assert "const normalizedResults = Object.keys(states).map" in script
    assert "paginationByState[state] = normalizedResults[index].pagination;" in script
    assert "results[index].pagination" not in script
    assert 'console.error(tr("Nie udało się wczytać list requestów."), error);' in script


def test_paginator_is_always_visible_for_empty_single_and_multiple_pages():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "requestPagination.hidden = false;" in script
    assert "requestPagination.hidden = pagination.total_pages <= 1;" not in script
    assert 'pageSummaryDesktop.textContent = tr("Strona {page} z {pages} · {count} pozycji"' in script
    assert "pageSummaryMobile.textContent = `${pagination.page}/${pagination.total_pages} · ${pagination.total_items}`;" in script
    assert 'pagination.page <= 1' in script
    assert 'pagination.page >= pagination.total_pages' in script


def test_mobile_paginator_uses_compact_single_row_without_changing_desktop_copy():
    template = (ROOT / "request_app" / "templates" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "request_app" / "static" / "app.css").read_text(encoding="utf-8")
    mobile_styles = styles[styles.index("@media (max-width: 680px)"):styles.index("@media (max-width: 430px)")]

    selector_position = template.index('class="pagination-page-size"')
    previous_position = template.index('class="button pagination-direction pagination-previous"')
    summary_position = template.index('class="pagination-summary"')
    next_position = template.index('class="button pagination-direction pagination-next"')
    assert selector_position < previous_position < summary_position < next_position
    assert '<span class="pagination-page-size-label">{{ t("Na stronie") }}</span>' in template
    assert '<span class="pagination-direction-desktop">{{ t("Poprzednia") }}</span>' in template
    assert '<span class="pagination-direction-desktop">{{ t("Następna") }}</span>' in template
    assert '<span class="pagination-direction-mobile" aria-hidden="true">‹</span>' in template
    assert '<span class="pagination-direction-mobile" aria-hidden="true">›</span>' in template
    assert 'data-page-summary-desktop>{{ t("Strona 1 z 1 · 0 pozycji") }}</span>' in template
    assert 'data-page-summary-mobile>1/1 · 0</span>' in template

    desktop_rule = ".request-pagination { display: flex; gap: 10px; align-items: center; justify-content: flex-end; flex-wrap: wrap; margin: 12px 0; }"
    assert desktop_rule in styles[:styles.index("@media (max-width: 680px)")]
    assert "grid-template-columns: 68px minmax(0, 1fr) 42px 42px;" in mobile_styles
    assert ".request-pagination [data-page-size] { width: 68px; min-height: 42px; padding: 7px 8px 7px 9px; font-size: 14px; text-align: center; }" in mobile_styles
    assert "overflow-x: visible;" in mobile_styles
    assert ".request-pagination .pagination-previous { order: 3; }" in mobile_styles
    assert ".request-pagination .pagination-next { order: 4; }" in mobile_styles
    assert "white-space: nowrap;" in mobile_styles
    assert "min-height: 42px;" in mobile_styles
    assert "overflow-x: auto" not in mobile_styles
    assert "overflow-x: scroll" not in mobile_styles
