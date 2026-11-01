# TuDirector — Piano dell'Applicazione

> "Il regista AI per i tuoi video": carichi un (o più) video, l'AI analizza audio e immagini,
> e un LLM‑regista decide le mosse di regia (zoom, effetti, sovrimpressioni, polaroid,
> citazioni, cambio fotocamera) che vengono poi renderizzate sul video finale.

---

## 1. Visione

TuDirector è un'appllicazione web self‑hosted che simula il lavoro di un regista montatore:

1. **Ingest** — l'utente carica 1 traccia video (o più tracce, es. multi‑camera dal telefono).
2. **Analisi** — il sistema estrae audio → testo con timecode (S2T) e descrive ogni fotogramma
   (1 fps, ridimensionato 800×480) con un modello img2txt.
3. **Regia** — un LLM con *structured output* JSON, a finestre di 30 secondi, decide le azioni
   di regia usando un **registro di tools** (zoom, effetti colore, SVG overlay, polaroid,
   citazioni, cambio camera…).
4. **Render** — una pipeline ffmpeg traduce le decisioni in filtri video e produce il MP4 finale.
5. **Revisione** — l'utente vede la timeline delle decisioni, può modificare le skill di regia
   (YAML editabili) e ri‑eseguire la regia su singole finestre.

Tutto il server è **Python (FastAPI) su porta 18736**, con riferimenti URL **sempre relativi**
per funzionare anche dietro reverse proxy (RP) montato su qualunque sub‑path.

---

## 2. Architettura

```
                         ┌──────────────────────────────────────────────┐
   Browser  ────HTTP────▶│  FastAPI  :18736   (tutti gli URL relativi)  │
   (SPA vanilla JS)      │  ┌──────────┐ ┌───────────┐ ┌─────────────┐  │
                         │  │ /ui      │ │ /api      │ │ /media      │  │
                         │  │ static   │ │ jobs REST │ │ file job    │  │
                         │  └──────────┘ └─────┬─────┘ └─────────────┘  │
                         └──────────────────────┼────────────────────────┘
                                                │ JobManager (async, stage pipeline)
                ┌───────────────────────────────┼────────────────────────────────┐
                ▼                 ▼             ▼              ▼                 ▼
          ┌──────────┐    ┌─────────────┐ ┌──────────┐ ┌────────────┐   ┌────────────┐
          │ media.py │    │ stt.py      │ │ vision.py│ │ director.py│   │ render.py  │
          │ ffmpeg   │    │ fal.ai      │ │ OpenRouter│ │ OpenRouter │   │ ffmpeg     │
          │ ffprobe  │    │ whisper     │ │ vision   │ │ + skills   │   │ filtergraph│
          └──────────┘    └─────────────┘ └──────────┘ └─────┬──────┘   └────────────┘
                                                             │
                                     ┌───────────────────────┼──────────────────┐
                                     ▼                       ▼                  ▼
                               skills/*.yaml          assets.py           prompts/*.md
                               (regia editabile)      Openverse/Wikimedia (prompt base
                                                      + fal.ai flux       editabili)
```

### Componenti backend (`tuddirector/`)

| Modulo | Responsabilità |
|---|---|
| `main.py` | App FastAPI, montaggio static, ciclo di vita, avvio su `:18736` |
| `config.py` | Settings (pydantic‑settings): chiavi API, modelli, parametri pipeline, `.env` |
| `jobs.py` | JobManager: creazione job, avanzamento stage, stato su disco (`data/jobs/<id>/job.json`) |
| `media.py` | ffprobe, estrazione audio (WAV), estrazione frame 1 fps 800×480, contact sheet |
| `stt.py` | S2T con **fal.ai** (whisper/wizper) → segmenti `{start,end,text}`; modalità mock |
| `vision.py` | img2txt con **OpenRouter** (modelli vision); batching frame; cache per frame |
| `llm.py` | Client OpenRouter con **structured output** (json_schema strict), retry, logging |
| `director.py` | Finestre da 30 s, composizione prompt (base + skill + contesto), parsing decisioni |
| `skills.py` | Caricamento/validazione/salvataggio skill YAML (`skills/`), registry scenari |
| `assets.py` | Ricerca immagini (Openverse, Wikimedia) e generazione **fal.ai** (flux) per polaroid/citazioni |
| `overlay.py` | SVG → PNG (cairosvg), polaroid PNG (Pillow), PNG animazioni (car, frecce…) |
| `render.py` | Costruzione filtergraph ffmpeg: zoompan/crop animato, eq, overlay, fade, xfade multicam |
| `routes_*.py` | API REST + pagine UI |

