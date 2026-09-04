# penczREQ 0.5.2 — instrukcja użytkownika i administratora

[Polski](INSTRUKCJA-PL.md) | [English](INSTRUKCJA-EN.md)

## Do czego służy aplikacja

penczREQ jest samodzielnie hostowanym, ręcznie obsługiwanym systemem requestów
multimedialnych dla Jellyfin. Użytkownicy wyszukują filmy i seriale w TMDB,
zgłaszają zainteresowanie i śledzą realizację. Aplikacja nie pobiera plików, nie
czyta biblioteki Jellyfin i nie integruje się z Sonarr/Radarr.

System składa się z dwóch usług:

- **Public** (`8000`) — interfejs użytkowników i zwykła administracja requestami;
- **Control** (`8001`) — prywatny panel bezpieczeństwa i konfiguracji.

Control ma osobne konto, sesje i bazę. Nie należy udostępniać go w Internecie.

## Uruchomienie Windows DEV/UAT

Wymagane są Python 3.12 i Node.js. Po instalacji zależności uruchom w dwóch
oknach PowerShell:

```powershell
.\start-dev.ps1
```

```powershell
.\start-control-dev.ps1
```

Adresy:

- Public: <http://127.0.0.1:8000>
- Control: <http://127.0.0.1:8001>

Przy pierwszym uruchomieniu Control zapisuje jednorazowe dane logowania w
`dev-data/control/CONTROL-FIRST-LOGIN.txt`. Po obowiązkowej zmianie hasła plik
jest automatycznie usuwany. Nie kopiuj jego treści do repozytorium, obrazu ani
wiadomości.

## Konta, role i sesje

Uprawnienia wynikają wyłącznie z roli zapisanej w bazie — nazwa użytkownika nie
nadaje praw administratora. Control pozwala tworzyć, wyłączać i przywracać konta,
zmieniać role oraz przenosić rolę administratora. Operacje bezpieczeństwa
unieważniają odpowiednie sesje.

Hasło ma 15–128 znaków ASCII i zawiera małą literę, wielką literę oraz cyfrę.
Public i Control mają oddzielne cookies oraz sekrety sesji. Wrażliwe operacje
Control wymagają ponownego podania aktualnego hasła.

Limiter logowania nie ujawnia, czy konto istnieje. Dziesięć błędnych prób z
jednego adresu w ciągu 10 minut blokuje adres na 15 minut, a kolejne cykle w
ciągu 24 godzin wydłużają blokadę maksymalnie do 24 godzin. IPv6 jest grupowane
według prefiksu `/64`. Blokady można przeglądać i usuwać w Control.

## Język polski i angielski

Każdy użytkownik wybiera język PL/EN niezależnie. Wybór obejmuje Public,
powiadomienia, komunikaty błędów i treści generowane przez system. Control także
ma pełny interfejs PL/EN. Historyczne powiadomienia rozpoznawalnych typów są
normalizowane do bieżących szablonów bez zmiany własnych wiadomości
administratora.

Karty requestów i wyszukiwarka TMDB używają jako głównego tytułu lokalizacji
zgodnej z wybranym językiem. Tytuł oryginalny pojawia się pod nim tylko wtedy,
gdy jest znacząco różny. Istniejące rekordy, które nie mają jeszcze zapisanej
lokalizacji angielskiej, bezpiecznie korzystają z tytułu oryginalnego; samo
renderowanie listy nie wykonuje zapytań do TMDB w tle.

## Requesty i udział użytkownika

Public ma trzy główne karty: `Requesty`, `Przed premierą` i `Zrealizowane`.
Każda ma server-side sortowanie i paginator 25/50/100, również dla pustej listy.
Filtr statusu dotyczy Requestów. Serial może prezentować zgrupowane sezony.

Użytkownik może dodać własny request albo dołączyć do istniejącego. Akcja
`Wycofaj mój request` ma dwa rezultaty:

- jeżeli użytkownik jest jedyną aktywnie zainteresowaną osobą, request zostaje
  usunięty;
- jeżeli inni użytkownicy nadal są zainteresowani, request pozostaje, a
  wycofujący użytkownik usuwa tylko własny udział i nie otrzymuje dalszych
  powiadomień dotyczących tego requestu.

