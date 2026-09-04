from __future__ import annotations

from typing import Any

from .i18n import normalize_language, translate


CHANGELOG: tuple[dict[str, Any], ...] = (
    {
        "version": "0.5.2",
        "date": "02.09.2026",
        "public": (
            "Zaktualizowano aplikację do wersji 0.5.2 bez zmian schematu bazy danych.",
        ),
        "admin": (
            "Reverse proxy automatycznie rozpoznaje przy każdym starcie bieżący prywatny gateway kontenera i ufa wyłącznie jego adresowi /32.",
            "Ręczne wpisy trusted proxy pozostają oddzielne, a zmiany zaczynają obowiązywać po restarcie właściwej usługi.",
        ),
    },
    {
        "version": "0.5.1",
        "date": "30.08.2026",
        "public": (
            "Zaktualizowano aplikację do wersji 0.5.1 bez zmian schematu bazy danych.",
        ),
        "admin": (
            "Instalator TrueNAS wymusza teraz ownership root:root i tryb 0600 dla plików public.env oraz control.env podczas fresh install i upgrade.",
        ),
    },
    {
        "version": "0.5.0",
        "date": "22.08.2026",
        "public": (
            "Dodano pełny interfejs angielski i polski z językiem zapisywanym osobno dla każdego konta.",
            "W widoku angielskim tytuł oryginalny jest główny, a polskie daty premier pozostają ukryte.",
            "Rozszerzono paginację 25/50/100 na wszystkie trzy główne karty i dodano zwarty paginator mobilny.",
            "Dodano bezpieczne wycofanie własnego requestu oraz responsywny układ akcji kart.",
            "Wydłużono sesje użytkowników i panelu Control zgodnie z nową polityką prywatnego wdrożenia.",
        ),
        "admin": (
            "Dodano administratorowi możliwość przywrócenia omyłkowo zrealizowanej pozycji do aktywnych requestów.",
            "Nowe powiadomienia, komunikaty Control i Web Push są generowane w języku odbiorcy.",
        ),
    },
    {
        "version": "0.4.3",
        "date": "23.07.2026",
        "public": (
            "Przygotowano bezpieczne, powtarzalne wydanie kontenerowe dla TrueNAS.",
        ),
        "admin": (
            "Rozdzielono dane penczREQ Control od procesu publicznego i przeniesiono wykonywanie kopii obu baz do usługi Control.",
            "Dodano bezpieczne rozpoznawanie adresu klienta panelu Control za jawnie zaufanym reverse proxy.",
            "Dodano utwardzenie kontenerów, trwałe datasety, healthchecki oraz ścieżkę przyszłych aktualizacji z rejestru obrazów.",
        ),
    },
    {
        "version": "0.4.2",
        "date": "23.07.2026",
        "public": (
            "Wzmocniono ochronę logowania, sesji oraz danych kont bez wprowadzania permanentnych automatycznych blokad użytkowników.",
            "Okładki są dostępne wyłącznie po zalogowaniu, a publiczne endpointy diagnostyczne zostały zamknięte.",
            "Zwiększono minimalną długość nowych haseł do 15 znaków i dodano rotację sesji po ich zmianie.",
            "Istniejące konta jednorazowo ustawią hasło zgodne z nową polityką 15 znaków.",
        ),
        "admin": (
            "Usunięto zależność roli administratora od konkretnej nazwy konta; publiczny administrator jest teraz konfigurowalną rolą.",
            "Przeniesiono konta, blokady, klucze API, ustawienia proxy, logi i kopie do oddzielnego lokalnego panelu penczREQ Control.",
            "Dodano szyfrowanie klucza TMDB, audyt JSONL, retencję, kontrolę integralności oraz automatyczne kopie SQLite.",
        ),
    },
    {
        "version": "0.4.1",
        "date": "23.07.2026",
        "public": (),
        "admin": (
            "Dodano przełącznik diagnostyczny pomiędzy widokiem administratora i zwykłego użytkownika bez przelogowywania.",
            "W trybie użytkownika ukrywane są dane i akcje administracyjne, a lista requestów korzysta z tych samych ograniczeń prywatności co konto użytkownika.",
            "W domyślnym widoku administratora usunięto przycisk dodawania requestu; pozostaje dostępny w trybie diagnostycznym użytkownika.",
        ),
    },
    {
        "version": "0.4.0",
        "date": "23.07.2026",
        "public": (
            "Dodano cichą synchronizację list requestów bez przeładowywania całej strony.",
            "Aktywna karta, sortowanie, filtry, wybrany sezon i pozycja strony pozostają zachowane podczas aktualizacji.",
            "Licznik powiadomień aktualizuje się automatycznie co 15 sekund, a listy requestów co 30 sekund.",
            "Po powrocie do przeglądarki lub PWA dane są synchronizowane natychmiast.",
        ),
        "admin": (
            "Automatyczna podmiana listy jest wstrzymywana podczas edycji statusu, potwierdzania operacji i pracy w otwartym oknie dialogowym.",
        ),
    },
    {
        "version": "0.3.13",
        "date": "23.07.2026",
        "public": (
            "Seriale zakończone pokazują w fioletowej etykiecie zakres lat premiery i zakończenia, np. 1993-2018.",
            "Seriale trwające pokazują oznaczenie „rok-trwa”, a przy braku danych „rok-????”.",
            "Dane cyklu serialu są pobierane z TMDB i okresowo aktualizowane również w dziale Gotowe.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.12",
        "date": "23.07.2026",
        "public": (
            "Pasek wyboru sezonów jest teraz wyświetlany przy każdym serialu, także gdy zamówiono tylko jeden sezon.",
            "Usunięto numer sezonu z fioletowej etykiety nad tytułem serialu; numer pozostaje wyłącznie na zakładce sezonu.",
            "Ujednolicono zachowanie w Requestach, Przed premierą i Gotowych na desktopie oraz telefonie.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.11",
        "date": "23.07.2026",
        "public": (
            "Przeniesiono przełącznik sezonów do osobnej, pełnoszerokościowej ramki nad kartą serialu.",
            "Przywrócono wcześniejsze proporcje plakatu i układ karty głównej dla administratora oraz użytkowników.",
            "Poprawiono szerokość i poziome przewijanie przełącznika sezonów na telefonach.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.10",
        "date": "23.07.2026",
        "public": (
            "Zgrupowano zamówione sezony tego samego serialu w jedną kartę z przełącznikiem sezonów.",
            "Każdy sezon zachowuje własne daty, status, lajki i akcje oraz trafia niezależnie do właściwego działu.",
            "Na desktopie pasek pokazuje pełne nazwy sezonów, a na telefonie zwarte oznaczenia S01, S02, S03…",
        ),
        "admin": (),
    },
    {
        "version": "0.3.9",
        "date": "23.07.2026",
        "public": (
            "Dodano możliwość zaznaczenia i dodania wielu sezonów serialu w jednej operacji.",
            "Dostosowano wielokrotny wybór sezonów osobno do desktopu i urządzeń mobilnych.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.8",
        "date": "23.07.2026",
        "public": (
            "Dodano faviconę i ikonę aplikacji „Pr”.",
            "Dodano możliwość instalacji penczREQ jako PWA na Androidzie i w obsługiwanych przeglądarkach desktopowych.",
            "Zintegrowano powiadomienia wewnętrzne z systemowymi powiadomieniami Androida i przeglądarki.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.7",
        "date": "23.07.2026",
        "public": (),
        "admin": (
            "Umieszczono wszystkie akcje administratora w jednym wierszu na telefonie i skrócono mobilną etykietę zielonego przycisku do „Wypełniony”.",
        ),
    },
    {
        "version": "0.3.6",
        "date": "23.07.2026",
        "public": (
            "Dodano powiadomienie wewnętrzne po umieszczeniu requestu w karcie „Przed premierą”.",
            "Ujednolicono nazwę serwisu.",
            "Umieszczono nazwę zalogowanego użytkownika pod przyciskiem „Moje konto” w widoku desktopowym.",
        ),
        "admin": (
            "Maksymalnie skompresowano mobilny pasek akcji requestu w widoku administratora.",
        ),
    },
    {
        "version": "0.3.5",
        "date": "23.07.2026",
        "public": (
            "Dodano nazwę zalogowanego użytkownika obok przycisku „Moje konto”.",
            "Dodano jednoznaczny komunikat po zaklasyfikowaniu requestu jako pozycji przed premierą.",
            "Dodano historię zmian otwieraną po kliknięciu numeru wersji.",
            "Dodano jednorazowe powiadomienia o każdej aktualizacji serwera.",
            "Naprawiono usuwanie wszystkich odczytanych powiadomień.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.4",
        "date": "23.07.2026",
        "public": (
            "W desktopowym widoku użytkownika zakotwiczono datę dodania i premiery tuż nad linkami TMDB/IMDb.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.3",
        "date": "23.07.2026",
        "public": (
            "Wyrównano dolną krawędź desktopowej karty użytkownika z dolną krawędzią okładki.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.2",
        "date": "23.07.2026",
        "public": (
            "Dodatkowo skompresowano pionowo karty w widoku desktopowym.",
        ),
        "admin": (),
    },
    {
        "version": "0.3.1",
        "date": "23.07.2026",
        "public": (
            "Przywrócono użytkownikom odnośnik do IMDb bez ujawniania ani kopiowania IMDb ID.",
        ),
        "admin": (),
    },
    {
        "version": "0.3",
        "date": "23.07.2026",
        "public": (
            "Zastąpiono tekstowe linki TMDB i IMDb kompaktowymi ikonami.",
            "Przebudowano i skompresowano desktopowy układ kart.",
        ),
        "admin": (
            "Dodano kopiowanie TMDB ID oraz IMDb ID.",
            "Usunięto tekstowe wyświetlanie IMDb ID.",
            "Przebudowano obsługę wyboru i zatwierdzania statusu.",
        ),
    },
    {
        "version": "0.2",
        "date": "23.07.2026",
        "public": (
            "Rozszerzono wyszukiwarkę o kraj, reżysera i główną obsadę.",
            "Dodano światowe i polskie informacje o premierach.",
            "Rozbudowano powiadomienia, zasady lajków i zabezpieczenia logowania.",
            "Dodano osobne listy powiadomień odczytanych i nieodczytanych.",
        ),
        "admin": (
            "Dodano zarządzanie blokadami kont i adresami known proxy.",
            "Dodano ostatnie adresy IP logowania oraz wiadomości administratora do wszystkich użytkowników.",
            "Dodano potwierdzanie zmian statusu i realizacji requestu.",
        ),
    },
    {
        "version": "0.1",
        "date": "22.07.2026",
        "public": (
            "Uruchomiono pierwszą działającą wersję prywatnego systemu requestów.",
            "Dodano wyszukiwanie TMDB, requesty filmów i sezonów, lajki oraz trzy główne karty.",
            "Dodano konta użytkowników, zmianę hasła i powiadomienia wewnętrzne.",
        ),
        "admin": (
            "Dodano zarządzanie użytkownikami, statusami, realizacją i trwałym usuwaniem requestów.",
        ),
    },
)


def _visible_changes(entry: dict[str, Any], is_admin: bool) -> list[str]:
    changes = list(entry["public"])
    admin_changes = list(entry["admin"])
    if admin_changes:
        changes.extend(admin_changes if is_admin else ["Inne poprawki administracyjne."])
    return changes


def changelog_for(is_admin: bool, language: str = "pl") -> list[dict[str, Any]]:
    normalized = normalize_language(language)
    return [
        {
            "version": entry["version"],
            "date": entry["date"],
            "changes": [translate(change, normalized) for change in _visible_changes(entry, is_admin)],
        }
        for entry in CHANGELOG
    ]


def update_notification_bodies(version: str, language: str = "pl") -> tuple[str, str]:
    normalized = normalize_language(language)
    entry = next((item for item in CHANGELOG if item["version"] == version), None)
    if entry is None:
        fallback = translate(
            "Wdrożono wersję {version}.\nPełna historia zmian jest dostępna po kliknięciu numeru wersji.",
            normalized,
            version=version,
        )
        return fallback, fallback

    def build(is_admin: bool) -> str:
        bullets = "\n".join(
            f"• {translate(change, normalized)}" for change in _visible_changes(entry, is_admin)
        )
        heading = translate("Wdrożono wersję {version}.", normalized, version=version)
        footer = translate(
            "Pełna historia zmian jest dostępna po kliknięciu numeru wersji.", normalized
        )
        return f"{heading}\n{bullets}\n{footer}"

    return build(False), build(True)
