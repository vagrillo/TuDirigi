# TuDirector — Piano e Pipeline LLM

Documento tecnico che descrive la pipeline di chiamate ai modelli AI, i prompt di base,
i tool di regia e le skill. Provider preferiti: **OpenRouter.ai** (testo/visione/structured
output) e **fal.ai** (S2T Whisper, generazione immagini).

---

## 1. Schema generale della pipeline

```
video (1..N tracce)
   │
   ├─▶ [ffmpeg] audio.wav ──▶ fal.ai whisper ──▶ segments[{start,end,text}]
   │
   ├─▶ [ffmpeg] frames 1fps 800×480 ──▶ OpenRouter vision ──▶ captions[{t,text,shot}]
   │                                             (batch 8–12 frame / richiesta)
   ▼
finestre da 30 s (sliding, step 30 s, overlap 5 s)
   │  input finestra:
   │   - transcript della finestra (con timecode)
   │   - captions dei frame (campionate, con timecode)
   │   - summary finestra precedente (continuità narrativa)
   │   - skill attiva (scenario) + registry tools + limiti
   ▼
LLM regista (OpenRouter, structured output JSON Schema strict)
   ▼
directions[] (decisioni tipizzate) ──▶ validazione/clamp ▼
assets (Openverse/Wikimedia/fal flux) ▼
overlay PNG (cairosvg/Pillow)          ▼
[ffmpeg render] ──▶ output/final.mp4
```

### Modelli di default (configurabili in UI / `.env`)

| Ruolo | Provider | Default | Note |
|---|---|---|---|
| STT | fal.ai | `fal-ai/whisper` | `chunk_level=segment` → timecode; alt: `fal-ai/wizper` |
| Vision (img2txt) | OpenRouter | `google/gemini-2.5-flash` | economico, ottimo su frame; alt: `qwen/qwen2.5-vl-72b-instruct`, `openai/gpt-4o-mini` |
| Regista | OpenRouter | `anthropic/claude-sonnet-4.5` | structured output robusto; alt: `google/gemini-2.5-pro`, `openai/gpt-4.1` |
| Generazione immagini (fallback asset) | fal.ai | `fal-ai/flux/schnell` | veloce/economico per polaroid & copertine |
| (Opz.) TTS voce narrante | fal.ai | — | roadmap |

Le scelte sono in `config.py` / tab Impostazioni e per‑job.

---

## 2. Stage 1 — S2T (fal.ai)

- Input: `audio.wav` (mono 16 kHz per ridurre payload).
- Chiamata: `fal-ai/whisper` con `task=transcribe`, `chunk_level=segment`, `version=3`.
- Output normalizzato: `[{start, end, text}]`.
- **Post-processing**: fusione dei segmenti < 1 s, punteggiatura mantenuta, mappa parole→finestra.
- Mock: genera segmenti da uno script di demo (usato anche per i test E2E).

## 3. Stage 2 — Vision 1 fps (OpenRouter)

- Frame ogni 1 s ridimensionati **800×480** (jpeg q=80) — come da specifica.
- **Batching**: 10 frame per richiesta (rimane sotto i limiti di rate e di dimensione payload;
  ~10 richieste/minuto di video). Concorrenza regolabile (`VISION_CONCURRENCY`, default 3).
- Prompt di sistema vision (`prompts/vision.md`): descrizione **televisiva** in 1–2 frasi:
  soggetto, azione, inquadratura (wide/medium/close), presenza volti, testo leggibile,
  movimento camera, elementi rilevanti per la regia.
- Output per frame: `{"t": 12, "caption": "...", "shot": "medium", "faces": 1, "motion": "pan-right"}`.
  Parsing tollerante: se il modello non restituisce JSON si salva la didascalia testuale.
- **Riduzione costi**: i frame vicini (< 0.3 ssim) sono deduplicati; cache hash sul contenuto.

## 4. Stage 3 — Il Regista LLM (cuore del sistema)

### 4.1 Finestratura

- **Modalità `fixed`** (default): finestre da **30 s**, step 30 s, con **overlap di 5 s** dai limiti per continuità.
- **Modalità `scene`** (opzionale, `TD_WINDOW_MODE=scene`): la scene detection ffmpeg
  (`select=gt(scene,t)`) individua i confini di scena e la finestra preferisce un confine
  dentro la tolleranza [win_min, win_max] (default 18–48 s) → ogni finestra contiene scene
  intere, decisioni più coerenti.
