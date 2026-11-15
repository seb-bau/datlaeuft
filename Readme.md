# Installation
```
git clone git@github.com:seb-bau/datlaeuft.git wattnu-test
python3 -m venv venv
pip install -r requirements.txt
sudo apt install -y uwsgi uwsgi-plugin-python3
nano uwsgi.ini
sudo nano /etc/systemd/system/wattnu-test.service
sudo systemctl daemon-reload
sudo nano /etc/systemd/system/wattnu-test-worker.service
sudo systemctl daemon-reload
nano instance/config.py
sudo apt install libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libharfbuzz-subset0
flask db upgrade
flask datlaeuft-seed-settings
flask datlaeuft-create-admin
flask datlaeuft-import-entra-users
flask datlaeuft-sync-buildings
flask datlaeuft-sync-company-codes
flask datlaeuft-sync-commission-types
flask datlaeuft-sync-object-directory
flask datlaeuft-sync-buildings
flask datlaeuft-sync-service-catalog
flask datlaeuft-sync-commissions
flask datlaeuft-sync-object-photos

```
* Cron anlegen auf flask datlaeuft-sync-commissions, z.B.
```
cd /var/projects/wattnu-test && /var/projects/wattnu-test/venv/bin/flask datlaeuft-sync-commissions  >>/var/projects/wattnu-test/logs/datlaeuft-sync-commissions.log 2>&1
```
* uwsgi.ini
````
[uwsgi]
plugin = python3
chdir = /var/projects/wattnu-test
module = wsgi:app
home = /var/projects/wattnu-test/venv

master = true
processes = 2
threads = 2
enable-threads = true
single-interpreter = true
lazy-apps = true

http-socket = 0.0.0.0:8000
http-keepalive = true
listen = 128
so-keepalive = true

need-app = true
vacuum = true
die-on-term = true
thunder-lock = true

harakiri = 180
harakiri-verbose = true
max-requests = 1000
reload-on-rss = 512
worker-reload-mercy = 30

buffer-size = 65535
post-buffering = 8192
log-x-forwarded-for = true

````

* System unit main
````
[Unit]
Description=WattNu-Test uWSGI
After=network-online.target mariadb.service
Wants=network-online.target
Requires=mariadb.service

[Service]
Type=simple
User=edv
Group=edv
WorkingDirectory=/var/projects/wattnu-test
Environment=PATH=/var/projects/wattnu-test/venv/bin:/usr/bin
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=XDG_CACHE_HOME=/var/projects/wattnu-test/instance/.cache
ExecStart=/usr/bin/uwsgi --ini /var/projects/wattnu-test/uwsgi.ini
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=5
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0027
LimitNOFILE=65536
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
ReadWritePaths=/var/projects/wattnu-test/instance /var/projects/wattnu-test/logs

[Install]
WantedBy=multi-user.target
````
* System unit worker
````
[Unit]
Description=WattNu-Test Background-Worker
After=network-online.target mariadb.service wattnu-test.service
Wants=network-online.target
Requires=mariadb.service
PartOf=wattnu-test.service

[Service]
Type=simple
User=edv
Group=edv
WorkingDirectory=/var/projects/wattnu-test
Environment=PATH=/var/projects/wattnu-test/venv/bin
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=XDG_CACHE_HOME=/var/projects/wattnu-test/instance/.cache
ExecStart=/var/projects/wattnu-test/venv/bin/flask --app wsgi:app datlaeuft-worker --poll-seconds 10
Restart=always
RestartSec=5
UMask=0027
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
NoNewPrivileges=true
ReadWritePaths=/var/projects/wattnu-test/instance /var/projects/wattnu-test/logs

[Install]
WantedBy=multi-user.target

````

# Aktualisierungen und Datenbankmigrationen

Vor einer Datenbankmigration den Webdienst und den Hintergrundworker stoppen. Auch geplante Synchronisationsbefehle während der Migration pausieren, damit deren offene Transaktionen keine Tabellenänderungen blockieren. MariaDB bleibt dabei gestartet.

Nach dem Aktualisieren des Anwendungscodes in der aktivierten Python-Umgebung ausführen:

```bash
flask db upgrade
flask db current
```

Webdienst, Worker und geplante Synchronisationen erst wieder starten, wenn das Upgrade erfolgreich beendet wurde.

Die Freigabemigration `a3c5e7f9b1d2` kann nach einem Abbruch erneut ausgeführt werden. Sie ergänzt nur fehlende Spalten, Indizes und Fremdschlüssel; bereits gespeicherte Benutzerzuordnungen und Freigaben bleiben erhalten. Nach einem Sperr-Timeout und anschließendem Fehler wegen einer bereits vorhandenen Spalte zuerst die korrigierte Anwendungsversion einspielen und dann `flask db upgrade` erneut ausführen. Es müssen dafür keine Spalten gelöscht oder Migrationsstände manuell umgestellt werden.

# Worker-Diagnose

Der Hintergrund-Worker begrenzt OPENWOWI-Anfragen einschließlich Anmeldung und Token-Erneuerung auf 10 Sekunden Verbindungsaufbau und 60 Sekunden ohne Antwortdaten. Schreibanfragen werden bei Verbindungsfehlern oder Zeitüberschreitungen nicht unmittelbar erneut gesendet; die vorhandenen Warteschlangen behandeln den Fehler.

Nach 120 Sekunden innerhalb eines Arbeitsschritts protokolliert der Worker `worker_step_slow` mit dem Namen des Schritts und dem Stacktrace des ausführenden Threads. Diese Diagnose läuft unabhängig vom blockierten Schritt und beendet ihn nicht. In Graylog nach `worker_step_slow`, `worker_step_failed`, `worker_iteration_failed` oder `mobile_delivery_failed` suchen. Eine langsame vollständige Batch-Verarbeitung kann ebenfalls diese Diagnose auslösen.

Der Health-Heartbeat wird weiterhin höchstens einmal pro Minute und nur bei tatsächlichem Fortschritt geschrieben, jetzt auch zwischen den Arbeitsschritten. Unerwartete Fehler einer Arbeitsrunde werden protokolliert; die Datenbanksitzung wird freigegeben und nach dem Poll-Intervall beginnt eine neue Runde.

# Webhooks

URL: https://example.com/webhooks/wowiport
3x Webhook (Vorgang, Auftrag, Nachricht)
Bearer

# API-Key
* Alle Buchwerke verwenden
* Alle Ticketquellen (damit die Infos aus beliebigen Tickets gelesen werden können)
* Berechtigungen:
  * Auftragswesen ändern/lesen
  * Ausstattungsgruppen und -elemente bearbeiten
  * Belegsystem Ausgangsrechnung lesen
  * Belegsystem Bewegungsdaten lesen
  * Belegsystem Buchungsvorgang erfassen
  * Belegsystem EIngangsrechnung erfassen
  * Belegsystem Stammdaten lesen
  * Dokumente ändern
  * Dokumente lesen
  * Objektbilder ändern
  * Objektbilder lesen
  * Objektdaten
  * Personen lesen
  * Ticket ändern
  * Tickt lesen
  * Vertragsdaten mit personenbezogenen Details
  * Vertragsdaten ohne personenbezogene Details

Dateien vorerst auf Blacklist stellen, damit alle Typen erlaubt sind.

Dies ist ein Testsatz in der README-Datei von datlaeuft.
