# The Hunger Games aligned reader

This project builds a local, searchable read-along edition of Suzanne Collins’s
*The Hunger Games* from the included `hunger_games_1.pdf`. Narration is generated with
the female British English Edge-TTS voice `en-GB-SoniaNeural`; word-boundary
timings drive the synchronized highlighting in the browser.

Run the complete, resumable build:

```bash
./run.sh
```

Or run individual stages:

```bash
python3 -m tools.hunger_reader_pipeline extract
python3 -m tools.hunger_reader_pipeline narrate
python3 -m tools.hunger_reader_pipeline build
```

Open `aligned_reader/index.html` after the build. Audio files remain external to
the HTML in `aligned_reader/audio/`, so keep that directory beside the page.
Search is available from the magnifying-glass button or with Ctrl/Cmd+F.
Click a word to play from it; double-click a word to pause at that position.
Use the playback speed selector (0.75×–2×) to adjust narration speed. Your choice
is remembered across chapters and page reloads.

Extraction uses Poppler (`pdftotext`) to detect all 27 chapter starts and remove
page footers. Narration requires `edge-tts`; building requires `ffprobe`.
Cached narration is reused only when its source text, voice, and rate match.
# hunger_game_1