Adminowe `Przywróć do requestów` jest odrębną operacją: cofa omyłkowe oznaczenie
pozycji jako zrealizowanej. Nie jest odwróceniem wycofania udziału użytkownika.

## TMDB API Read Access Token

Wymagany jest **TMDB API Read Access Token** używany jako Bearer token — nie
stary v3 API Key. Token zapisuje się i testuje w Control. Jest szyfrowany AES-GCM
w bazie ustawień, nie wraca w odpowiedzi API ani historii zmian i nie trzeba go
duplikować dla Public. Public odczytuje aktualną wspólną konfigurację przy
wyszukiwaniu filmów, seriali i szczegółów/sezonów.

## Tryby LAN i reverse proxy

Public i Control niezależnie obsługują `lan` oraz `reverse-proxy`, więc poprawne
są cztery kombinacje:

| Public | Control | Zastosowanie |
| --- | --- | --- |
| LAN | LAN | bezpośredni prywatny dostęp HTTP |
| reverse proxy | LAN | Public HTTPS, Control bezpośrednio w LAN |
| LAN | reverse proxy | Public HTTP, prywatny Control przez HTTPS proxy |
| reverse proxy | reverse proxy | oba przez proxy, Control nadal tylko dla LAN/VPN |

LAN wymaga URL HTTP i `COOKIE_SECURE=false`. Reverse proxy wymaga HTTPS oraz
`COOKIE_SECURE=true`. Na TrueNAS właściwa usługa przy każdym starcie wyznacza
bieżący, bezpośrednio połączony prywatny gateway kontenera i ufa tylko jego
`/32`; ręczne wpisy pozostają dodatkowe. Odtworzenie sieci wymaga więc restartu
usługi, a nie ręcznej korekty gateway. Ręczna zmiana wykonana w Control zaczyna
obowiązywać po restarcie właściwej usługi. Niejednoznaczne dane tras, wildcard i
`/0` kończą się fail-closed. Tryb reverse proxy Control nie oznacza ekspozycji do
Internetu — końcowy klient nadal musi należeć do prywatnej listy LAN/VPN.

## PWA i Web Push

Public można zainstalować jako PWA/WebAPK z sekcji konta lub menu obsługiwanej
przeglądarki. Android Chrome został empirycznie zweryfikowany na fizycznym
urządzeniu: otwarcie strony, instalacja PWA/WebAPK, uruchomienie zainstalowanej
aplikacji, Web Push i powiadomienia systemowe zakończyły się powodzeniem.

Google Chrome jest zalecaną przeglądarką do instalowania penczREQ jako PWA na Androidzie.

Samsung Internet również został empirycznie zweryfikowany: instalacja,
uruchomienie i Web Push działały, dlatego pozostaje wspierany, ale nie jest
preferowaną metodą instalacji. Pakiet generowany przez przeglądarkę może podczas
instalacji wyświetlić ostrzeżenie Androida o aplikacji przeznaczonej dla starszej
wersji systemu, a ikony TMDB/IMDb mogą wyglądać nieco jaśniej. Komunikat
wrappera/platformy pozostaje poza manifestem i service workerem penczREQ; w UAT
instalacja po wybraniu kontynuacji zakończyła się prawidłowo. Na iOS/iPadOS
16.4+ Web Push jest dostępny dla zainstalowanej aplikacji z ekranu początkowego,
jeżeli platforma i przeglądarka spełniają wymagania; ten wariant
nadal nie został empirycznie zweryfikowany dla penczREQ.

Instalacja i Web Push wymagają bezpiecznego kontekstu HTTPS, poza wyjątkiem
`localhost`. Zwykły HTTP w LAN nadal obsługuje aplikację, lecz nie gwarantuje
PWA/Push na innym urządzeniu. PWA pozostaje klientem sieciowym: nie cache'uje
prywatnych widoków, API, plakatów ani danych konta do pracy offline. Aktualizacja
service workera wymaga kontrolowanego odświeżenia zaakceptowanego przez
użytkownika. penczREQ nie używa TWA, osobnego APK ani wrappera.

