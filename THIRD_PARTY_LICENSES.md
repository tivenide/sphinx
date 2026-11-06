# Drittanbieter-Lizenzen und Modellgewichte

Stand der Prüfung: 6. November 2026.

Für die eigenen Projektinhalte gilt die [Apache-2.0-Projektlizenz](LICENSE).
Der Copyright-Hinweis der Technischen Hochschule Aschaffenburg steht in
[NOTICE](NOTICE).
Die folgenden Komponenten behalten ihre jeweiligen Lizenzen. Diese Übersicht
erfasst die eingesetzten Modelle und ausgewählte Laufzeitsoftware. Sie ist keine
vollständige Liste aller direkten und transitiven Paket- oder Image-Abhängigkeiten.

## Modellgewichte

Die ASR-, LLM- und TTS-Gewichte werden aus dem externen Volume `ai-models`
eingebunden. BGE-M3 liegt beim separat betriebenen Ollama-Dienst. Das optionale
VoiceDesign-Modell dient nur zur Erstellung der Referenzstimme. Diese Gewichte
werden nicht im Git-Repository mitgeliefert.

| Modell | Verwendung | Lizenz laut Quelle | Quelle |
| --- | --- | --- | --- |
| `Qwen/Qwen3-ASR-0.6B` | Spracherkennung | Apache-2.0 | [Modellkarte der konfigurierten Revision](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/raw/5eb144179a02acc5e5ba31e748d22b0cf3e303b0/README.md) |
| `Qwen/Qwen3-30B-A3B-Instruct-2507` | Textgenerierung | Apache-2.0 | [Lizenz der konfigurierten Revision](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507/raw/0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe/LICENSE) |
| `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` | Sprachausgabe; lokal `Fun-CosyVoice3-0.5B` | Apache-2.0 | [Modellkarte der lokal dokumentierten Revision](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512/raw/29e01c4e8d000f4bcd70751be16fa94bf3d85a18/README.md) |
| `BAAI/bge-m3`, über Ollama als `bge-m3:latest` | Embeddings für Import und Suche | MIT | [Modellkarte](https://huggingface.co/BAAI/bge-m3), [Ollama-Paket mit Lizenzangabe](https://ollama.com/library/bge-m3:latest) |
| `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | Optionale Erstellung der synthetischen Referenzstimme | Apache-2.0 | [Modellkarte der dokumentierten Revision](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign/raw/5ecdb67327fd37bb2e042aab12ff7391903235d3/README.md) |

## Laufzeitsoftware

| Komponente | Verwendung / Stand | Lizenz | Quelle |
| --- | --- | --- | --- |
| n8n und `@n8n/task-runner` | PDF-Import und JavaScript-Code-Nodes; Version 2.46.4, Images in Compose per Digest festgelegt | Sustainable Use License; Enterprise-Bestandteile haben separate Bedingungen | [n8n-Lizenz für 2.46.4](https://github.com/n8n-io/n8n/blob/n8n%402.46.4/LICENSE.md), [Runner-Paketangabe](https://github.com/n8n-io/n8n/blob/n8n%402.46.4/packages/%40n8n/task-runner/package.json) |
| CosyVoice-Code | TTS-Laufzeit; Commit `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` im Runtime-Dockerfile | Apache-2.0; eingebundene Drittkomponenten behalten eigene Lizenzen | [Lizenz dieses Commits](https://github.com/FunAudioLLM/CosyVoice/blob/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc/LICENSE) |
| vLLM | LLM-Inferenz; Version 0.22.1 | Apache-2.0 | [Lizenz für 0.22.1](https://github.com/vllm-project/vllm/blob/v0.22.1/LICENSE) |
| Ollama | Separat bereitgestellter Embedding-Dienst; Serverversion im Repository nicht festgelegt | MIT für das Ollama-Projekt; Modelle separat lizenziert | [Projektlizenz](https://github.com/ollama/ollama/blob/main/LICENSE) |

Die n8n Sustainable Use License erlaubt eigene interne Geschäftszwecke sowie
persönliche oder nichtkommerzielle Nutzung. Weitergabe bzw. Bereitstellung an
Dritte ist unter dieser Lizenz nur unentgeltlich für nichtkommerzielle Zwecke
gestattet. Für einen Vertrieb oder ein Kundenangebot mit n8n muss deshalb das
konkrete Nutzungsmodell gegen die n8n-Bedingungen geprüft und gegebenenfalls eine
passende kommerzielle Vereinbarung getroffen werden. Die Apache-Lizenz des
Sphinx-Codes erweitert die n8n-Nutzungsrechte nicht. Maßgeblich ist der oben
verlinkte Lizenztext der eingesetzten Version.

Weitere Abhängigkeiten sind unter anderem in
[frontend/package-lock.json](frontend/package-lock.json),
[backend/requirements.lock](backend/requirements.lock),
[models/requirements-asr.lock](models/requirements-asr.lock),
[models/requirements-cosyvoice.txt](models/requirements-cosyvoice.txt) und
[models/requirements-llm.txt](models/requirements-llm.txt) dokumentiert.
Hinzu kommen die Pakete der Container-Basisimages. Diese Dateien ersetzen kein
vollständiges Lizenzinventar der gebauten Images.

## Mitgelieferte Audiodateien

Die Datei [sphinx-male-calm.wav](models/voice/sphinx-male-calm.wav) wird sowohl
als TTS-Referenz als auch für den ASR-Warmup verwendet. Die
[Metadaten](models/voice/sphinx-male-calm.json) und das
[Generierungsskript](scripts/generate_voice.py) dokumentieren ihre synthetische
Erzeugung mit Qwen VoiceDesign anhand einer Textbeschreibung; das Skript
verwendet keine Aufnahme einer realen Sprecherperson als Eingabe.

## Weitergabe von Images oder Gewichten

Diese Übersicht verlinkt die geprüften Quellen. Wenn zusätzlich zum Sphinx-Code
Container-Images, Fremdcode oder Modellgewichte weitergegeben werden, müssen die
jeweils erforderlichen Lizenztexte, Copyright- und NOTICE-Angaben sowie etwaige
weitere Pflichten für genau diese Artefakte erfüllt werden. Die Links in dieser
Datei ersetzen keine mitzuliefernden Lizenztexte.