- Ogni finestra raccoglie: transcript (con t), captions campionate (1 ogni 2 s), summary precedente.
- Per ogni finestra una chiamata LLM separata con **structured output** → parallelizzabile
  (`DIRECTOR_CONCURRENCY`, default 2) e ri-eseguibile singolarmente dalla UI.

### 4.2 Registry dei tool di regia

Il prompt del regista elenca i **tool** disponibili con la loro firma JSON; il modello
"chiama" i tool tramite l'array `decisions[]` dello structured output. Ogni tool ha:
id, descrizione, parametri, vincoli (durata max, conflitti), esempi.

| Tool | Parametri chiave | Effetto render |
|---|---|---|
| `zoom` | `mode in/out`, `cx,cy` (0..1), `strength` (0..0.6), `dur`, easing | crop animato + scale |
| `pan` | `dx,dy`, `dur` | crop con deriva |
| `effects` | `brightness,saturation,contrast` (−1..1), `vignette`, `grayscale`, `sepia` | filtri eq/vignette/hue |
| `text_overlay` | `svg` (markup completo), `t,dur`, `anim: fade|slide-right|slide-up|typewriter` | SVG→PNG + overlay animato |
| `polaroid` | `query`, `caption`, `pos`, `rotate`, `t,dur` | ricerca immagine → PNG polaroid → overlay |
| `citation_card` | `kind: author|book`, `query`, `t,dur` | ricerca autore/copertina → card overlay |
| `sticker_anim` | `kind: car|arrow|star`, `path: ltr|rtl|down-up`, `t,dur`, `size` | PNG animato con path lineare |
| `camera_switch` | `track`, `transition: cut|fade`, `dur` | switch multi‑traccia (xfade) |
| `lower_third` | `title`, `subtitle`, `t,dur` | SVG banner elegante |
| `highlight_pulse` | `cx,cy`, `t`, `dur` | zoom pulse rapido |
| `slowmo` | `t,dur,factor (0.5–0.9)` | **rallenty reale**: trim+setpts video e atrim+atempo audio sincronizzati; la durata totale aumenta di dur·(1−factor); le decisioni successive vengono rimappate sul nuovo tempo |
| `narration` | `text`, `speed`, `t` | voce narrante **TTS** (fal.ai) mixata nell'audio con adelay+amix |

### 4.3 JSON Schema (estratto dello structured output)

```jsonc
{
  "name": "director_window",
  "strict": true,
  "schema": {
    "type": "object",
    "properties": {
      "summary":   {"type": "string"},
      "mood":      {"type": "string", "enum": ["neutro","giocoso","drammatico","tensione","info","epico"]},
      "decisions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "type":  {"type":"string","enum":["zoom","pan","effects","text_overlay","polaroid",
                      "citation_card","sticker_anim","camera_switch","lower_third",
                      "highlight_pulse","slowmo"]},
            "t":     {"type":"number"},       // inizio relativo all'inizio del video (assoluto)
            "dur":   {"type":"number"},       // durata
            "params":{"type":"object"}        // parametri specifici del tool
          },
          "required": ["type","t","dur","params"]
        }
      }
    },
    "required": ["summary","mood","decisions"]
  }
}
```

I `params` sono validati lato server contro la firma del tool; valori fuori range → clamp
+ log. Le decisioni fuori finestra (t < t_start−overlap) vengono scartate.

### 4.4 Prompt di base (componibile)

Il prompt del regista = `prompts/director.md` (ruolo+regole) **+** skill YAML attiva
(persona/scenario) **+** blocco tecnico (durata video, tracce disponibili, tool registry,
limiti) **+** dati finestra. Estratto del ruolo:

