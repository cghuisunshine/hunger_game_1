import unittest
from pathlib import Path


READER_HTML = Path(__file__).resolve().parents[1] / "aligned_reader" / "index.html"


class AlignedReaderHtmlTests(unittest.TestCase):
    def test_reader_saves_progress_to_local_storage(self):
        html = READER_HTML.read_text(encoding="utf-8")

        self.assertIn("const progressStorageKey =", html)
        self.assertIn("function saveProgress(", html)
        self.assertIn("localStorage.setItem(progressStorageKey", html)

    def test_reader_restores_saved_progress_on_startup(self):
        html = READER_HTML.read_text(encoding="utf-8")

        self.assertIn("function loadSavedProgress()", html)
        self.assertIn("function loadInitialChapter()", html)
        self.assertIn("loadInitialChapter();", html)

    def test_reader_uses_saved_time_before_audio_metadata_loads(self):
        html = READER_HTML.read_text(encoding="utf-8")

        self.assertIn("function updateTimes(localOverride = null)", html)
        self.assertIn("function resolveLocalTime(localOverride = null)", html)
        self.assertIn("if (pendingStartTime !== null) {", html)
        self.assertIn("pendingStartTime = null;", html)
        self.assertIn("Math.abs(local - pendingStartTime) >= 0.25", html)
        self.assertIn("const local = resolveLocalTime(localOverride);", html)
        self.assertIn("updateTimes(localStart);", html)

    def test_reader_renders_and_highlights_word_spans(self):
        html = READER_HTML.read_text(encoding="utf-8")

        self.assertIn("span.className = 'tts-word';", html)
        self.assertIn("const unit = findPlaybackUnitAt(chapter, local);", html)
        self.assertIn("audio.currentTime = word.start;", html)
        self.assertIn("saveProgress(word.id, word.start);", html)

    def test_reader_supports_prompt_search_to_matched_sentence(self):
        html = READER_HTML.read_text(encoding="utf-8")

        self.assertIn('id="searchButton"', html)
        self.assertIn('aria-label="Search text"', html)
        self.assertIn("function searchSentences(query)", html)
        self.assertIn("function promptSearch()", html)
        self.assertIn("window.prompt('Search text')", html)
        self.assertIn("loadChapter(match.chapterIndex, false, match.sentence.localBegin);", html)
        self.assertIn("scrollToPlaybackUnit(match.sentence.id);", html)
        self.assertIn("searchButton.addEventListener('click', promptSearch);", html)


if __name__ == "__main__":
    unittest.main()
