# Instalacja penczREQ 0.5.2 na TrueNAS SCALE

Ten dokument opisuje przygotowany instalator wydania `0.5.2`. Przykładowy
właściciel obrazu `<owner>` pozostaje placeholderem do czasu skonfigurowania
zatwierdzonego repozytorium obrazu. Produkcyjne wykonanie wymaga jawnej zgody
operatora na konkretną operację, świeżych kopii, snapshotu i zaakceptowanego
planu rollbacku.

Fresh install jest podstawową ścieżką dla nowych użytkowników, ponieważ 0.5.2
jest pierwszym publicznym wydaniem. Upgrade `0.4.3 -> 0.5.2` jest
zachowaną ścieżką migracji istniejącej instalacji legacy.

Instalator obsługuje:

- fresh install z pustymi bazami;
- upgrade istniejącej instalacji `0.4.3` bez zmiany sekretów;
- Public w trybie `lan` albo `reverse-proxy`;
- Control domyślnie w trybie `lan` oraz opcjonalnie `reverse-proxy`;
- osobne portale TrueNAS dla Public i Control;
- dynamiczne wykrycie bieżącego prywatnego gateway `/32` przy każdym starcie
  kontenera reverse proxy;
- tworzenie kopii SQLite i rekursywnego snapshotu ZFS przed upgrade.

## Model bezpieczeństwa

Proces Public montuje wyłącznie dataset `app`. Proces Control montuje `app`,
`control` i `backups`. Oba kontenery działają jako UID/GID `568:568`, z
read-only rootfs, `cap_drop: ALL` i `no-new-privileges`. Nie otrzymują Docker
socketa, TrueNAS API, konfiguracji Caddy, katalogów domowych ani datasetów
multimediów.

Sekrety nie są wpisywane do Compose. Produkcyjny kontrakt plików jest
następujący:

- `public.env` — `root:root`, tryb `0600`;
- `control.env` — `root:root`, tryb `0600`;
- `bootstrap-credentials.txt` — `root:root`, tryb `0600`;
- `app/.vapid-private.pem` — `apps:apps` (`568:568`), tryb `0600`, aby proces
  Public mógł odczytać klucz bez uprawnień roota.

Instalator uruchomiony jako root tworzy trzy pierwsze pliki atomowo z trybem
`0600`, a dla `public.env` i `control.env` jawnie wymusza także `root:root`
niezależnie od ownershipu datasetu nadrzędnego. Klucz VAPID jest generowany przy
starcie Public przez kontener działający
jako `568:568`, również z trybem `0600`. Po pierwszym logowaniu i wymuszonej
zmianie haseł należy usunąć plik bootstrap credentials oraz wyczyścić bootstrap
passwords z env.

Control nigdy nie może być przekierowany na Internet. Dozwolone sieci Control
muszą być prywatnymi/lokalnymi CIDR i nie mogą zawierać `/0`.

## Pobranie wersjonowanej paczki

Po publikacji release jedna udokumentowana komenda pobierze paczkę i jej sumę,
zweryfikuje SHA-256 przed rozpakowaniem, a następnie uruchomi domyślny dry-run:

```sh
version=0.5.2; base="https://github.com/<owner>/penczREQ/releases/download/v${version}"; curl --fail --location --remote-name "${base}/penczreq-installer-${version}.tar.gz" --remote-name "${base}/penczreq-installer-${version}.tar.gz.sha256" && sha256sum --check "penczreq-installer-${version}.tar.gz.sha256" && tar --extract --gzip --file "penczreq-installer-${version}.tar.gz" && sudo "penczreq-installer-${version}/install.sh" --dry-run
```

Nie uruchamiaj skryptu pobranego z sieci, jeśli `sha256sum --check` nie zwróci
`OK`. Nie zamieniaj tej kontroli na sumę podaną w tym samym niezaufanym pliku
bez porównania jej z GitHub Release.

## Dry-run bez zmian TrueNAS

Dry-run jest zachowaniem domyślnym. Prowadzi przez te same pytania co instalacja,
ale nie generuje prawdziwych sekretów, nie wywołuje `midclt`, nie tworzy
datasetów i nie zmienia aplikacji:

```sh
./install.sh --dry-run --output-dir /tmp/penczreq-0.5.2-preview
```

