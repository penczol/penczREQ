from request_app.security import normalize_username, validate_password, validate_username


def test_username_rules():
    assert normalize_username("  Anna.Nowak ") == "anna.nowak"
    assert validate_username("anna-nowak") is None
    assert validate_username("za krótki") is not None


def test_password_policy_v02():
    assert validate_password("DlugieHaslo2026") is None
    assert validate_password("Krotkie1A") is not None
    assert validate_password("same-malelitery-2026") is not None
    assert validate_password("SAME-WIELKIE-2026") is not None
    assert validate_password("BezCyfryHaslo") is not None
    assert validate_password("PolskieHasło2026") is not None
    assert validate_password("Specjalne!Haslo2026") is None
