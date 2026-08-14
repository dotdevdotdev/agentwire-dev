#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "python-dotenv"]
# ///
"""Measure nonce-candidate spelling stability under the REAL transcriber.

The admission test for ``confirm.NONCE_WORDS`` (documented at the tuple) is
"one TRANSCRIBER RENDERING each" — a claim about ``gpt-4o-mini-transcribe``,
not about orthography, and two of the original twenty words failed it
(``harbor`` -> ``harbour``, ``ripcord`` -> ``rip cord``). #1039 adopts the NATO
phonetic alphabet as the base vocabulary, and this probe is the measurement:
every candidate is synthesized inside the real phrase ("confirm <word>") at
several macOS voices, sent to the real transcription model, and admitted only
if EVERY transcription renders it as exactly its canonical token after
``confirm.normalize``.

Synthesized speech is not the owner's voice, but the property under test is the
transcriber's orthographic prior (which spelling it emits for a word it heard),
not acoustic robustness — and a word that can't survive clean synthesized audio
has no business in the alphabet.

Usage: ``tools/nonce_stt_probe.py [--rounds N]`` (default 3 voices x 1 round).
Requires OPENAI_API_KEY in ``~/.agentwire/.env`` and macOS ``/usr/bin/say``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from dotenv import load_dotenv

# NATO alphabet, canonical ICAO spellings, plus the spellings the issue names
# as suspected hazards so the failure is measured rather than assumed.
CANDIDATES = [
    "alfa", "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
    "hotel", "india", "juliett", "juliet", "kilo", "lima", "mike", "november",
    "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform", "victor",
    "whiskey", "xray", "x-ray", "yankee", "zulu",
]

VOICES = ["Samantha", "Daniel", "Karen"]

_PUNCT_RE = re.compile(r"[^\w\s]+")


def normalize(text: str) -> str:
    return _PUNCT_RE.sub(" ", text.lower()).strip()


def transcribe(path: Path, key: str) -> str:
    with path.open("rb") as fh:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (path.name, fh, "audio/wav")},
            data={"model": "gpt-4o-mini-transcribe", "language": "en"},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["text"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()

    load_dotenv(Path.home() / ".agentwire" / ".env")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY not found", file=sys.stderr)
        return 1

    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for word in CANDIDATES:
            spoken = word.replace("-", " ")
            renderings: list[str] = []
            for voice in VOICES:
                for round_index in range(args.rounds):
                    wav = Path(tmp) / f"{word}-{voice}-{round_index}.wav"
                    subprocess.run(
                        ["/usr/bin/say", "-v", voice, "-o", str(wav),
                         "--data-format=LEI16@16000", f"confirm {spoken}"],
                        check=True,
                    )
                    text = transcribe(wav, key)
                    renderings.append(text)
            tokens_per = [normalize(t).split() for t in renderings]
            # The nonce token(s) are whatever follows "confirm" in each
            # transcription; the admission test is that this is exactly the
            # canonical one-token spelling, every time.
            tails = []
            for tokens in tokens_per:
                try:
                    idx = tokens.index("confirm")
                    tails.append(" ".join(tokens[idx + 1:]))
                except ValueError:
                    tails.append(" ".join(tokens))
            canonical = normalize(word).replace(" ", "")
            passed = all(t == canonical for t in tails)
            results[word] = {
                "pass": passed,
                "renderings": sorted(set(renderings)),
                "tails": sorted(set(tails)),
            }
            status = "PASS" if passed else "FAIL"
            print(f"{status:4} {word:10} tails={sorted(set(tails))}")

    passing = [w for w, r in results.items() if r["pass"]]
    print("\npassing:", passing)
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
