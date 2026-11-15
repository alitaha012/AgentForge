"""Speech to text, on this machine.

Hermes ships `tts` but nothing that listens, so transcription is ours to do.
It runs locally through faster-whisper: the recording never leaves the
machine, which is the same promise the rest of the app makes about team data.

The engine is optional. A missing package must not stop the server from
starting or break any other feature, so everything here is probed at call
time and reported as a sentence the person can act on, never as a traceback.

Model size is `AGENTFORGE_WHISPER_MODEL` (default `tiny`, the only size that
is comfortable on a CPU without a usable GPU). `base` is noticeably better
and roughly twice the work; `small` is better still and too slow to dictate
into on most laptops.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

_MODEL = None
_MODEL_NAME = ""
_LOCK = threading.Lock()          # loading is slow; two callers must not race

# Whisper invents speech from silence: these are the canonical hallucinations
# it emits for an empty clip, and passing them on as a question would be
# worse than saying nothing was heard.
_EMPTY = {
    "", ".", "you", "thank you", "thanks for watching", "thank you.",
    "thanks for watching.", "bye", "bye.", "subscribe", "music",
    "[blank_audio]", "[silence]", "[music]",
}


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def how_to_install() -> str:
    return ("Voice input needs the faster-whisper package. Install it with: "
            "pip install --default-timeout=120 --retries 10 -r "
            "requirements-voice.txt  — the long timeout matters, because the "
            "download is large and pip's default gives up partway on a slow "
            "link. The first recording then fetches a small model once, "
            "about 75MB, and everything after that is local.")


def model_name() -> str:
    return os.getenv("AGENTFORGE_WHISPER_MODEL", "tiny").strip() or "tiny"


def _load():
    """Load once and keep it. The first call pays the download and the load."""
    global _MODEL, _MODEL_NAME
    want = model_name()
    with _LOCK:
        if _MODEL is not None and _MODEL_NAME == want:
            return _MODEL
        from faster_whisper import WhisperModel
        # int8 on CPU: the difference against float32 is inaudible for
        # dictation and it is several times faster, which is the whole
        # question on a machine without a usable GPU.
        _MODEL = WhisperModel(want, device="cpu", compute_type="int8")
        _MODEL_NAME = want
        return _MODEL


def decode(path: str, rate: int = 16000):
    """Read any browser recording into the mono float32 Whisper expects.

    faster-whisper has its own reader, but it calls PyAV with an argument
    that recent PyAV versions removed, so the pair that pip installs today
    raises a TypeError on the first clip. Decoding here means the version of
    PyAV underneath stops mattering: anything PyAV can open, this can read.
    """
    import av
    import numpy as np

    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("that recording has no audio track")
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        # flush whatever the resampler is still holding
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks).astype(np.float32) / 32768.0
    return pcm


def transcribe(path: str, language: Optional[str] = None) -> Dict[str, Any]:
    """Turn a recording into text.

    `language` may be given to skip detection, which is both faster and more
    reliable for short clips — Whisper's detector is unsteady on a few
    seconds of speech, and this app is used in Arabic as well as English.
    """
    audio = decode(path)
    seconds = round(len(audio) / 16000.0, 1)
    if seconds < 0.3:
        return {"text": "", "language": "", "seconds": seconds,
                "model": model_name()}

    model = _load()
    segments, info = model.transcribe(
        audio,
        language=language or None,
        vad_filter=True,              # drop silence rather than hallucinate
        beam_size=1,                  # dictation does not need a search
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    if text.lower().strip(" .") in {e.strip(" .") for e in _EMPTY}:
        text = ""
    return {
        "text": text,
        "language": getattr(info, "language", "") or "",
        "seconds": seconds,
        "model": _MODEL_NAME,
    }
