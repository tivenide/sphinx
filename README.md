# Sphinx

Sphinx ist eine selbst betriebene Webanwendung für deutschsprachige
Sprachdialoge. Nutzer melden sich im Browser an, sprechen mit dem Assistenten
**Cordi** und hören seine Antwort. Spracherkennung, Sprachmodell und
Sprachausgabe laufen auf der eigenen Infrastruktur. Eine optionale Wissensbasis
ergänzt die Antworten um Inhalte aus importierten PDF-Dokumenten.

## Funktionen

- Sprachaufnahme und gestreamte Sprachausgabe im Browser, mit Abbruchfunktion.
- Benutzerverwaltung mit Adminrollen, Anmeldung und Passwortänderung.
- Anzeige der Modellbereitschaft und Start vorbereiteter Modellcontainer durch
  Admins.
- PDF-Upload, Dokumentübersicht und Uploadverlauf für Admins.
- Dokumentwissen für Sprachantworten mit Nennung des Quelldateinamens.

Die Oberfläche ist auf Sprachdialoge ausgelegt; sie zeigt keinen Textchat und
keine Transkripte. Der Gesprächskontext wird vorübergehend im Arbeitsspeicher
gehalten.

## Aufbau

| Komponente | Aufgabe |
| --- | --- |
| Frontend | React/TypeScript; Caddy stellt HTTPS und die Verbindung zum Backend bereit. |
| Backend | FastAPI, Sprachverarbeitung, Benutzerverwaltung und SQLite für Konten, Sitzungen und Uploadmetadaten. |
| Modellsteuerung | Start und Statusabfrage der drei vorbereiteten Modellcontainer über den lokalen Docker-Socket. |
| Spracherkennung (ASR) | `Qwen/Qwen3-ASR-0.6B`. |
| Sprachmodell (LLM) | `Qwen/Qwen3-30B-A3B-Instruct-2507` mit vLLM. |
| Sprachausgabe (TTS) | CosyVoice3 mit einer mitgelieferten synthetischen Referenzstimme. |
| Wissensbasis, optional | PostgreSQL mit pgvector sowie ein separat betriebener Ollama-Dienst mit `bge-m3:latest`. |
| PDF-Import, optional | n8n und ein separater JavaScript-Runner. |

Die Dienste werden über [Docker Compose](compose.yaml) betrieben. Die
Modellcontainer verwenden ein internes Docker-Netz ohne Internetzugriff und
lesen ihre Gewichte aus einem gemeinsam genutzten, schreibgeschützt eingebundenen
Volume. Modellrevisionen und Referenzstimme stehen in
[models/manifest.json](models/manifest.json).

## Voraussetzungen

Die aktuelle Einrichtung richtet sich an Betreiber mit einer vorbereiteten
Docker- und GPU-Umgebung. **Die Setup-Skripte laden keine Images, Abhängigkeiten
oder Modellgewichte herunter.** Das Repository enthält Quellcode, Build-Rezepte
und Konfiguration; die Bereitstellung der Laufzeitumgebung erfolgt separat.

Benötigt werden:

- Linux mit Docker Engine, Docker Compose und Python 3 für die Setup-Skripte.
- Ein lokaler Docker-Unix-Socket für die Modellsteuerung; rootless Docker wird
  vom Setup berücksichtigt.
- NVIDIA Container Toolkit, zu den Runtime-Images passende NVIDIA-Treiber und
  ausreichend GPU-Speicher. Die mitgelieferte LLM-Konfiguration verwendet zwei
  GPUs; ASR und TTS benötigen zusätzlich Speicher auf der gewählten Sprach-GPU.
- Die in Compose konfigurierten Images und ein vollständig befülltes externes
  Modell-Volume, standardmäßig `ai-models`.
- Ein Browser mit Mikrofonzugriff und HTTPS mit vertrauenswürdigem Zertifikat.

### Images bereitstellen

Die Standardkonfiguration erwartet `sphinx-backend:0.3.0`,
`sphinx-frontend:0.3.0`, `sphinx-asr:0.3.0`, `sphinx-llm:0.3.0` und
`sphinx-tts:0.3.0`. Backend und Frontend können mit ihren Dockerfiles gebaut
werden:

```sh
docker compose build backend frontend
```

Die Modell-Builds haben zusätzliche Voraussetzungen:

