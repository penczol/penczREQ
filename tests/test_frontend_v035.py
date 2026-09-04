from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_header_uses_new_brand_and_stacks_username_below_account():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    assert "penczREQ" in base
    assert "penczREQ" in (ROOT / "request_app" / "templates" / "login.html").read_text(encoding="utf-8")
    assert "Prywatna lista multimediów" not in base
    assert 'class="account-trigger-wrap"' in base
    account = base[base.index('class="account-trigger-wrap"'):base.index("</div>", base.index('class="account-trigger-wrap"'))]
    assert account.index("data-open-account") < account.index("{{ user.username }}")


def test_version_opens_changelog():
    base = (ROOT / "request_app" / "templates" / "base.html").read_text(encoding="utf-8")
    common = (ROOT / "request_app" / "static" / "common.js").read_text(encoding="utf-8")
    assert "data-open-changelog" in base
    assert "changelog_for(is_admin, language)" in base
    assert 'querySelector("[data-open-changelog]")' in common


def test_delete_all_read_uses_unambiguous_route():
    main = (ROOT / "request_app" / "main.py").read_text(encoding="utf-8")
    common = (ROOT / "request_app" / "static" / "common.js").read_text(encoding="utf-8")
    assert '@app.delete("/api/notifications/read/all")' in main
    assert 'api("/api/notifications/read/all", { method: "DELETE" })' in common


def test_upcoming_request_toast_is_explicit_and_shown_after_refresh():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    message = "Pozycja została sklasyfikowana jako przed premierą"
    assert message in script
    block = script[script.index("async function addRequest"):script.index('searchResults.addEventListener("click"')]
    assert block.index("await loadAll();") < block.index("toast(message")


def test_mobile_action_footer_has_role_specific_user_and_admin_layouts():
    css = (ROOT / "request_app" / "static" / "v035.css").read_text(encoding="utf-8")
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "@media (max-width: 680px)" in css
    assert "body.user-view .request-card-v02 .card-actions" in css
    assert "body.admin-view .request-card-v02 .card-actions" in css
    assert "body.user-view .request-card-v02 .participation-actions" in css
    assert "body.admin-view .request-card-v02 .request-state-actions" in css
    assert ".copy-prefix" in css
    assert 'class="copy-prefix"' in script

def test_mobile_admin_state_actions_use_a_full_width_single_column_section():
    css = (ROOT / "request_app" / "static" / "v035.css").read_text(encoding="utf-8")
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width: 680px)"):]
    actions = mobile[
        mobile.index("body.admin-view .request-card-v02 .request-state-actions {"):
        mobile.index("body.admin-view .request-card-v02 .request-state-actions > .button,")
    ]
    assert "display: grid" in actions
    assert "grid-template-columns: minmax(0, 1fr)" in actions
    assert "width: 100%" in actions
    assert "flex-wrap: nowrap" not in mobile
    assert "white-space: normal" in mobile
    assert "Wycofaj mój request" in script
    assert "Przywróć do requestów" in script
    assert "complete-label-desktop" in css
    assert "complete-label-mobile" in css
    assert script.count("Wypełniony") == 2


def test_mobile_user_footer_is_compact_and_wraps_safely():
    css = (ROOT / "request_app" / "static" / "v035.css").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width: 680px)"):]
    footer = mobile[
        mobile.index("body.user-view .request-card-v02 .card-actions {"):
        mobile.index("body.user-view .request-card-v02 .external-actions {")
    ]
    state = mobile[
        mobile.index("body.user-view .request-card-v02 .request-state-actions {"):
        mobile.index("body.user-view .request-card-v02 .request-state-actions > .button {")
    ]
    assert "display: flex" in footer
    assert "flex-wrap: wrap" in footer
    assert "flex-wrap: nowrap" not in footer
    assert "width: auto" in state
    assert "flex: 0 1 auto" in state
    assert "margin-left: auto" in mobile


def test_action_markup_keeps_participation_separate_from_state_changes():
    script = (ROOT / "request_app" / "static" / "app.js").read_text(encoding="utf-8")
    action_markup = script[script.index("function actionMarkup"):script.index("function releasePeriodLabel")]
    assert 'class="participation-actions"' in action_markup
    assert 'class="request-state-actions"' in action_markup
    assert action_markup.index('class="external-actions"') < action_markup.index('class="participation-actions"')
    assert action_markup.index('class="participation-actions"') < action_markup.index('class="request-state-actions"')