> Sei un regista/montatore esperto. Ricevi la trascrizione con timecode e le descrizioni
> dei fotogrammi di una finestra da 30 s di un video (forse multi-camera). Decidi le mosse
> di regia per rendere il video coinvolgente: usa i tool disponibili con parsimonia e
> intenzione. Se il parlato chiede attenzione/ausilio visivo (es. "guardate", "come vedete
> qui", nomi, numeri), supporta con overlay, polaroid o zoom. Se lo speaker cita ripetutamente
> un autore/libro, mostra autore o copertina. Se esistono più tracce e la seconda mostra ciò
> di cui si parla, passa a quella con fade. Mai più di N overlay simultanei; le decisioni
> devono rispettare la finestra e non sovrapporsi in modo caotico.

Varianti di scenario (in YAML, editabili):

```yaml
# skills/in_auto.yaml
name: "Regista — In macchina"
scenario: in_auto
description: "Dashboard cam / guida: enfasi su strada, sviate, ostacoli e commenti dello speaker."
system_extra: |
  Il video è girato da un veicolo in movimento. Privilegia: zoom su elementi della strada
  quando commentati, sticker frecce per indicare sviate, effetti leggeri (aumento leggero
  saturazione nelle ore diurne), polaroid per fatti citati. Evita slowmo prolungati.
tools: [zoom, pan, effects, text_overlay, polaroid, sticker_anim, camera_switch, lower_third]
limits: {max_simultaneous_overlays: 2, max_zoom_strength: 0.4}
style: {pacing: "dinamico", tone: "info"}
preset_effects: {saturation: 0.08, contrast: 0.05}
```

Altri: `base.yaml`, `a_piedi.yaml`, `intervista.yaml`, `viaggio.yaml`, `cucina.yaml`,
`sport.yaml`, `conferenza.yaml`.

### 4.5 Continuità e multi‑camera

- Il summary della finestra precedente è iniettato nella successiva → decisioni coerenti.
- Se il job ha ≥ 2 tracce: le captions di ogni traccia sono etichettate `[CAM 0]`… e il
  regista sa di poter usare `camera_switch`; il render applica cut/xfade ai tempi decisi.
- Le tracce secondarie sono analizzate con lo stesso stage vision ma con sampling più rado
  (1 frame / 2 s) per contenere i costi.

## 5. Stage 4 — Assets (polaroid, citazioni, sticker)

Ordine di risoluzione per ogni `polaroid`/`citation_card`:

1. **Openverse API** (senza chiave, filtri licenza) → prima immagine pertinente ≥ 800 px;
2. **Wikimedia Commons** search (per autori, libri, luoghi) → file thumbnail;
3. **fal.ai flux/schnell** generazione text2img (se 1–2 falliscono o `assets_policy=generate`).

Le immagini sono salvate in `assets/<window>/`, verifiche (dimensione, NSFW flag Openverse),
poi trasformate in PNG "polaroid" (cornice bianca, rotazione, caption, ombra) via Pillow.

## 6. Stage 5 — Overlay/SVG

- Il regista può produrre SVG completo per `text_overlay` (viewBox 800×480, font sistemici,
  testo evidente). Server: **sanitizzazione** (solo svg/text/tspan/path/rect/circle/g/…,
  niente script/foreignObject), poi `cairosvg` → PNG con alpha.
- Il modello deve "dire che path deve fare": l'animazione è dichiarata nel tool
  (`anim: slide-right|fade|typewriter|draw`); per `draw` il render usa una maschera alpha
  progressiva (v1: fade su path con stroke‑dasharray simulato).
- Fallback: se l'SVG è rotto, viene rigenerato da un template (`lower_third` / `title`).

## 7. Stage 6 — Render (ffmpeg)

- Un solo pass per traccia base: catena di filtri
  `eq → crop/zoom animato → overlay xN → format=yuv420p`.
- Zoom: `crop` con espressioni su `t` (finestra animata) + `scale` al master 1280×720.
- Overlay animati: input PNG con `fade` alpha in/out + `overlay` con espressioni `x(t)`.
- Polaroid: PNG pre‑composto (Pillow) con rotazione/ombra.
- Sticker: `overlay` con `x(t)` lineare (ltr/rtl).
- Multi‑camera: le tracce sono renderizzate in segmenti e unite con `xfade`/`concat`
  secondo le `camera_switch` (offset allineati all'inizio comune).
- Audio: la traccia 0 domina; eventuali beep/ducking in roadmap.
- Output: `H.264 + AAC, 1280×720@25, faststart` (configurabile).

## 8. Osservabilità, costi e cache

- Ogni chiamata LLM/fal è loggata in `data/jobs/<id>/logs/api.log` (provider, modello,
  token, latenza, costo stimato dal pricing OpenRouter se disponibile).
- La UI mostra per ogni finestra: input (transcript+captions), decisioni, ragioni, costo.
- Ri-esecuzione di singola finestra (`redo`) per iterare senza rifare tutto.
- **Cache su disco** (`data/cache/`):
  - `vision/` — didascalia per **hash del contenuto del frame** + modello: un job
    ri-eseguito (o frame identici tra video) non ripaga il modello;
  - `stt/` — trascrizione per **hash dell'audio** + modello.

## 9. Slowmo reale, fps sorgente e sincronia A/V

Il rallenty è implementato con una **timeline segmentata**: ogni decisione `slowmo`
prende `dur × factor` secondi di sorgente e li espande a `dur` secondi di uscita
(video `trim + setpts / factor`, audio `atrim + atempo factor`), quindi:

- video e audio restano **sincronizzati** (nessun freeze);
- la durata totale **aumenta** di `dur × (1 − factor)` (nessun contenuto scartato);
- tutte le decisioni successive vengono **rimappate** sul nuovo tempo di uscita;
- nel percorso multi-camera il rallenty resta approssimato a freeze-frame (v2.1).

Per la sincronia A/V:

- **fps di uscita = fps della sorgente** (impostazione `out_fps: 0` = auto): nessuna
  conversione forzata (es. 30→25) che può dare percezione di labiale fuori sync;
- **`audio_offset_ms`**: compensazione manuale del desync della sorgente
  (+ ritarda la voce, − la anticipa) applicata nel filtergraph (adelay/atrim);
- gli **overlay sono ancorati alla fascia bassa** dello schermo (polaroid, citazioni,
  sticker, banner): non coprono il soggetto; il prompt del regista impone la stessa
  regola per gli SVG;
- **varietà di camera**: il prompt impone di alternare zoom e pan (il pan ampio e
  lento è il "guardarsi attorno"), massimo 2 zoom per finestra.

## 10. Roadmap LLM (v3)

- **Scene detection** ffmpeg (`select=gt(scene,t)`) per finestre "semantiche" — ✅ fatto (modalità `scene`).
- **Rallenty reale** con sincronia A/V — ✅ fatto (timeline segmentata).
- **TTS narratore** (fal.ai) — ✅ fatto (tool `narration`).
- **Cache** vision/STT su hash — ✅ fatto (`data/cache/`).
- Slowmo reale anche nel percorso multi-camera.
- **Reranking delle didascalie** con un secondo modello economico.
- **Audio ducking** automatico sotto le narrazioni.
- **Fine‑tuning delle skill** dai feedback 👍/👎 dell'utente sulle decisioni.

## 11. Spotlight, preview e editing del montaggio (v3)

### Spotlight sul soggetto
- La vision estrae anche `subject_box` (x,y,w,h normalizzati) del soggetto principale
  per ogni fotogramma (cache key v3).
- Nuovo tool `spotlight` (`mode=color`: resto B/N, soggetto a colori; `mode=dim`:
  resto scurito di `dim`, soggetto normale). Il regista indica centro/raggi
  dall'analisi; il server **riposiziona comunque l'ellisse** dalle subject_box
  (mediana nella finestra) — `fill_spotlight_from_box`.
- Render: base = frame trattato; patch = crop colore del soggetto con alpha
  dall'ellisse sfumata (`alphamerge`, solo luma → nessun mix di croma);
  overlay attivo solo nella finestra con passthrough originale fuori.

### Preview prima del render finale
- `POST api/jobs/{id}/preview {t0, dur}` → anteprima **400px** (proporzioni
  mantenute), preset ultrafast, solo le decisioni che intersecano la finestra
  (shiftate sul nuovo tempo). Stage `preview` dedicato; ripristina lo stato del job.
- Nella UI card Output: inizio + durata (20/30/60s) + "⚡ Preview 400px".

### Editing con linguaggio naturale
- `POST api/jobs/{id}/window/{wid}/edit {query}` → il regista riscrive le decisioni
  della finestra applicando la richiesta (es. "allunga lo zoom a 6 secondi").
  Validazione anti-vuoti e guard anti-overlap restano attivi.

### Merge finestre in un effetto unico
- `POST api/jobs/{id}/merge {windows:[...], query?}` → unisce finestre contigue
  (60/90/120s) ri-eseguendo il regista sull'intero span con max_decisions amplificato.
  Nella UI: checkbox sulle finestre → barra "⧉ Unisci".