Można też skopiować `answers.example.json`, wypełnić wyłącznie niesekretne
wartości i uruchomić:

```sh
./install.sh \
  --dry-run \
  --answers ./answers.local.json \
  --output-dir /tmp/penczreq-0.5.2-preview
```

Wynikiem są `compose.yaml`, dwa zredagowane przykłady env oraz `summary.json`.
Prawdziwe sekrety nie są tworzone ani wypisywane.

### Lokalny obraz RC podczas UAT

Opcja maintainerska `--local-image` jest przeznaczona wyłącznie do kontrolowanego
UAT nieopublikowanego obrazu, który operator wcześniej zaimportował do lokalnego
image store TrueNAS. W tym trybie oba serwisy otrzymują `pull_policy: never`,
więc instalator nie próbuje pobierać go z registry. Ścieżka `--execute
--local-image` wykonuje read-only `docker image inspect` po walidacji targetu,
ale przed utworzeniem datasetu, snapshotem, backupem, zapisem env, generowaniem
sekretów i wywołaniem `app.create`/`app.update`. Brak wskazanego taga kończy ten
preflight bez mutacji i z instrukcją wcześniejszego `docker load`.

Do ręcznego importu offline należy używać wyłącznie jednoznacznie nazwanego
artefaktu `penczreq-<version>-docker-amd64.tar` (lokalny RC może dodatkowo
zawierać `rc-local-<commit>`), wygenerowanego przez standardowy `docker save`.
Pure OCI archive, np. plik zakończony `-oci.tar`, służy wyłącznie do
inspekcji/provenance i nie jest wejściem dla tego workflow. Po zweryfikowaniu
opublikowanej sumy SHA-256 operator importuje Docker archive i potwierdza tag:

```sh
sha256sum -c penczreq-0.5.2-docker-amd64.tar.sha256
sudo docker load -i penczreq-0.5.2-docker-amd64.tar
sudo docker image inspect ghcr.io/<owner>/penczreq:0.5.2
```

W `answers.local.json` ustaw pole `image` na dokładny tag odczytany po imporcie.
Przykładowy plik odpowiedzi dla instalacji sieciowej wskazuje ruchomy kanał.

```sh
./install.sh --dry-run --local-image --answers ./answers.local.json
sudo ./install.sh --execute --local-image --answers ./answers.local.json
```

Bez jawnej opcji `--local-image` generator zachowuje dotychczasową semantykę:
nie dodaje pola `pull_policy`. W instalacji z rejestru pozwala to TrueNAS
pobrać obraz wskazany przez zarejestrowany YAML podczas aktualizacji.

## Tryby dostępu

### Public `lan`

- URL ma postać `http://IP_NAS:PORT_PUBLIC`;
- `COOKIE_SECURE=false`;
- nie wymaga domeny ani reverse proxy;
- portal TrueNAS prowadzi bezpośrednio do podanego URL;
- zwykła aplikacja działa, ale Web Push/PWA na innych urządzeniach może być
  ograniczone przez brak bezpiecznego kontekstu HTTPS.

Dla pełnej instalacji PWA/WebAPK na Androidzie użyj Public za HTTPS. Android
Chrome został empirycznie zweryfikowany na fizycznym urządzeniu:
instalacja, uruchomienie, Web Push i powiadomienia systemowe zakończyły się
powodzeniem. Samsung Internet powinien działać zgodnie z udokumentowanym
wsparciem PWA/Web Push, ale nie został jeszcze empirycznie sprawdzony w tym UAT.

Na iOS/iPadOS 16.4+ Web Push jest dostępny dla aplikacji dodanej do ekranu
początkowego, gdy platforma/przeglądarka spełnia wymagania i Public działa w
bezpiecznym kontekście. penczREQ nie został jeszcze empirycznie zweryfikowany na
iOS/iPadOS. Edge, Brave, Opera i Firefox mogą tworzyć skrót lub aplikację
zależną od przeglądarki; taki rezultat nie oznacza błędu serwera. penczREQ 0.5.2
nie używa TWA, Bubblewrap, osobnego APK ani wrappera i nie obiecuje pracy offline
na prywatnych danych.

### Public `reverse-proxy`