### Frontend (`web/` — SPA vanilla, nessun build step)

- `index.html` — shell a tab: **Progetti**, **Studio** (job), **Skills**, **Impostazioni**.
- `app.js` — logica client; tutte le fetch usano path **relativi** (`api/...`, `media/...`).
- `style.css` — tema dark "editing suite".

---

## 3. Modello dati

### Job su disco: `data/jobs/<job_id>/`

```
job.json               # stato, metadata, avanzamento stage, timeline decisioni
source/track-0.mp4     # tracce caricate (track-0 = principale)
audio/track-0.wav
frames/track-0/f000000.jpg   # 1 fps, 800×480
descriptions/track-0.json    # [{t, caption}]
transcript/track-0.json      # [{start,end,text}]
windows/w003.json            # input+output regista per ogni finestra 30 s
assets/w003/polaroid-1.jpg   # immagini trovate/generate
overlays/w003/*.svg|png
output/final.mp4             # render finale
thumb.jpg                    # anteprima card
```

### Timeline decisioni (structured output del regista)

```jsonc
{
  "window_id": "w003",
  "t_start": 60.0, "t_end": 90.0,
  "summary": "Lo speaker descrive l'incidente visto lungo la strada",
  "mood": "drammatico",
  "decisions": [
    {"type":"zoom","t":62.0,"dur":2.5,"mode":"in","cx":0.5,"cy":0.42,"strength":0.35,
     "reason":"il parlato chiede attenzione al volto"},
    {"type":"polaroid","t":64.0,"dur":5.0,"query":"auto cappottata incidente",
     "pos":"right","caption":"L'auto cappottata", "rotate": -6},
    {"type":"text_overlay","t":70.0,"dur":4.0,"svg":"<svg>…</svg>",
     "anim":{"path":"fade"} },
    {"type":"camera_switch","t":80.0,"track":1,"transition":"fade","dur":1.0,
     "reason":"lo speaker indica qualcosa alle spalle → camera posteriore"}
  ]
}
```

Catalogo decisioni (v1): `zoom`, `pan`, `effects`, `text_overlay`, `polaroid`,
`citation_card`, `sticker_anim`, `camera_switch`, `lower_third`, `highlight_pulse`, `slowmo`.

---

## 4. API REST (tutte relative)

| Metodo | Path | Descrizione |
|---|---|---|
| `GET`  | `api/health` | stato server, chiavi configurate, versione |
| `POST` | `api/jobs` | crea job (multipart: file video 1..N + opzioni) |
| `GET`  | `api/jobs` | lista job |
| `GET`  | `api/jobs/{id}` | stato completo (stage, progress, timeline) |
| `POST` | `api/jobs/{id}/run` | avanza/riparte la pipeline |
| `POST` | `api/jobs/{id}/cancel` | annulla |
| `GET`  | `api/jobs/{id}/window/{w}` | dettaglio finestra (input LLM + output) |
| `POST` | `api/jobs/{id}/window/{w}/redo` | ri-esegue la regia su una finestra (con eventuali note utente) |
| `PATCH`| `api/jobs/{id}/decision/{i}` | modifica manuale di una decisione |
| `POST` | `api/jobs/{id}/render` | lancia il render finale |
| `GET`  | `api/skills` / `POST` / `PUT` / `DELETE` | gestione skill YAML |
| `GET`  | `api/settings` / `PUT` | lettura/scrittura config runtime (modelli, parametri) |
| `GET`  | `api/pipeline` | documentazione viva della pipeline (stadi, modelli, costi stimati) |
| `GET`  | `media/jobs/{id}/…` | file statici del job (video, frame, output) |

