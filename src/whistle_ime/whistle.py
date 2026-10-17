"""Whistle speech-to-text through Cactus Compute's Needle engine (libneedle.so), with word timestamps.

The engine and the weights are separate downloads, both Apache-2.0 (huggingface.co/Cactus-Compute): the
engine from the needle3 repo's Linux wheel, the weights from the whistle repo. The Nix package pins both and
passes their paths in WHISTLE_IME_LIB and WHISTLE_IME_WEIGHTS.
"""
from __future__ import annotations

import ctypes
import json
import os

import numpy

LANGUAGES = ("en", "de", "fr", "es", "it", "nl", "pl")
LIMIT_SECONDS = 30  # the engine refuses longer audio
SAMPLE_RATE = 16000
# Double quotation marks, in the languages above. The model adds them when it guesses speech is a quotation,
# which dictation never asks for. Apostrophes (' and ’) stay: they are part of words.
QUOTES = '"“”„«»'


def without_quotes(word):
    """The word without quotation marks, or None when one closes it. The engine times a word by its last token,
    and a closing quote's time can be a second or more late ('much."' at 3.12 s where every other pass said
    2.12 s), so the word would vote in the wrong place; the passes around it place it right."""
    word = word.strip()
    if word and word[-1] in QUOTES:
        return None
    return word.translate(str.maketrans("", "", QUOTES))


class Whistle:
    """One loaded model. The engine keeps a single model per process and is not thread-safe."""

    def __init__(self, lib_path=None, weights_path=None, buffer_size=1 << 18):
        lib_path = lib_path or os.environ["WHISTLE_IME_LIB"]
        weights_path = weights_path or os.environ["WHISTLE_IME_WEIGHTS"]
        self.lib = ctypes.CDLL(lib_path)
        floats = ctypes.POINTER(ctypes.c_float)
        self.lib.needle_load.argtypes = [ctypes.c_char_p, ctypes.c_uint64]
        self.lib.needle_load.restype = ctypes.c_int
        # samples, count, language, keywords (newline-separated), word timestamps, out buffer, its size
        self.lib.needle_transcribe.argtypes = [floats, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
                                               ctypes.c_char_p, ctypes.c_int]
        self.lib.needle_transcribe.restype = ctypes.c_int
        self.lib.needle_last_error.argtypes = []
        self.lib.needle_last_error.restype = ctypes.c_char_p
        with open(weights_path, "rb") as handle:
            data = handle.read()
        if self.lib.needle_load(data, len(data)) < 0:
            raise RuntimeError(self._error())
        self.buffer = ctypes.create_string_buffer(buffer_size)

    def _error(self):
        return self.lib.needle_last_error().decode("utf-8", "replace")

    def words(self, samples, language=None, keywords=()):
        """[(word, end seconds into samples, probability)] for 16 kHz mono float samples in [-1, 1]."""
        samples = numpy.ascontiguousarray(samples, numpy.float32)
        assert samples.ndim == 1 and len(samples) <= LIMIT_SECONDS * SAMPLE_RATE, samples.shape
        assert language is None or language in LANGUAGES, language
        if len(samples) == 0:
            return []
        code = self.lib.needle_transcribe(
            samples.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), len(samples),
            language.encode("utf-8") if language else None,
            "\n".join(keywords).encode("utf-8") if keywords else None,
            1, self.buffer, len(self.buffer))
        if code < 0:
            raise RuntimeError(self._error())
        result = json.loads(self.buffer.value.decode("utf-8", "replace"))
        words = [(without_quotes(w["word"]) or "", w["end"], w["probability"]) for w in result.get("words", [])]
        return [(text, end, probability) for text, end, probability in words if text]