| Image | Build-Rezept und Voraussetzung |
| --- | --- |
| ASR | [Dockerfile.asr](models/docker/Dockerfile.asr) baut auf `sphinx-asr-runtime:0.1.0` auf; [Runtime-Rezept](models/docker/Dockerfile.asr-runtime). |
| TTS | [Dockerfile.cosyvoice](models/docker/Dockerfile.cosyvoice) baut auf `sphinx-tts-runtime:0.1.0` mit CosyVoice-Code und Abhängigkeiten auf; [Runtime-Rezept](models/docker/Dockerfile.cosyvoice-runtime). |
| LLM | [Dockerfile.llm](models/docker/Dockerfile.llm) benötigt `sphinx-llm-runtime:py312-local` sowie ein kompatibles Python-3.12-`site-packages`-Verzeichnis als Build-Kontext `llm_packages`; [Runtime-Rezept](models/docker/Dockerfile.llm-runtime) und [Paketinventar](models/requirements-llm.txt). |

Die Runtime-Builds können Netzwerkzugriff benötigen. Insbesondere das
LLM-Paketinventar beschreibt eine vorbereitete Umgebung; es ist keine
vollständige Installationsanleitung für einen neuen Rechner. Die
Image-Vorbereitung ist vor den folgenden Startschritten abzuschließen.

### Modellgewichte bereitstellen

Das externe Volume muss diese Struktur enthalten:

```text
ai-models/
├── huggingface/hub/
│   ├── models--Qwen--Qwen3-ASR-0.6B/
│   └── models--Qwen--Qwen3-30B-A3B-Instruct-2507/
└── cosyvoice/Fun-CosyVoice3-0.5B/
```

Die Hugging-Face-Caches müssen die in Compose festgelegten Snapshots mitsamt
Blobs und relativen Symlinks enthalten. Für CosyVoice wird das vollständige
Modellverzeichnis einschließlich des `llm.pt`-Symlinkziels und
`CosyVoice-BlankEN` benötigt. Compose erstellt oder befüllt dieses Volume nicht.
Modellquellen und Lizenzen sind in der
[Drittanbieter-Übersicht](THIRD_PARTY_LICENSES.md) aufgeführt.

## Sprachapp starten

Alle Befehle werden im Projektroot ausgeführt. Sobald Images, Gewichte und
GPU-Umgebung bereitstehen:

```sh
python3 scripts/setup.py
```

Das Setup prüft die vorhandenen Images und das externe Volume und erzeugt
`.env` sowie Zufallsgeheimnisse unter `.secrets/`. Vorhandene Einstellungen und
Geheimnisse bleiben erhalten. Mit `python3 scripts/setup.py --check` lässt sich
die Prüfung ohne Dateiänderungen wiederholen. Die vollständigen Gewichte und
die GPU-Bereitschaft werden erst beim Modellstart geprüft.

Vor dem Start die lokale `.env` anpassen. Die
[Beispielkonfiguration](.env.example) beschreibt unter anderem Hostnamen,
Browser-Origin, Ports, Images und GPU-Zuordnung. Eigene Adressen gehören in
`.env`, Passwörter und Tokens in `.secrets/`; beide sind von Git ausgeschlossen.

```sh
# Modellcontainer anlegen, ohne die Modelle zu laden:
docker compose create --no-build --pull never asr llm tts

# Oberfläche, Backend und Modellsteuerung starten:
docker compose up -d --no-build --pull never backend frontend model-control
```

Standardmäßig ist die App auf dem Docker-Host unter
**https://localhost:6500** erreichbar. Caddy erzeugt dafür eine lokale
Zertifizierungsstelle. Deren öffentliches Zertifikat exportieren und auf dem
Browsergerät als vertrauenswürdig hinterlegen:

```sh
mkdir -p data
docker compose cp frontend:/data/caddy/pki/authorities/local/root.crt data/sphinx-local-ca.crt
```

Für den Zugriff von anderen Geräten `SITE_ADDRESS`, `PUBLIC_ORIGIN` und
`BIND_ADDRESS` in `.env` anpassen und HTTPS für die gewählte Adresse einrichten.
`PUBLIC_ORIGIN` muss exakt der im Browser verwendeten Origin entsprechen,
einschließlich Schema und gegebenenfalls Port.

Mit **admin** und dem Passwort aus `.secrets/admin_password` anmelden. Das
Bootstrap-Passwort gilt beim Anlegen einer leeren Nutzerdatenbank und kann danach
im Kontomenü geändert werden. **Modelle laden** startet die vorbereiteten
Modellcontainer; **Sprechen** wird freigegeben, sobald alle drei Modelle bereit
sind. Das Laden baut keine Images und lädt keine Gewichte herunter.

## Optional: Dokumentwissen und PDF-Import

Für den reinen Sprachbetrieb ist diese Erweiterung optional. Für Antworten aus
Dokumenten werden PostgreSQL mit pgvector und Ollama benötigt. Die
Ingestionspipeline übernimmt den PDF-Import und benötigt zusätzlich n8n und den
JavaScript-Runner. Bereits importierte Dokumente können auch bei gestopptem n8n
abgefragt werden, solange PostgreSQL und Ollama erreichbar sind.

