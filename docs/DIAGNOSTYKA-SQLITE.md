# Diagnostyka SQLite na TrueNAS SCALE

Procedurę sprawdzono na TrueNAS SCALE 25.10.7 dla Public, Control i dziennika migracji. Operator wykonuje połączenia SQLite wyłącznie na jednorazowych kopiach w prywatnym katalogu tmpfs. Kontenery zachowują standardowe uprawnienia. Surowe dane, dzienniki, pliki env i kopie pozostają na NAS-ie.

## Dlaczego potrzebna jest migawka

Podczas odczytu bazy w trybie WAL SQLite może utworzyć pliki `-wal` i `-shm`, nawet przy połączeniu `mode=ro`. Właścicielem nowych plików staje się użytkownik procesu. Istniejący WAL może zawierać zatwierdzone transakcje, których jeszcze nie ma w głównym pliku. Kopiowanie kolejno plików aktywnego datasetu podczas zapisów nie gwarantuje wspólnego punktu w czasie. Parametr `immutable=1` na zmieniającej się bazie nie zapewnia takiej gwarancji. [Dokumentacja WAL](https://www.sqlite.org/wal.html) · [Kopiowanie podczas transakcji](https://www.sqlite.org/howtocorrupt.html)

## Sprawdzenie na działającym UAT

Poniższe polecenia są dla powłoki NAS. Przed użyciem sprawdź, że ścieżki montowań aplikacji wskazują dokładnie na wskazany dataset. UAT w tej konfiguracji trzyma wszystkie trzy bazy w jednym datasecie. Dla układu instalatora jedna rekurencyjna migawka rodzica obejmuje oba dzieci. Każda baza jest dostępna pod własnym punktem montowania, w podkatalogu `.zfs/snapshot/NAZWA`. Osobne migawki robione po kolei nie spełniają tego warunku.

1. Odczytaj stan aplikacji, właściwości datasetu i jego dzieci. Zanotuj UID, GID, tryb, i-węzeł, rozmiar oraz czasy modyfikacji baz i istniejących plików pomocniczych. Do tego wystarcza `stat`. Zmiany wywołane przez działającą aplikację mogą wystąpić niezależnie od diagnostyki.
2. Z konta operatora utwórz jedną nierekurencyjną migawkę UAT z unikatową nazwą. Zachowaj zwrócone ID i GUID. W tym przykładzie narzędzie `findmnt` powinno pokazać typ `zfs` i źródło z końcówką `@<nazwa>`.

   ```sh
   dataset=apps-pool/containers/penczreq-update-uat
   name="penczreq-diag-$(date -u +%Y%m%dT%H%M%SZ)"
   midclt call pool.snapshot.create "{\"dataset\":\"$dataset\",\"name\":\"$name\",\"recursive\":false}"
   snapshot="/mnt/$dataset/.zfs/snapshot/$name"
   findmnt -n -o FSTYPE,SOURCE,OPTIONS -T "$snapshot"
   ```

3. Uruchom skrypt projektu na NAS, przekazując mu wyłącznie ścieżkę migawki. Można też przekazać treść samodzielnego modułu `deploy/truenas/sqlite_snapshot.py` przez istniejącą sesję SSH na standardowe wejście interpretera `python3 - "$snapshot"`. W obu przypadkach interpreter i `/dev/shm` znajdują się na NAS. Konto musi mieć odczyt odpowiednich plików migawki. Przy odmowie dostępu przerwij diagnostykę z tego konta. Zachowaj ACL i właścicieli.

   ```sh
   python3 tools/diagnose_sqlite_snapshot.py "$snapshot"
   ```

   Skrypt weryfikuje nazwę, źródło i tryb tylko do odczytu migawki, wspólny system plików oraz lokalizację tmpfs. Kopiuje bazy wraz z istniejącymi `-wal` i `-journal` pod właściwymi nazwami. SQLite sam odbudowuje indeks `-shm` po stronie roboczej. Pełne `integrity_check`, `foreign_key_check` i porównanie identyfikatorów instalacji obejmują kopie. Wynik zawiera wersje schematów i ostatni etap migracji. Brak miejsca, uprawnień, wymaganych plików lub niezgodność danych oznacza nieudaną diagnostykę. Katalog roboczy jest usuwany również po zwykłym wyjątku; po zabiciu procesu sprawdź pozostałości prywatnego katalogu w tmpfs.

4. Oczekiwany stan gotowego kandydata to ta sama instalacja, schemat 2 obu komponentów oraz `phase=ready` dla działającego buildu. Przy fazie pośredniej zachowaj migawkę potrzebną do analizy błędu. Do osobnej kontroli kopii przedmigracyjnej porównaj oba SHA-256 z chronionym manifestem **i** dziennikiem Control, po czym ponownie sprawdź integralność obu plików w tmpfs. Dostęp do prywatnego katalogu pary powinien odbywać się jako uprawniony UID aplikacji przez montowanie migawki tylko do odczytu.
5. Porównaj metadane w źródle z pomiarem początkowym. Po sukcesie sprawdź ID i GUID migawki; usuń tylko utworzoną przez siebie migawkę diagnostyczną. Zachowaj pary przedmigracyjne i migawki rollbacku.

   ```sh
   midclt call pool.snapshot.get_instance "$dataset@$name"
   midclt call pool.snapshot.delete "$dataset@$name"
   ```

## Istniejące ścieżki operatorskie

Instalator pobiera wspólną migawkę rekurencyjną zarówno do kontroli po starcie, jak i do wytworzenia pary kopii przed aktualizacją. Sprawdza dokładną nazwę datasetu każdego dziecka. Otwiera w SQLite wyłącznie kopie w prywatnym tmpfs. Po każdej próbie usuwa własną migawkę diagnostyczną; migawka rollbacku ma inną nazwę i obejmuje gotowe pliki kopii. Gdy proces został zabity, sprawdź osierocone migawki `penczreq-check-*` według nazwy, czasu i datasetu. Brak miejsca w tmpfs, niewłaściwe montowanie lub nieudane usunięcie migawki uniemożliwiają potwierdzenie instalacji.

W kontenerze Control koordynator wykonuje operacje SQLite jako konto aplikacji. Kontrole integralności otwierają istniejące bazy w trybie odczytu bez zmiany trybu dziennika. Online Backup API uwzględnia zatwierdzone wpisy w WAL. W systemie POSIX aplikacja i CLI sprawdzają UID bazy i istniejących plików pomocniczych przed połączeniem. Do diagnostyki z konta operatora używaj procedury migawkowej powyżej.
