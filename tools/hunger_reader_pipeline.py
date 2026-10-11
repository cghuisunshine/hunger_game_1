from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET


BOOK_FILE = Path("hunger_games_1.pdf")
OUTPUT_DIR = Path("aligned_reader")
TTS_SCRIPT = Path(__file__).with_name("edge_tts_word_timings.py")
VOICE = "en-GB-SoniaNeural"


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def join_pdf_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        # The PDF wraps compound words at existing hyphens.
        separator = "" if text.endswith("-") else " "
        text += (separator if text else "") + line
    return normalize_text(text.replace("\u00ad", ""))


def extract_chapters(book_file: Path, output_dir: Path) -> list[dict]:
    if not book_file.exists():
        raise FileNotFoundError(book_file)
    # Bounding boxes retain paragraph gaps and distinguish image-based chapter
    # headings from ordinary pages. No OCR or ebook conversion is needed.
    result = subprocess.run(
        ["pdftotext", "-bbox-layout", str(book_file), "-"],
        check=True, text=True, stdout=subprocess.PIPE,
    )
    pages = ET.fromstring(result.stdout).findall(".//{*}page")
    chapter_data: list[dict] = []
    current = None
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines and current is not None:
            current["paragraphs"].append(join_pdf_lines(paragraph_lines))
            paragraph_lines.clear()

    for page_number, page in enumerate(pages, start=1):
        # Footers begin below 570pt in this source edition.
        lines = [line for line in page.findall(".//{*}line")
                 if float(line.attrib["yMin"]) < 570]
        if not lines:
            continue
        first_y = float(lines[0].attrib["yMin"])
        if first_y > 200:  # Part title pages and END OF BOOK ONE.
            flush_paragraph()
            continue
        if 70 < first_y < 110:
            flush_paragraph()
            current = {"number": len(chapter_data) + 1,
                       "sourceStartPage": page_number,
                       "sourceEndPage": page_number, "paragraphs": []}
            chapter_data.append(current)
        if current is None:
            raise RuntimeError(f"Unexpected body text before Chapter 1 on page {page_number}")
        current["sourceEndPage"] = page_number
        previous_y = None
        for line in lines:
            y = float(line.attrib["yMin"])
            if previous_y is not None and y - previous_y > 20:
                flush_paragraph()
            value = " ".join(word.text or "" for word in line.findall("{*}word"))
            paragraph_lines.append(value)
            previous_y = y
        # A short final line ending a sentence is a paragraph boundary, even
        # when the next paragraph starts on a new page. Otherwise carry it over.
        final_text = paragraph_lines[-1] if paragraph_lines else ""
        if (float(lines[-1].attrib["xMax"]) < 300
                and re.search(r'[.!?…][”’"\']?$', final_text)):
            flush_paragraph()
    flush_paragraph()
    if len(chapter_data) != 27:
        raise RuntimeError(f"Expected 27 chapters, found {len(chapter_data)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    text_dir = output_dir / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    chapters = []
    for chapter in chapter_data:
        number = chapter["number"]
        paragraphs = chapter["paragraphs"]
        if not paragraphs:
            raise RuntimeError(f"No text found for Chapter {number}")
        heading = f"Chapter {number}."
        speech_text = heading + "\n\n" + "\n\n".join(paragraphs)
        records = []
        cursor = len(heading) + 2
        for index, paragraph in enumerate(paragraphs, start=1):
            records.append({"id": f"c{number:03d}_p{index:04d}", "text": paragraph,
                            "startChar": cursor, "endChar": cursor + len(paragraph)})
            cursor += len(paragraph) + 2
        text_path = text_dir / f"chapter_{number:03d}.txt"
        text_path.write_text(speech_text, encoding="utf-8")
        chapters.append({**chapter, "title": f"Chapter {number}",
                         "textFile": text_path.relative_to(output_dir).as_posix(),
                         "paragraphs": records})
    metadata = {"title": "The Hunger Games", "author": "Suzanne Collins",
                "source": book_file.name, "voice": VOICE, "chapters": chapters}
    (output_dir / "chapters.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "book.txt").write_text(
        "\n\n\n".join((output_dir / chapter["textFile"]).read_text(encoding="utf-8")
                           for chapter in chapters), encoding="utf-8")
    return chapters


def narration_command(
    chapter: dict,
    output_dir: Path,
    tts_script: Path,
    voice: str,
    rate: str,
) -> tuple[int, str]:
    number = chapter["number"]
    audio_dir = output_dir / "audio"
    timing_dir = output_dir / "timings"
    basename = f"chapter_{number:03d}"
    audio_path = audio_dir / f"{basename}.mp3"
    generated_json = audio_dir / f"{basename}.words.json"
    timing_path = timing_dir / f"{basename}.words.json"
    source_text = (output_dir / chapter["textFile"]).read_text(encoding="utf-8")
    if audio_path.exists() and audio_path.stat().st_size > 1024 and timing_path.exists():
        try:
            cached = json.loads(timing_path.read_text(encoding="utf-8"))
            if (cached.get("words") and cached.get("voice") == voice
                    and cached.get("rate") == rate and cached.get("text") == source_text):
                return number, "cached"
        except (json.JSONDecodeError, OSError):
            pass

    command = [
        sys.executable,
        str(tts_script),
        "--text-file",
        str(output_dir / chapter["textFile"]),
        "--out-dir",
        str(audio_dir),
        "--basename",
        basename,
        "--voice",
        voice,
        "--rate",
        rate,
    ]
    env = os.environ.copy()
    dependency_dir = Path(__file__).resolve().parents[1] / ".deps"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(dependency_dir), env.get("PYTHONPATH", "")])
    )
    for attempt in range(3):
        try:
            subprocess.run(command, check=True, env=env, stdout=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    timing_dir.mkdir(parents=True, exist_ok=True)
    generated_json.replace(timing_path)
    return number, "generated"


def narrate(
    output_dir: Path,
    tts_script: Path = TTS_SCRIPT,
    voice: str = VOICE,
    rate: str = "-5%",
    workers: int = 3,
    chapter_range: Sequence[int] | None = None,
) -> None:
    metadata_path = output_dir / "chapters.json"
    if not metadata_path.exists():
        raise RuntimeError("Run the extract step before narration.")
    if not tts_script.exists():
        raise FileNotFoundError(f"Edge-TTS timing script was not found at {tts_script}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    chapters = metadata["chapters"]
    if chapter_range is not None:
        first, last = chapter_range
        chapters = [chapter for chapter in chapters if first <= chapter["number"] <= last]
        if not chapters:
            raise ValueError(f"No chapters in range {first}-{last}")
    (output_dir / "audio").mkdir(parents=True, exist_ok=True)
    (output_dir / "timings").mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(narration_command, chapter, output_dir, tts_script, voice, rate): chapter
            for chapter in chapters
        }
        for future in as_completed(futures):
            number, status = future.result()
            print(f"Chapter {number:02d}: {status}", flush=True)
    metadata["voice"] = voice
    metadata["rate"] = rate
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def ffprobe_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return float(result.stdout.strip())


def build_manifest(output_dir: Path) -> dict:
    metadata = json.loads((output_dir / "chapters.json").read_text(encoding="utf-8"))
    manifest = {
        "title": metadata["title"],
        "author": metadata["author"],
        "voice": metadata["voice"],
        "duration": 0.0,
        "chapters": [],
    }
    global_offset = 0.0
    for chapter in metadata["chapters"]:
        number = chapter["number"]
        audio_path = output_dir / "audio" / f"chapter_{number:03d}.mp3"
        timing_path = output_dir / "timings" / f"chapter_{number:03d}.words.json"
        if not audio_path.exists() or not timing_path.exists():
            raise FileNotFoundError(f"Missing narration assets for Chapter {number}")
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        source_text = (output_dir / chapter["textFile"]).read_text(encoding="utf-8")
        if timing.get("text") != source_text or timing.get("voice") != metadata["voice"]:
            raise RuntimeError(f"Stale narration for Chapter {number}; run narrate again.")
        duration = ffprobe_duration(audio_path)
        words = timing["words"]
        paragraphs = []
        for paragraph in chapter["paragraphs"]:
            paragraph_words = []
            for word_index, word in enumerate(words):
                if paragraph["startChar"] <= word["start_char"] < paragraph["endChar"]:
                    paragraph_words.append(
                        {
                            "id": f"{paragraph['id']}_w{len(paragraph_words) + 1:04d}",
                            "text": word["text"],
                            "start": word["start"],
                            "end": word["end"],
                            "startChar": word["start_char"] - paragraph["startChar"],
                            "endChar": min(word["end_char"], paragraph["endChar"])
                            - paragraph["startChar"],
                        }
                    )
            begin = paragraph_words[0]["start"] if paragraph_words else 0.0
            end = paragraph_words[-1]["end"] if paragraph_words else begin
            paragraphs.append(
                {
                    "id": paragraph["id"],
                    "text": paragraph["text"],
                    "localBegin": begin,
                    "localEnd": end,
                    "words": paragraph_words,
                }
            )

        manifest["chapters"].append(
            {
                "kind": "chapter",
                "number": number,
                "title": chapter["title"],
                "audio": f"audio/chapter_{number:03d}.mp3",
                "start": round(global_offset, 3),
                "end": round(global_offset + duration, 3),
                "duration": round(duration, 3),
                "paragraphs": paragraphs,
            }
        )
        global_offset += duration
    manifest["duration"] = round(global_offset, 3)
    return manifest


def build_reader(output_dir: Path) -> None:
    manifest = build_manifest(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "index.html").write_text(build_reader_html(manifest), encoding="utf-8")


def build_reader_html(manifest: dict) -> str:
    manifest_json = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(manifest["title"])
    author = html.escape(manifest["author"])
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f5f5f4">
  <link rel="icon" href="data:,">
  <title>{title} — Aligned Reader</title>
  <style>
    :root {{ color-scheme: light; --bg:#f5f5f4; --surface:#fff; --line:#d7d3cc; --text:#1c1917; --muted:#6b6258; --soft:#f0eee9; --active:#ffe9a8; --accent:#8a4b16; --button:#292524; }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif; font-size:16px; line-height:1.65; }}
    button,input {{ font:inherit; }}
    button:focus-visible,input:focus-visible {{ outline:3px solid rgba(138,75,22,.26); outline-offset:2px; }}
    .app {{ min-height:100vh; display:grid; grid-template-columns:256px minmax(0,1fr); }}
    aside {{ height:100vh; position:sticky; top:0; overflow:auto; border-right:1px solid var(--line); background:var(--surface); padding:20px 14px 100px; }}
    .book-title {{ margin:0; font-size:16px; font-weight:750; line-height:1.3; }}
    .author {{ margin:3px 0 18px; color:var(--muted); font-size:14px; }}
    .chapter-list {{ display:flex; flex-direction:column; gap:2px; }}
    .chapter-link {{ border:0; border-radius:6px; background:transparent; color:var(--text); cursor:pointer; padding:7px 9px; text-align:left; }}
    .chapter-link:hover {{ background:var(--soft); }}
    .chapter-link.active {{ background:#e7e1d8; font-weight:700; }}
    main {{ min-width:0; padding:28px 30px 116px; }}
    .topbar {{ max-width:820px; margin:0 auto 18px; display:flex; align-items:baseline; justify-content:space-between; gap:16px; }}
    h1 {{ margin:0; font-size:22px; line-height:1.3; }}
    .time {{ color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; font-size:14px; }}
    .reader {{ max-width:820px; margin:0 auto; padding:30px 36px; border:1px solid var(--line); border-radius:8px; background:var(--surface); }}
    .paragraph {{ margin:0 0 1em; padding:2px 5px; border-left:3px solid transparent; border-radius:4px; cursor:pointer; }}
    .paragraph:hover {{ background:#faf8f5; }}
    .tts-word {{ border-radius:3px; cursor:pointer; transition:background-color 120ms ease,color 120ms ease; }}
    .tts-word:hover {{ background:#f0eee9; }}
    .tts-word.is-speaking {{ background:var(--active); color:#111827; }}
    .player {{ position:fixed; left:256px; right:0; bottom:0; z-index:4; display:grid; grid-template-columns:auto auto auto minmax(120px,1fr) auto; align-items:center; gap:10px; padding:12px 20px; border-top:1px solid var(--line); background:rgba(255,255,255,.97); }}
    .control {{ min-height:36px; padding:0 12px; border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--text); cursor:pointer; font-weight:700; }}
    .control:hover {{ background:var(--soft); }}
    .control.primary {{ min-width:70px; border-color:var(--button); background:var(--button); color:#fff; }}
    input[type="range"] {{ width:100%; accent-color:var(--accent); }}
    .search-button {{ position:fixed; right:20px; bottom:76px; z-index:5; width:44px; height:44px; display:grid; place-items:center; border:1px solid var(--button); border-radius:8px; background:var(--button); color:#fff; cursor:pointer; }}
    .search-button:hover {{ background:#44403c; }}
    .search-button svg {{ width:20px; height:20px; stroke:currentColor; }}
    @media (max-width:760px) {{
      .app {{ display:block; }} aside {{ position:static; height:auto; max-height:230px; border-right:0; border-bottom:1px solid var(--line); padding-bottom:16px; }}
      main {{ padding:20px 14px 126px; }} .reader {{ padding:22px 18px; }} .topbar {{ align-items:flex-start; flex-direction:column; gap:4px; }}
      .player {{ left:0; grid-template-columns:auto auto auto; }} .player input[type="range"] {{ grid-column:1/-1; }} .search-button {{ right:14px; bottom:116px; }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <p class="book-title">{title}</p>
      <p class="author">{author}</p>
      <nav class="chapter-list" id="chapterList" aria-label="Chapters"></nav>
    </aside>
    <main>
      <div class="topbar"><h1 id="chapterTitle"></h1><div class="time" id="timeLabel">0:00 / 0:00</div></div>
      <article class="reader" id="reader"></article>
    </main>
  </div>
  <div class="player">
    <button class="control" id="prevButton" type="button">Prev</button>
    <button class="control primary" id="playButton" type="button">Play</button>
    <button class="control" id="nextButton" type="button">Next</button>
    <input id="seekBar" type="range" min="0" max="1000" value="0" aria-label="Seek">
    <span class="time" id="chapterTime">0:00</span>
  </div>
  <button class="search-button" id="searchButton" type="button" aria-label="Search text" title="Search (Ctrl/Cmd+F)">
    <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="m16.5 16.5 4 4"></path></svg>
  </button>
  <audio id="audio" preload="metadata"></audio>
  <script>
    const manifest = {manifest_json};
    const audio = document.getElementById('audio');
    const chapterList = document.getElementById('chapterList');
    const reader = document.getElementById('reader');
    const chapterTitle = document.getElementById('chapterTitle');
    const playButton = document.getElementById('playButton');
    const prevButton = document.getElementById('prevButton');
    const nextButton = document.getElementById('nextButton');
    const seekBar = document.getElementById('seekBar');
    const timeLabel = document.getElementById('timeLabel');
    const chapterTime = document.getElementById('chapterTime');
    const searchButton = document.getElementById('searchButton');
    const progressStorageKey = 'hunger-games-aligned-reader-progress-v1';
    let currentIndex = 0;
    let currentParagraphId = null;
    let pendingStartTime = null;

    function formatTime(seconds) {{
      seconds = Math.max(0, Math.floor(seconds || 0));
      const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
      return h ? `${{h}}:${{String(m).padStart(2,'0')}}:${{String(s).padStart(2,'0')}}` : `${{m}}:${{String(s).padStart(2,'0')}}`;
    }}

    function renderNav() {{
      chapterList.textContent = '';
      manifest.chapters.forEach((chapter, index) => {{
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'chapter-link' + (index === currentIndex ? ' active' : '');
        button.textContent = chapter.title;
        button.addEventListener('click', () => loadChapter(index, true));
        chapterList.appendChild(button);
      }});
    }}

    function wordItems(chapter) {{
      return chapter.paragraphs.flatMap((paragraph) => paragraph.words);
    }}

    function findPlaybackUnitAt(chapter, local) {{
      for (const paragraph of chapter.paragraphs) {{
        const word = paragraph.words.find((item) => local >= item.start && local < item.end);
        if (word) return word;
      }}
      return null;
    }}

    function findParagraphByPlaybackId(chapter, playbackId) {{
      return chapter.paragraphs.find((paragraph) => paragraph.id === playbackId || paragraph.words.some((word) => word.id === playbackId)) || null;
    }}

    function searchSentences(query) {{
      const needle = query.trim().toLowerCase();
      if (!needle) return null;
      const matches = [];
      manifest.chapters.forEach((chapter, chapterIndex) => {{
        chapter.paragraphs.forEach((paragraph) => {{
          const lower = paragraph.text.toLowerCase();
          let from = 0;
          while (true) {{
            const found = lower.indexOf(needle, from);
            if (found < 0) break;
            const word = paragraph.words.find((item) => item.endChar > found) || paragraph.words[0];
            if (word) matches.push({{ chapterIndex, sentence: {{ id: word.id, localBegin: word.start }} }});
            from = found + Math.max(needle.length, 1);
          }}
        }});
      }});
      if (!matches.length) return null;
      const chapter = manifest.chapters[currentIndex];
      const globalTime = chapter.start + resolveLocalTime();
      return matches.find((match) => manifest.chapters[match.chapterIndex].start + match.sentence.localBegin > globalTime + .01) || matches[0];
    }}

    function scrollToPlaybackUnit(playbackId) {{
      document.getElementById(playbackId)?.scrollIntoView({{ block:'center', behavior:'smooth' }});
    }}

    function promptSearch() {{
      const query = window.prompt('Search text');
      if (query === null) return;
      const match = searchSentences(query);
      if (!match) {{ window.alert('No match found.'); return; }}
      pendingStartTime = null;
      loadChapter(match.chapterIndex, false, match.sentence.localBegin);
      requestAnimationFrame(() => {{
        scrollToPlaybackUnit(match.sentence.id);
      }});
      saveProgress(match.sentence.id, match.sentence.localBegin);
    }}

    function saveProgress(paragraphId = currentParagraphId, localTime = audio.currentTime || 0) {{
      if (!paragraphId) return;
      try {{ localStorage.setItem(progressStorageKey, JSON.stringify({{ chapterIndex:currentIndex, paragraphId, localTime }})); }} catch {{}}
    }}

    function loadSavedProgress() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(progressStorageKey) || 'null');
        if (!saved || !Number.isInteger(saved.chapterIndex)) return null;
        const chapter = manifest.chapters[saved.chapterIndex];
        if (!chapter || !findParagraphByPlaybackId(chapter, saved.paragraphId)) return null;
        return {{ chapterIndex:saved.chapterIndex, paragraphId:saved.paragraphId, localTime:Number(saved.localTime) || 0 }};
      }} catch {{ return null; }}
    }}

    function renderParagraph(paragraph) {{
      const node = document.createElement('p');
      node.className = 'paragraph';
      node.id = paragraph.id;
      let cursor = 0;
      paragraph.words.forEach((word) => {{
        if (word.startChar > cursor) node.append(document.createTextNode(paragraph.text.slice(cursor, word.startChar)));
        const span = document.createElement('span');
        span.className = 'tts-word';
        span.id = word.id;
        span.textContent = paragraph.text.slice(word.startChar, word.endChar);
        span.addEventListener('click', (event) => {{
          event.stopPropagation();
          pendingStartTime = null;
          audio.currentTime = word.start;
          updateHighlight(word.start);
          saveProgress(word.id, word.start);
          audio.play();
        }});
        node.append(span);
        cursor = word.endChar;
      }});
      if (cursor < paragraph.text.length) node.append(document.createTextNode(paragraph.text.slice(cursor)));
      node.addEventListener('click', () => {{
        const word = paragraph.words[0];
        if (!word) return;
        pendingStartTime = null;
        audio.currentTime = word.start;
        updateHighlight(word.start);
        saveProgress(word.id, word.start);
        audio.play();
      }});
      return node;
    }}

    function loadChapter(index, autoplay = false, startTime = 0) {{
      currentIndex = Math.max(0, Math.min(index, manifest.chapters.length - 1));
      const chapter = manifest.chapters[currentIndex];
      currentParagraphId = null;
      pendingStartTime = null;
      audio.src = chapter.audio;
      chapterTitle.textContent = chapter.title;
      reader.textContent = '';
      chapter.paragraphs.forEach((paragraph) => reader.appendChild(renderParagraph(paragraph)));
      renderNav();
      const localStart = Math.max(0, Math.min(Number(startTime) || 0, chapter.duration));
      if (localStart > 0) {{ pendingStartTime = localStart; try {{ audio.currentTime = localStart; }} catch {{}} updateTimes(localStart); }} else updateTimes();
      if (autoplay) audio.play();
    }}

    function resolveLocalTime(localOverride = null) {{
      if (typeof localOverride === 'number' && Number.isFinite(localOverride)) return localOverride;
      if (pendingStartTime !== null) return pendingStartTime;
      const local = Number(audio.currentTime);
      return Number.isFinite(local) ? local : 0;
    }}

    function updateTimes(localOverride = null) {{
      const chapter = manifest.chapters[currentIndex];
      const local = resolveLocalTime(localOverride);
      timeLabel.textContent = `${{formatTime(chapter.start + local)}} / ${{formatTime(manifest.duration)}}`;
      chapterTime.textContent = `${{formatTime(local)}} / ${{formatTime(chapter.duration)}}`;
      seekBar.value = chapter.duration ? String(Math.round(local / chapter.duration * 1000)) : '0';
      updateHighlight(local);
    }}

    function restorePendingStartTime() {{
      if (pendingStartTime === null) {{ updateTimes(); return; }}
      const startTime = pendingStartTime;
      audio.currentTime = startTime;
      updateTimes(startTime);
    }}

    function updateHighlight(local) {{
      const chapter = manifest.chapters[currentIndex];
      const unit = findPlaybackUnitAt(chapter, local);
      const nextId = unit ? unit.id : null;
      if (nextId === currentParagraphId) return;
      if (currentParagraphId) document.getElementById(currentParagraphId)?.classList.remove('is-speaking');
      currentParagraphId = nextId;
      if (currentParagraphId) {{
        document.getElementById(currentParagraphId)?.classList.add('is-speaking');
        if (!audio.paused) scrollToPlaybackUnit(currentParagraphId);
        saveProgress(currentParagraphId, local);
      }}
    }}

    function loadInitialChapter() {{
      const saved = loadSavedProgress();
      if (saved) loadChapter(saved.chapterIndex, false, saved.localTime); else loadChapter(0, false);
    }}

    playButton.addEventListener('click', () => audio.paused ? audio.play() : audio.pause());
    prevButton.addEventListener('click', () => loadChapter(currentIndex - 1, !audio.paused));
    nextButton.addEventListener('click', () => loadChapter(currentIndex + 1, !audio.paused));
    searchButton.addEventListener('click', promptSearch);
    document.addEventListener('keydown', (event) => {{
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {{ event.preventDefault(); promptSearch(); }}
    }});
    seekBar.addEventListener('input', () => {{
      const chapter = manifest.chapters[currentIndex];
      const local = Number(seekBar.value) / 1000 * chapter.duration;
      const unit = findPlaybackUnitAt(chapter, local);
      pendingStartTime = null;
      audio.currentTime = local;
      updateHighlight(local);
      saveProgress(unit?.id, local);
    }});
    audio.addEventListener('play', () => {{ playButton.textContent = 'Pause'; requestAnimationFrame(function paint() {{ updateTimes(); if (!audio.paused && !audio.ended) requestAnimationFrame(paint); }}); }});
    audio.addEventListener('pause', () => playButton.textContent = 'Play');
    audio.addEventListener('timeupdate', () => updateTimes());
    audio.addEventListener('loadedmetadata', restorePendingStartTime);
    audio.addEventListener('seeked', () => {{
      if (pendingStartTime !== null) {{
        const local = Number(audio.currentTime);
        if (!Number.isFinite(local) || Math.abs(local - pendingStartTime) >= 0.25) {{ updateTimes(pendingStartTime); return; }}
        pendingStartTime = null;
      }}
      updateTimes();
    }});
    audio.addEventListener('ended', () => {{ if (currentIndex < manifest.chapters.length - 1) loadChapter(currentIndex + 1, true); }});
    loadInitialChapter();
  </script>
</body>
</html>
'''


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build The Hunger Games aligned reader.")
    parser.add_argument("command", choices=["extract", "narrate", "build", "all"])
    parser.add_argument("--book", type=Path, default=BOOK_FILE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--tts-script", type=Path, default=TTS_SCRIPT)
    parser.add_argument("--voice", default=VOICE)
    parser.add_argument("--rate", default="-5%")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--chapters", type=int, nargs=2, metavar=("FIRST", "LAST"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in {"extract", "all"}:
        chapters = extract_chapters(args.book, args.output_dir)
        print(f"Extracted {len(chapters)} chapters", flush=True)
    if args.command in {"narrate", "all"}:
        narrate(args.output_dir, args.tts_script, args.voice, args.rate, args.workers, args.chapters)
    if args.command in {"build", "all"}:
        build_reader(args.output_dir)
        print(f"Reader ready: {args.output_dir / 'index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