- URL musi używać HTTPS;
- `COOKIE_SECURE=true`;
- port backendu pozostaje związany z konkretnym adresem NAS;
- przy każdym starcie Public wyznacza faktyczny, bezpośrednio połączony prywatny
  gateway Docker i ufa wyłącznie jego `/32`;
- nagłówki forwarded od innych peerów nie zmieniają adresu klienta.

Automatyczny gateway nie jest zapisywany jako ustawienie użytkownika ani do env.
Po odtworzeniu sieci Docker zwykły restart kontenera wyznacza nowy adres i usuwa
stary adres automatyczny z efektywnego zbioru. Ręczne wpisy trusted proxy są
zachowane, scalane i deduplikowane; zmiana ręcznej listy w Control zaczyna
obowiązywać po restarcie Public. Brak jednego bezpiecznego gateway RFC1918
kończy się ostrzeżeniem i fail-closed, bez poszerzenia zaufania. `*` i `/0` są
niedozwolone.

### Control `lan` — wariant domyślny

- URL ma postać `http://IP_NAS:PORT_CONTROL`;
- nie wymaga Caddy, lokalnej domeny ani prywatnego CA;
- `COOKIE_SECURE=false`;
- host i rzeczywisty klient nadal muszą przejść listy Control;
- portu nie wolno publikować na routerze.

### Control `reverse-proxy` — wariant zaawansowany

- URL musi używać HTTPS;
- `COOKIE_SECURE=true`;
- przy każdym starcie Control wyznacza dynamicznie ten sam wąski gateway `/32`
  przed skonfigurowaniem obsługi forwarded headers;
- `CONTROL_ALLOWED_NETWORKS` nadal ogranicza końcowych klientów do prywatnego
  LAN/VPN. Sama obecność reverse proxy nie pozwala wystawić Control do Internetu.

## Fresh install

Przed wykonaniem przygotuj wersjonowany obraz oraz prawidłowy dataset nadrzędny,
np. `POOL/apps`. Następnie uruchom na TrueNAS:

```sh
sudo ./install.sh --execute --answers ./answers.local.json
```

Installer:

1. sprawdza techniczną zgodność z gałęzią TrueNAS `25.10.x`, uprawnienia root i
   wymagane narzędzia; zweryfikowanym empirycznie targetem projektu jest
   `25.10.6`;
2. odmawia fresh install, jeżeli istnieje aplikacja, env albo bazy;
3. tworzy/sprawdza datasety `penczreq`, `app`, `control`, `backups`;
4. ustawia UID/GID `568:568`, `0770` i wykonuje kontrolowany test zapisu;
5. generuje różne sekrety sesji Public/Control oraz wspólny wymagany klucz
   szyfrowania konfiguracji;
6. generuje różne hasła startowe zgodne z polityką;
7. zapisuje env i credentials jako `0600`;
8. waliduje Compose przez `docker compose config -q`;
9. tworzy Custom App oficjalną metodą middleware `app.create`;
10. odczytuje stan aplikacji; dla dokładnego `STOPPED` wykonuje job `app.start`,
    dla innych stanów non-`RUNNING` tylko ograniczenie czasowo czeka na
    `RUNNING`;
11. usługi reverse proxy wyznaczają bieżący gateway podczas własnego startu,
    bez zapisu adresu do env i bez dodatkowego `app.redeploy`;
12. wymaga `RUNNING`, kontroluje bazy i dopiero wtedy zapisuje prywatny
    raport wyniku.

Po instalacji najpierw zaloguj się do Control, potem do Public. Zmień oba hasła
startowe. Token TMDB ustaw przez Control, nie w historii terminala ani pliku
odpowiedzi.

## Upgrade istniejącej instalacji legacy 0.4.3 → 0.5.2

W `answers.local.json` ustaw `"mode": "upgrade"` i przypnij `image` do
docelowego wydania, np. `ghcr.io/<owner>/penczreq:0.5.2`, lub do jego
zweryfikowanego digestu. Po zatwierdzeniu okna serwisowego zatrzymaj obie
usługi przed wykonaniem kopii i snapshotu. Instalator wymaga istniejącej
aplikacji, obu env i obu baz. Nie regeneruje `SESSION_SECRET`,
`CONTROL_SESSION_SECRET` ani `CONFIG_ENCRYPTION_KEY`; odmawia działania, gdy
klucze są brakujące lub klucze szyfrowania Public/Control są różne.

