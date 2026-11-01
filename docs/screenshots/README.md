# Screenshot per il README

Cattura le schermate seguenti e salvale in questa cartella con questi nomi esatti.
Consiglio: finestra del browser a **1440×900**, tema chiaro, con un progetto vero
(elaborato con chiavi API) aperto, così i dati mostrati sono reali.

| File | Cosa catturare | Dove appare nel README |
|---|---|---|
| `studio.png` | Vista **Studio** di un progetto completato: pipeline verde, trascrizione a sinistra, strip fotogrammi a destra, timeline regia sotto (idealmente con una finestra espansa che mostra le decisioni) | riga principale della galleria |
| `projects.png` | Vista **Progetti**: dropzone "Nuovo progetto" con 1–2 video trascinati, selettore skill, lista progetti sotto | galleria, cella 1 |
| `skills.png` | Vista **Skills**: lista skill a sinistra, editor YAML aperto su una skill (es. `intervista`) a destra | galleria, cella 2 |
| `hero-result.jpg` | Un **fotogramma estratto dal video finale renderizzato** in cui si vede un effetto del regista (lower-third, polaroid o sticker). Esempio: `ffmpeg -ss 12 -i output/final.mp4 -frames:v 1 hero-result.jpg` | galleria, riga finale ("The result") |

Opzionali (non referenziate nel README, utili per issue/discussioni):

| File | Cosa catturare |
|---|---|
| `settings.png` | Vista **Impostazioni**: modelli modificabili + diagnostica API |
| `timeline.png` | Zoom sulla **timeline regia** con le decisioni per finestra |

Dopo aver aggiunto i file, elimina la nota "TO ADD BEFORE PUBLISHING" dal README.
