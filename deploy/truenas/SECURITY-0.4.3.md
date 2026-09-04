# Raport bezpieczeństwa wydania 0.4.3

Data kontroli: 24 lipca 2026 (Europe/Warsaw)

Zakres obejmuje kod penczREQ, konfigurację produkcyjną, obraz
`penczreq:0.4.3`, czystą instalację dwóch usług oraz ponowny start na
zachowanych datasetach. Raport nie jest obietnicą braku przyszłych podatności.
Jest zapisem stanu wydania i warunków jego bezpiecznego wdrożenia.

## Wynik

Wydanie może zostać udostępnione przez Caddy, jeżeli:

- do Internetu trafia wyłącznie publiczna usługa przez Caddy;
- Control pozostaje dostępny tylko z LAN;
- kontenery nie otrzymują datasetów z multimediami, katalogów domowych,
  Docker socketu ani API TrueNAS;
- użyte zostaną unikalne sekrety i silne, różne hasła startowe;
- Caddy, TrueNAS i obraz są regularnie aktualizowane.

## Wykonane kontrole

- 82 testy aplikacji i wdrożenia: zaliczone.
- Test czystej instalacji, bootstrapu, kopii, integralności oraz restartu bez
  haseł startowych: zaliczony.
- Kontrola SQLite `quick_check` obu baz: zaliczona.
- `pip-audit` przypiętych zależności: 44 zależności, 0 znanych podatności.
- Trivy dla pakietów Pythona w obrazie: 0 podatności.
- Trivy secret scan obrazu: 0 sekretów.
- Trivy secret scan paczki źródłowej: 0 sekretów.
- Trivy configuration scan Dockerfile: 27 kontroli zaliczonych, 0 błędów.
- Kontrola historii i środowiska obrazu: brak haseł, tokenów i kluczy.
- Kontrola zawartości obrazu: brak testów, narzędzi, dokumentacji wdrożenia,
  baz, plakatów i `pip`.
- Kontrola kodu: brak uruchamiania shella, `subprocess`, archiwów od
  użytkownika i endpointu wysyłania plików.

## Izolacja

- Public i Control są osobnymi procesami i mają osobne sekrety sesji.
- Public montuje wyłącznie `/data`; nie widzi `control.db` ani kopii.
- Control montuje `/data`, `/control-data` i `/backups`.
- Oba kontenery pracują jako `568:568`, bez roota, bez capabilities, z
  `no-new-privileges`, limitem procesów i systemem plików tylko do odczytu.
- `/tmp` jest osobnym `tmpfs` z `noexec`, `nosuid` i `nodev`.
- Endpointy healthcheck odpowiadają tylko po połączeniu z loopback.
- Plakaty wymagają uwierzytelnionej sesji, a nazwa pliku jest sprawdzana
  przed odczytem.
- Control honoruje `X-Forwarded-For` wyłącznie od jawnie zaufanego proxy,
  po czym ponownie sprawdza rzeczywisty adres klienta względem sieci LAN.

## Pozostałe wpisy systemu bazowego

Trivy zgłasza w Debianie 12.14 191 wpisów systemowych, w tym 6 `CRITICAL`
i 18 `HIGH`. Dla żadnego z 24 wpisów `HIGH/CRITICAL` repozytorium Debiana
nie udostępnia obecnie wersji naprawionej: 18 ma status `affected`, 5
`fix_deferred`, a 1 `will_not_fix`.

Najwięcej wpisów dotyczy `perl-base`; aplikacja nie uruchamia Perla ani
poleceń systemowych. Pozostałe dotyczą między innymi bibliotek SQLite, zlib,
ncurses i util-linux. Nie znaleziono ścieżki, w której niezaufany użytkownik
może przekazać aplikacji bazę SQLite, archiwum albo argument polecenia
systemowego. Jest to ryzyko resztkowe obrazu bazowego, nie wynik w kodzie
penczREQ.

Nie zostało ono ukryte przez listę wyjątków. Raport JSON pozostaje częścią
wydania. Przy każdym kolejnym buildzie należy pobrać aktualny digest oficjalnego
obrazu Pythona, ponowić skan i zastosować poprawki, gdy Debian je opublikuje.
Zmiana na Alpine lub Trixie nie została przyjęta, ponieważ w dniu kontroli nie
dawała mniejszej liczby istotnych znalezisk i zwiększała ryzyko regresji.

## Granica NAS

Najważniejszą ochroną danych NAS jest brak ich montowania. Nawet pełne
przejęcie procesu publicznego nie daje bezpośredniej ścieżki do udziałów z
multimediami, plików Caddy, panelu TrueNAS ani Docker socketu. Atakujący mógłby
naruszyć dane requestów dostępne w `/data`, dlatego potrzebne są snapshoty i
kopie. Nie wolno rozszerzać listy volume bez ponownego audytu.

Port `18000` jest backendem publicznym dla Caddy. Port `18001` i host Control
nie mogą być przekierowane na routerze ani obsługiwane przez publiczny DNS.
Na routerze do Caddy kierowane są tylko `80/tcp` i `443/tcp`.

## Identyfikacja sprawdzonego obrazu

- tag lokalny: `penczreq:0.4.3`
- platforma: `linux/amd64`
- image ID: `sha256:41645507bac67736ac3a25728856238b0bdd24a3fb46d51e4a5c258ee00eadff`
- skompresowany obraz:
  `penczreq-0.4.3-linux-amd64.tar.gz`
- SHA-256 paczki obrazu:
  `40602c3579922d3b3fdb2dbab18ad76801e47407bd10e206d362f13260c82bfc`

Pełne wyniki znajdują się w `dist/trivy-image-report-final.json`,
`dist/trivy-config-report-final.json`, `dist/pip-audit-final.json` oraz
`dist/penczreq-0.4.3-sbom.cdx.json`.