Przed `app.update` instalator:

1. wykonuje `PRAGMA quick_check` i `PRAGMA foreign_key_check` obu baz;
2. tworzy spójne kopie przez SQLite Backup API i ponownie je kontroluje;
3. tworzy rekursywny snapshot `ROOT_DATASET@pre-0.5.2-TIMESTAMP`;
4. zapisuje prywatny `ROLLBACK.json` z nazwą snapshotu i ścieżkami kopii;
5. dopiero potem uzupełnia niesekretne pola env i wywołuje `app.update`.

`app.update` zachowuje wcześniejszy stan zatrzymanej Custom App. Dlatego
instalator obsługuje także wspierany upgrade aplikacji `STOPPED`: po aktualizacji
odczytuje stan, uruchamia wyłącznie dokładny `STOPPED` przez job `app.start` i
czeka na `RUNNING`. Nie wymaga ręcznego startu przez operatora. Dla innego stanu
non-`RUNNING` nie uruchamia równoległego jobu i kończy fail-closed, jeżeli
ograniczone czasowo oczekiwanie nie osiągnie `RUNNING`.

Uruchomienie tego trybu na produkcji wymaga jawnej zgody operatora na dokładną
operację, świeżego planu oraz weryfikacji stanu wejściowego. Sam fakt istnienia
skryptu nie uruchamia ani nie autoryzuje migracji.

Prywatny upgrade walidacyjny `0.5.0/0.5.1 -> 0.5.2` korzysta z tego samego
trybu. Nie rotuje istniejących sekretów ani nie usuwa ręcznych lub historycznych
wpisów trusted proxy. Jeżeli treść `public.env` lub `control.env` jest już
aktualna, instalator zachowuje jej bajty i naprawia wyłącznie metadane do
`root:root` oraz `0600` przed `app.update`.

## Prosta aktualizacja obrazu a migrator

Control wykonuje typowe migracje przy starcie. Przed zapisem nowego buildu
zabezpiecza obie bazy w /backups/pre-update. Public, CLI i zadania w tle
respektują wspólną blokadę. Przy pierwszym przejściu z historycznej 0.5.2
zatrzymaj aplikację i CLI, potwierdź STOPPED i wykonaj kopię oraz snapshot.
W katalogu montowanym jako /control-data utwórz pusty plik .legacy-writers-stopped.
Utrzymuj starą aplikację zatrzymaną aż do zakończenia przekazania.

W nowej instalacji ze śledzeniem aktualizacji oba serwisy wskazują
`ghcr.io/<owner>/penczreq:stable` dla linii zgodnej z 0.5.2.
Użyj wygenerowanego YAML i istniejących na NAS plików env oraz host paths.
W tym trybie pomiń `--local-image`. W Apps → Configuration → Settings sprawdź
wykrywanie aktualizacji obrazów. Po pojawieniu się „Update available” wybierz
penczREQ i Update. Przy zwykłej aktualizacji YAML pozostaje taki sam. Po
zakończeniu porównaj wersję i digest z wydaniem, sprawdź oba healthchecki,
ustawienia i zapisany rekord użytkownika. Jeśli obraz jest aktualny w GHCR,
a aktualizacji nie widać, sprawdź dokładne tagi obu usług, dostęp NAS do
rejestru i cykl sprawdzania obrazów.

Na TrueNAS SCALE 25.10.7 odseparowana Custom App wykryła zmianę digestu
w GHCR. Zatwierdzenie Update w panelu odtworzyło Public i Control z nowego
obrazu. Zachowały się obie bazy, konta, ustawienia języka, zgłoszenie,
zarejestrowany YAML i oba env. W osobnej próbie wadliwego obrazu nadrzędny
job `app.upgrade` miał status SUCCESS, a `app.redeploy` zakończył się FAILED.
Control zakończył pracę, Public był niedostępny, a znacznik aktualizacji zniknął.
W takim przypadku sprawdź job potomny i oba healthchecki. W UAT przywrócenie
snapshotu i przypięcie poprzedniego digestu przywróciło działanie aplikacji.