Vorbereitung:

- Die in Compose festgelegten Images für `vector-db`, `n8n` und `n8n-runner`
  lokal bereitstellen.
- Ollama separat mit `bge-m3:latest` und der in
  [database/01-schema.sql](database/01-schema.sql) festgelegten Modellrevision
  bereitstellen. Import und Suche erwarten 1024-dimensionale Embeddings.
- `OLLAMA_HOST_ADDRESS` in `.env` setzen. Der Standard `host-gateway` adressiert
  den Docker-Host; für einen anderen Rechner oder ein abweichendes
  Rootless-Netzwerk eine aus den Containern erreichbare IP-Adresse verwenden.
  Ollama muss dort auf Port 11434 erreichbar sein. Bei einer Änderung das
  Backend mit `docker compose up -d --no-deps --no-build --pull never backend`
  neu erstellen; ein bereits laufendes n8n ebenfalls neu erstellen.

```sh
python3 scripts/setup_ingestion.py
docker compose up -d --no-build --pull never vector-db n8n n8n-runner
python3 scripts/import_ingestion.py
```

Das Importscript legt fehlende Credentials und den
[PDF-Workflow](n8n/pdf-ingestion.workflow.json) als inaktiven Entwurf an.
Bestehende Einträge bleiben erhalten. Anschließend auf dem Docker-Host
**http://localhost:5678** öffnen, das n8n-Owner-Konto einrichten und den Workflow
testen und veröffentlichen. Danach können Admins in Sphinx PDFs hochladen.

PDFs benötigen extrahierbaren Text; OCR ist nicht enthalten. Das Limit liegt
bei 20 MiB je Datei. Alle angemeldeten Nutzer teilen sich eine Wissensbasis;
es gibt keine dokumentbezogenen Zugriffsrechte. Antworten können den
Quelldateinamen nennen, aber keine Seitenzahlen.

## Datenhaltung

SQLite speichert Konten, Sitzungen und Uploadmetadaten. Gesprächsaudio und
Transkripte werden dort nicht gespeichert; der Gesprächskontext bleibt im RAM
und wird unter anderem bei Logout oder Backend-Neustart verworfen.

Die optionale Wissensdatenbank speichert Dokumentmetadaten, Textabschnitte und
Vektoren. Original-PDFs werden dort nicht archiviert. n8n verwaltet Workflows
und Credentials in einer eigenen SQLite-Datenbank. Die konfigurierte Speicherung
der Ausführungsdaten kann auch extrahierte Texte und Vektoren in der
n8n-Historie vorhalten. PDF-Binärdaten werden separat im Dateisystem des
`n8n_data`-Volumes abgelegt.

Anwendungsdaten, Wissensdatenbank, n8n-Daten und Zertifikate liegen in Docker-
Volumes. Diese müssen bei Updates erhalten bleiben. `docker compose down -v`
löscht die anwendungseigenen persistenten Volumes und eignet sich daher nicht
für einen normalen Neustart.

## Entwicklung und Tests

Die Python-Testabhängigkeiten stehen in
[backend/requirements-test.lock](backend/requirements-test.lock), die
Frontend-Abhängigkeiten in [frontend/package-lock.json](frontend/package-lock.json).
Mit eingerichteter Python-Testumgebung unter `.venv/`, verfügbarem FFmpeg,
installierten Frontend-Abhängigkeiten und Playwright-Browser:

```sh
.venv/bin/python -m pytest backend/tests models/tests
npm --prefix frontend test
npm --prefix frontend run test:browser
npm --prefix frontend run build
```

Unit- und Vertragstests verwenden simulierte Modelldienste und benötigen keine
GPU. Browser-Tests simulieren das Mikrofon. Echte Spracherkennung, Modellstart
und Stimmqualität benötigen zusätzlich Prüfungen mit den laufenden GPU-Diensten.

## Lizenz

Dieses Projekt steht, soweit nicht anders angegeben, unter der
[Apache License 2.0](LICENSE). Copyright 2026 Technische Hochschule Aschaffenburg;
siehe auch [NOTICE](NOTICE).

Eingesetzte Drittanbieter-Software und Modellgewichte unterliegen ihren jeweiligen
Lizenzbedingungen; die Projektlizenz ersetzt diese nicht. Die
[Drittanbieter-Übersicht](THIRD_PARTY_LICENSES.md) nennt die Modell- und
Laufzeitlizenzen, die Einschränkungen von n8n sowie die Herkunft der
mitgelieferten Referenzstimme.
