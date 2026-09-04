# Specyfikacja wersji 0.1

## Role i prywatność

- Brak publicznej rejestracji.
- Uprawnienia administratora wynikają wyłącznie z roli w bazie; neutralny login startowy to `admin`.
- Użytkownicy nie widzą autorów requestów ani osób, które je polubiły.
- Administrator widzi autora i listę zainteresowanych.
- Uprawnienia są sprawdzane po stronie serwera.

## Widoki

1. **Requesty** — aktywne pozycje.
2. **Przed premierą** — pozycje z przyszłym `release_date`; dla sezonu jest to data pierwszego odcinka.
3. **Zrealizowane** — pozycje oznaczone przez administratora jako dostępne.

## Aktywne statusy

- `Oczekujący` (domyślny),
- `W oczekiwaniu na tłumaczenie`,
- `W trakcie realizacji`,
- `Aktualnie brak źródła`.

## Request

- Wyszukiwanie TMDB po tytule polskim lub oryginalnym.
- Plakat TMDB preferujący język oryginalny.
- Polski i oryginalny tytuł, rok, film/serial, sezon, IMDb ID oraz data dodania.
- Linki TMDB i IMDb oraz kopiowanie czystego IMDb ID.
- Autor automatycznie daje pierwszy like.
- Jeden request na film albo konkretny sezon; kolejne osoby dołączają like.
- Sortowanie po dacie, liczbie like’ów i statusie; filtrowanie po statusie.

## Administracja

- Zmiana statusu.
- Przeniesienie do zrealizowanych i możliwość cofnięcia.
- Trwałe usunięcie aktywnego requestu wymaga podania powodu.
- Panel tworzenia, blokowania i resetowania haseł użytkowników.

## Powiadomienia wewnętrzne

- Administrator: nowy request użytkownika.
- Autor: ktoś polubił jego request (bez ujawniania osoby).
- Zainteresowani: pozycja została zrealizowana.
- Zainteresowani: request usunięto wraz z powodem.
- Użytkownik może wyłączyć poszczególne typy powiadomień.

## Hasła

- Minimum 15, maksimum 128 znaków.
- Wymagana jest mała litera, wielka litera i cyfra; dozwolone są wyłącznie znaki ASCII.
- Blokada popularnych haseł, Argon2id, limity prób logowania.
- Zmiana hasła wymaga obecnego hasła i unieważnia pozostałe sesje.