Przejście z lokalnego RC wymaga jednorazowego porównania wersji, schematu
i zarejestrowanego YAML. Zabezpiecz obie bazy, snapshot rekurencyjny,
poprzedni obraz i konfigurację. Dopiero po zatwierdzeniu zmień odwołanie obu
serwisów na kanał i usuń `pull_policy: never`, zachowując te same montowania
i pliki env. Jeśli potrzebna jest migracja, wykonaj ją najpierw instalatorem
z numerowanym obrazem. Panel Apps nie uruchamia naszego instalatora.
Nie przywraca też host paths przy rollbacku aplikacji.

Stała instancja UAT używa `:uat` w obu serwisach oraz własnych datasetów,
portów, kont i plików env. W Public ustaw WEB_PUSH_DELIVERY=disabled. Oba
kanały używają Apps → Update. Osobny migrator obsługuje zmiany wymagające
interwencji operatora, np. nowego montowania lub niedostępnego klucza.

Kontrole baz przy `--execute` wymagają miejsca w `/dev/shm` na prywatne kopie obu baz razem z WAL lub dziennikiem, a przy aktualizacji także na eksport kopii. Instalator używa jednej rekurencyjnej migawki ZFS i sprawdza, czy każdy plik pochodzi z właściwej migawki dziecka. Nowa próba gotowości pobiera nową migawkę. Migawka rollbacku i zweryfikowana para kopii pozostają dostępne po aktualizacji.

### Uprawnienia stałej instancji UAT pod udziałem SMB

1. W **Datasets** sprawdź punkt montowania rodzica, typ ACL, ACL Mode oraz
   właściciela. W **Shares → Windows (SMB) Shares** sprawdź także udziały
   obejmujące rodzica lub katalog poniżej planowanego UAT. Dostęp klienta
   zależy zarówno od ACL udziału, jak i praw do przejścia przez katalogi.
   Właściwości ZFS i źródło ich wartości możesz odczytać poleceniem
   `zfs get -o name,property,value,source acltype,aclmode,aclinherit DATASET`.
2. Przed zapisaniem sekretów utwórz prywatny dataset UAT. Pod udziałem
   NFSv4 wybierz NFSv4 również dla każdego osobno montowanego dziecka.
   Sprawdź wartości po utworzeniu. Dla rodzica używającego
   `nfsv4/passthrough/passthrough` taki sam zestaw obsługuje zmianę trybu
   prywatnych plików przez aplikację. Sam wybór presetu ani zgodność
   właściwości ZFS nie określają, kto ma dostęp do plików. Pozostaw tworzenie
   dodatkowego udziału SMB wyłączone.
3. Skontroluj wpisy ACL na nowym korzeniu i flagi dziedziczenia. Daj
   administratorowi dostęp do korzenia (np. `0700`) i jego plików env
   (`0600`). Zamontuj osobno katalogi danych Public, Control i kopii.
   UID/GID kontenera muszą mieć wymagany dostęp wewnątrz tych montowań.
   Sprawdź też prywatność klucza VAPID i katalogów kopii przedmigracyjnych.
   Rekurencyjne skopiowanie ACL udziału może przyznać grupie aplikacji
   dostęp do sekretów. Instalator 0.5.2 tworzy własny układ datasetów
   i ustawia właściciela ich korzeni. Przy prywatnym korzeniu zarządzanym
   przez operatora przygotuj host paths oddzielnie.
4. Przed naprawą istniejącego UAT zatrzymaj jego procesy, zapisz właściwości
   ZFS wraz ze źródłami, granice montowań, UID/GID, tryby oraz rozszerzone
   i domyślne ACL. Wykonaj snapshot UAT. Zmiana typu nie tłumaczy nazwanych
   wpisów POSIX na wpisy NFSv4. Jeżeli takie wpisy istnieją, przygotuj
   odwzorowanie praw przed zmianą. Przy prostych prawach porównaj wszystkie
   ścieżki po zmianie, zanim ponownie włączysz aplikację. Odtworzenie
   właściwości ZFS wymaga osobnego kroku przy ewentualnym rollbacku.
5. Sprawdź listowanie przez zwykłą sesję SMB i próbę utworzenia, zmiany nazwy
   oraz usunięcia pliku tymczasowego w każdym zapisywalnym montowaniu jako
   użytkownik usługi. Sprawdź źródło alertu SMBPath i aktywne alerty.
   Po pierwszym **Apps → Update** skontroluj oba zadania TrueNAS, wewnętrzne
   healthchecki, dziennik Control, integralność obu baz i sumy pary kopii.

