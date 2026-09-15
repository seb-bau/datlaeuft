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

# Webhooks
URL: https://wattnu-tst.wbg-luenen.de/webhooks/wowiport
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