# TuDirector — Regista AI per i tuoi video

Web app self-hosted: carichi uno o più video, l'AI trascrive l'audio (S2T), descrive i
fotogrammi (1 fps → 800×480), e un **LLM regista** decide su finestre di 30 s le mosse di
regia (zoom, effetti, sovrimpressioni SVG, polaroid, citazioni autori/libri, cambio camera
multi-traccia) che vengono **renderizzate con ffmpeg** nel video finale.

- Piano app: [`docs/PLAN.md`](docs/PLAN.md)
- Pipeline LLM & prompt: [`docs/PIPELINE.md`](docs/PIPELINE.md)

## Requisiti

- Python 3.10+ (venv consigliato), **ffmpeg/ffprobe** nel PATH
- Chiavi API (opzionali — senza chiavi l'app gira in **mock mode**):
  - `OPENROUTER_API_KEY` — vision + regista (https://openrouter.ai/keys)
  - `FAL_KEY` — STT whisper + generazione immagini (https://fal.ai/dashboard/keys)

## Avvio

```bash
cd /home/ubuntu/src/AI/TuDirector
python3 -m venv .venv && . .venv/bin/activate   # se non usi un venv già attivo
pip install -r requirements.txt
cp .env.example .env      # opzionale: inserisci le chiavi
./run.sh                  # server su http://0.0.0.0:18736
```

Dietro un reverse proxy l'app funziona su qualunque sub-path: tutti i riferimenti sono
relativi (`api/…`, `media/…`).

## Struttura

```
docs/            piano app + pipeline LLM
tuddirector/     backend FastAPI (media, stt, vision, director, assets, overlay, render)
  skills/        skill di regia YAML editabili (scenari: auto, a piedi, intervista…)
  prompts/       prompt base editabili (vision, director)
web/            frontend SPA (vanilla JS)
data/            job (upload, frame, trascrizioni, decisioni, output)
```

## Uso

1. **Progetti → Nuovo**: trascina 1 video (o più tracce multi-camera), scegli la skill di
   regia, avvia.
2. Segui la pipeline: estrazione → trascrizione → descrizione frame → regista → render.
3. Nello **Studio**: leggi trascrizione e didascalie, rivedi le decisioni per finestra,
   ri-esegui la regia su una finestra (`redo`), scarica il video finale.
4. Nelle **Skills**: crea/modifica gli stili di regia (YAML validato).