Schemat pierwszego publicznego wydania 0.5.2 zawiera nullable
`requests.title_en`. Upgrade `0.4.3 -> 0.5.2` nie jest prostą aktualizacją image-only.
Oficjalny tryb
`upgrade` tworzy najpierw sprawdzoną parę kopii i snapshot rollbacku, następnie
rejestruje nowy Compose przez `app.update`. Jeżeli aplikacja pozostała
`STOPPED`, instalator uruchamia ją przez `app.start` i wymaga `RUNNING`. Start
Control wykonuje idempotentną migrację
lokalnego schematu, Public czeka na zdrowy Control, a instalator przed zapisaniem
udanego wyniku ponownie kontroluje obie bazy i wymaga obecności
`requests.title_en`. Fresh install tworzy tę nullable kolumnę bezpośrednio z
aktualnego schematu.

Sieciowy `python -m request_app.cli backfill-english-titles` jest osobnym,
kontrolowanym wzbogaceniem danych, a nie automatycznym ani obowiązkowym krokiem
migracji schematu. Bez backfillu istniejące `title_en = NULL` jest poprawne i EN
używa tytułu oryginalnego. Wykonanie `--apply` na produkcji wymaga osobnej jawnej
zgody i nie jest autoryzowane przez sam upgrade instalatora.

Przygotowany workflow wydania ma tworzyć paczkę instalatora, sumy SHA-256,
manifest wydania i release notes, a po rzeczywistym zbudowaniu obrazu także SPDX
SBOM oraz raporty skanów. Obraz `:<wersja>` jest kotwicą rollbacku; `:stable`
może zostać przesunięty dopiero po zaakceptowanym wydaniu. Push do `main` nie
publikuje obrazu i nie aktualizuje NAS-a. Przed utworzeniem kolejnego wydania
sprawdź reguły tagów w Settings → Environments → release. Przy liście
pojedynczych tagów dodaj dokładny tag zatwierdzonego wydania i zachowaj
wymagany przegląd wdrożenia. Na Windows 11 z Docker Desktop
wykonano lokalnie realne buildy Linux/amd64, niezależny rebuild bez cache, SPDX
image SBOM, Trivy image scan i utwardzony runtime smoke Public/Control. Osobna
walidacja TrueNAS SCALE 25.10.6 potwierdziła fresh install, upgrade, rollback i
kontrakt datasetów ZFS. Pełny kontrakt jest opisany w
[`../../docs/UPDATE.md`](../../docs/UPDATE.md).

## Zweryfikowany kontrakt TrueNAS

Poniższa kolejność została wykonana dla kandydata 0.5.2 na TrueNAS SCALE
25.10.6 i pozostaje referencyjną bramką dla kolejnych kandydatów. Nie stanowi
polecenia uruchomienia `--execute` bez osobnej decyzji operatora.

### Etap A — fresh install side-by-side

Najpierw należy utworzyć całkowicie odrębną testową instancję:

- inna nazwa Custom App;
- osobne testowe datasety;
- inne porty Public i Control;
- LAN-only, jeżeli upraszcza izolację;
- brak zmian w działającej aplikacji produkcyjnej.

Celem jest empiryczna weryfikacja fresh install i `app.create`: datasetów,
UID/GID, permissions, ownershipu i trybu plików sekretów, wygenerowanego Compose,
portali, healthchecków, logowania, konfiguracji TMDB, dynamicznego
gateway/proxy oraz podstawowego działania Public i Control.

### Etap B — prywatny upgrade 0.5.0/0.5.1 → 0.5.2

Następnie należy użyć wyłącznie prywatnej instancji UAT 0.5.0 lub 0.5.1 albo jej
odrębnego klona:

- wyłącznie prywatna kopia danych, nigdy bezpośrednio produkcyjne datasety;
- osobna nazwa Custom App, porty i datasety;
- zero mutacji aplikacji i baz produkcyjnych;
- izolacja Web Push przed pierwszym uruchomieniem klona.