Źródła wsparcia platform: [WebKit — Web Push dla aplikacji ekranu początkowego
iOS/iPadOS 16.4+](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
oraz [Samsung Internet — PWA i Web Push](https://developer.samsung.com/internet/android/web-developer-guide.html).

## Dane, kopie i odzyskiwanie

W DEV główna baza znajduje się w `dev-data/app.db`, a baza Control w
`dev-data/control/control.db`. Plakaty są w `dev-data/posters/`, logi JSONL w
`dev-data/logs/`, a kopie w `dev-data/backups/`.

Control tworzy transakcyjną parę kopii obu baz i sprawdza każdą przez SQLite
`quick_check`. Przy odzyskiwaniu operacji obejmujących konta, ustawienia lub
bezpieczeństwo należy przywrócić obie bazy z tego samego zestawu — nie jedną.
Retencję kopii i logów ustawia się w Control. Sekrety, hasła i klucze są
maskowane w dzienniku.

## TrueNAS, aktualizacja i rollback

Fresh install jest podstawową ścieżką dla nowych użytkowników wersji 0.5.2.
Konfigurator TrueNAS działa domyślnie jako dry-run; mutacje wymagają jawnego
`--execute` uruchomionego na NAS-ie oraz zatwierdzenia konkretnej zmiany przez
operatora. Walidator technicznie dopuszcza gałąź TrueNAS SCALE 25.10.x, natomiast
TrueNAS SCALE 25.10.6 jest zweryfikowanym empirycznie targetem.

Upgrade `0.4.3 -> 0.5.2` pozostaje ścieżką migracji istniejącej instalacji
legacy. Nie jest wymagany dla nowego użytkownika rozpoczynającego od 0.5.2.
Szczegółowa instrukcja jest w
[`../deploy/truenas/INSTALL.md`](../deploy/truenas/INSTALL.md).

Prosta aktualizacja obrazu jest dopuszczalna tylko bez zmian Compose, portów,
mountów, sekretów i kontraktu baz. Zmiana któregokolwiek z tych elementów wymaga
wersjonowanego migratora, kontroli obu baz, ich spójnej kopii, snapshotu ZFS i
zapisanego planu rollbacku. Numerowany tag obrazu jest kotwicą rollbacku;
`:stable` wskazuje ostatnie zatwierdzone wydanie. Szczegóły opisuje
[`UPDATE.md`](UPDATE.md).

## Testy i diagnostyka

Pełny lokalny zestaw uruchamia:

```powershell
.\security-test.ps1
```

Obejmuje regresję aplikacji, test runtime obu usług, CSRF, reautoryzację,
sekrety, kopie i integralność. Przygotowany workflow CI dodatkowo sprawdza
składnię Python/JavaScript/shell, zależności, sekrety, prywatne identyfikatory i
konfigurację. Na runnerze z silnikiem kontenerowym ma również zbudować obraz,
utworzyć SPDX SBOM, wykonać inspekcję i skan obrazu. Operacje te zostały
empirycznie wykonane lokalnie na Windows 11 z Docker Desktop dla zamrożonego
baseline kontenerowego, wraz z utwardzonym smoke Public/Control i niezależnym rebuildem.
Osobna walidacja TrueNAS SCALE 25.10.6 objęła fresh install, upgrade, rollback
oraz zachowanie datasetów ZFS. Czerwonych wyników nie wolno maskować jako
udanego wydania.

## Stałe granice bezpieczeństwa

- Control nie jest publicznym panelem internetowym.
- Kontenery nie dostają socketa Docker, API TrueNAS, danych Jellyfin ani Caddy.
- Tokenów, haseł, kluczy i danych startowych nie zapisuje się w Compose ani Git.
- Instalator nie zmienia Caddy, Jellyfin ani routera.
- Publikacja GitHub/GHCR nie jest automatyczna, projekt jest licencjonowany jako
  AGPL-3.0-only, a zmiana produkcyjna wymaga jawnego zatwierdzenia operatora.

Ten produkt korzysta z API TMDB, ale nie jest wspierany ani certyfikowany przez
TMDB.