## 5. Pipeline di elaborazione (stadi del job)

| # | Stage | Output | Provider |
|---|---|---|---|
| 0 | `probe`   | durata, fps, risoluzione, tracce | ffprobe |
| 1 | `extract` | WAV audio + frame 1 fps 800×480 + contact sheet | ffmpeg |
| 2 | `stt`     | segmenti con timecode | **fal.ai** whisper/wizper |
| 3 | `vision`  | didascalia per frame (raggruppate per scena) | **OpenRouter** vision |
| 4 | `director`| decisioni JSON per finestra 30 s | **OpenRouter** structured output |
| 5 | `assets`  | immagini polaroid/citazioni | Openverse/Wikimedia + **fal.ai** flux |
| 6 | `overlay` | PNG da SVG, polaroid, sticker | cairosvg + Pillow |
| 7 | `render`  | `output/final.mp4` | ffmpeg |

Ogni stage è ri‑avviabile e indipendente; lo stato è interrogabile in polling
(`GET api/jobs/{id}`), con percentuali e log per stage.

## 6. Modalità mock (sviluppo senza chiavi)

Se `OPENROUTER_API_KEY` / `FAL_KEY` non sono impostate, i moduli girano in **mock mode**:
- STT: segmenti fittizi realistici costruiti da uno script dimostrativo;
- vision: didascalie generate da heuristics (colore medio, movimento, presenza volti via OpenCV‑free heuristics);
- regista: decisioni demo variate per scenario;
- assets: immagini placeholder generate con Pillow.

Così l'app è sempre testabile end‑to‑end e la UI si sviluppa senza costi API.

## 7. Scenari & Skills

Le **skill** sono file YAML in `tuddirector/skills/` che configurano il "personaggio" del regista:
`name`, `description`, `scenario`, `system_extra` (prompt addizionale), `tools` (whitelist),
`limits` (max overlay per finestra, max zoom…), `style` (pacing, tono), `preset_effects`.

Scenari forniti: `base`, `in_auto` (in macchina), `a_piedi` (camminata/città),
`intervista` (speaker fisso), `cucina`, `viaggio`, `sport`, `conferenza`.
La UI consente di crearli/modificarli (editor YAML con validazione).

## 8. Milestone

| M | Contenuto | Stato |
|---|---|---|
| M1 | Scheletro server + upload + job manager + UI shell | ✅ |
| M2 | Estrazione media (audio, frame) + probe | ✅ |
| M3 | STT fal.ai + vision OpenRouter (+ mock) | ✅ |
| M4 | Regista LLM structured output + skill YAML | ✅ |
| M5 | Assets (ricerca/generazione immagini) | ✅ |
| M6 | Renderer ffmpeg completo (zoom, eq, overlay, polaroid, multicam) | ✅ |
| M7 | UI Studio (timeline, trascrizione, didascalie, redo finestra) | ✅ |
| M8 | v2: slowmo reale A/V, scene detection, TTS narratore, cache vision/STT | ✅ |
| M9 | Ottimizzazioni: parall. director, ducking, reranking didascalie | ⏳ |

## 9. Sicurezza & note operative

- Il server è pensato per LAN/dev dietro RP; le chiavi API risiedono in `.env` (mai nel repo).
- Upload limit configurabile (`MAX_UPLOAD_MB`, default 2 GB).
- `data/` è cache‑abile: i frame e le descrizioni sono riutilizzati se il job viene ri‑aperto.
- Tutti gli endpoint e asset usano percorsi relativi → deployabile sotto `https://host/tuddirector/`.