Kopia bazy może zawierać prawdziwe endpointy i klucze subskrypcji Web Push. Przed
uruchomieniem testowej instancji trzeba kontrolowanie usunąć albo wyłączyć
subskrypcje **wyłącznie w kopii UAT** (lub zastosować równoważną blokadę wysyłki),
aby test nie wysyłał powiadomień realnym użytkownikom. Nie wolno zmieniać
subskrypcji w produkcyjnej bazie.

Dopiero tak odizolowana instancja może przejść prywatny upgrade
`0.5.0/0.5.1 -> 0.5.2`. Test ma potwierdzić zachowanie sekretów i danych, naprawę
ownershipu obu env do `root:root` bez niepotrzebnej zmiany ich treści, spójną
parę backupów, snapshot, `ROLLBACK.json`, `quick_check`, `foreign_key_check`,
logowanie, sesje oraz zachowanie przepływu aplikacji `STOPPED`.

### Etap C — rozważenie produkcyjnego upgrade

Produkcję można rozważać dopiero po PASS wszystkich trzech prób: fresh
side-by-side, upgrade odizolowanego klona i rollback odizolowanego klona. Przed
dokładnie zatwierdzoną operacją produkcyjną nadal wymagane są:

1. świeża, zweryfikowana para backupów;
2. `quick_check` i `foreign_key_check` obu baz;
3. rekursywny snapshot ZFS;
4. zapis aktualnego digestu obrazu;
5. zapis aktualnego Compose;
6. kompletny `ROLLBACK.json`;
7. jawna zgoda operatora na konkretną zmianę oraz jej rollback.

## Rollback

Jeżeli aplikacja nie osiąga `RUNNING`, nie usuwaj starego obrazu, env ani kopii.
Zatrzymaj dalsze działania i zachowaj komunikat błędu. Kontrolowany rollback
produkcyjny obejmuje:

1. zatrzymanie Custom App;
2. przywrócenie snapshotów głównego datasetu i jego dzieci z `ROLLBACK.json`.
   Sama para kopii SQLite wystarcza tylko wtedy, gdy zmiany dotyczyły wyłącznie
   baz i dostępne są odpowiadające im konfiguracja oraz klucze;
3. przywrócenie poprzedniego zarejestrowanego Compose i przypięcie dawnego
   digestu. Dla lokalnego RC zachowaj archiwum Docker, jego sumę, tag i ID obrazu;
4. uruchomienie obu usług;
5. `quick_check`, foreign keys, logowanie Public/Control, dane i Web Push.

Nie przywracaj tylko jednej bazy, jeżeli operacja dotyczyła kont, ustawień albo
bezpieczeństwa. Dokładne komendy rollbacku należy zatwierdzić osobno dla
rzeczywistego snapshotu i konfiguracji.

## Oficjalny kontrakt TrueNAS

Installer używa lokalnego klienta middleware `midclt` i jobów `app.create`,
`app.update` oraz warunkowo `app.start`. Odpowiada to namespace'owi wersjonowanego
JSON-RPC API TrueNAS 25.10 dla Custom Apps z
`custom_compose_config_string`. Numer API nie jest deklaracją empirycznej
certyfikacji całej gałęzi 25.10.x. Instalator sprawdzono na 25.10.6,
a aktualizację obrazów z panelu i odzyskiwanie danych również na 25.10.7.
Compose zawiera `x-portals`, które TrueNAS wykorzystuje do
pokazania klikalnych linków Public i Control.

- API 25.10: <https://api.truenas.com/v25.10/api_methods_app.create.html>
- Custom App YAML: <https://www.truenas.com/docs/scale/apps/installcustomappscreens/>

## Rzeczy poza instalatorem

Installer nie zmienia Caddy, Jellyfin ani routera, nie montuje mediów, nie daje
kontenerom dostępu do Docker/TrueNAS i nie usuwa historycznego stagingu. Nie
publikuje obrazu ani repozytorium. Konfiguracja ownera GHCR, publikacja i
migracja produkcyjna są oddzielnymi, kontrolowanymi krokami
wydaniowymi/operacyjnymi.

## Diagnostyka SQLite na działającym NAS-ie

Do kontroli operatorskiej użyj [procedury odczytu z migawki ZFS](../../docs/DIAGNOSTYKA-SQLITE.md). Obejmuje ona Public, Control i dziennik migracji.
