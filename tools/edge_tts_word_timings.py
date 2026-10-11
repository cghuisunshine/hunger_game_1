#!/usr/bin/env python3
"""Generate Edge-TTS MP3 audio with source-mapped word boundaries."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path


TICKS_PER_SECOND = 10_000_000


def normalize_token(value: str) -> str:
    return re.sub(r"\W+", "", value, flags=re.UNICODE).lower()


def find_span(text: str, token: str, cursor: int) -> tuple[int, int]:
    direct = text.lower().find(token.lower(), cursor)
    if direct >= 0:
        return direct, direct + len(token)
    wanted = normalize_token(token)
    if not wanted:
        return cursor, cursor
    for start in range(cursor, len(text)):
        if not text[start].isalnum():
            continue
        collected: list[str] = []
        end = start
        while end < len(text) and len("".join(collected)) < len(wanted):
            if text[end].isalnum():
                collected.append(text[end].lower())
            end += 1
        if "".join(collected) == wanted:
            while end < len(text) and text[end].isalnum():
                end += 1
            return start, end
    return cursor, min(cursor + len(token), len(text))


async def synthesize(args: argparse.Namespace) -> dict:
    import edge_tts

    text = Path(args.text_file).read_text(encoding="utf-8")
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = output_dir / f"{args.basename}.mp3"
    json_path = output_dir / f"{args.basename}.words.json"
    communicate = edge_tts.Communicate(
        text,
        voice=args.voice,
        rate=args.rate,
        boundary="WordBoundary",
    )
    words = []
    cursor = 0
    with audio_path.open("wb") as audio_file:
        async for message in communicate.stream():
            if message["type"] == "audio":
                audio_file.write(message["data"])
            elif message["type"] == "WordBoundary":
                spoken = message.get("text", "")
                start_char, end_char = find_span(text, spoken, cursor)
                cursor = max(cursor, end_char)
                start = round(float(message.get("offset", 0)) / TICKS_PER_SECOND, 6)
                duration = round(float(message.get("duration", 0)) / TICKS_PER_SECOND, 6)
                words.append(
                    {
                        "text": spoken,
                        "start": start,
                        "end": round(start + duration, 6),
                        "duration": duration,
                        "start_char": start_char,
                        "end_char": end_char,
                    }
                )
    if not words:
        raise RuntimeError("Edge-TTS returned no word-boundary metadata")
    payload = {
        "text": text,
        "voice": args.voice,
        "rate": args.rate,
        "audio": audio_path.name,
        "words": words,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"audio": str(audio_path), "json": str(json_path), "words": len(words)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--basename", default="speech")
    parser.add_argument("--voice", default="en-GB-SoniaNeural")
    parser.add_argument("--rate", default="-5%")
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(synthesize(parse_args())), indent=2))
