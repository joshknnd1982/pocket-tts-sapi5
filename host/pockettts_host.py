"""Pocket TTS SAPI5 host process.

Keeps the Kyutai pocket-tts model warm in memory and serves speech,
voice-cloning and voice-management requests over a framed TCP protocol on
localhost. Both the 32-bit and 64-bit SAPI5 engine DLLs and the Voice
Manager utility are clients of this process.

Protocol: every frame is  <uint32 type><uint32 payload_size><payload>,
little-endian, strings are UTF-8 length-prefixed inside payloads.
"""

import ctypes
import hashlib
import json
import logging
import math
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

# The bundled Python runtime has a python310._pth file, and with one Python
# leaves the folder of the script off sys.path, so the module that sits next
# to this file would not be found without this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pockettts_models as catalog  # noqa: E402
from pockettts_text import speakable  # noqa: E402

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    override = os.environ.get("POCKETTTS_DATA_DIR")
    if override:
        return Path(override)
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return Path(program_data) / "PocketTTS"


def _local_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    d = Path(local) / "PocketTTS"
    d.mkdir(parents=True, exist_ok=True)
    return d


DATA_DIR = _data_dir()
MODELS_DIR = DATA_DIR / "models"
VOICES_DIR = DATA_DIR / "voices"
VOICES_SRC_DIR = VOICES_DIR / "src"
VOICES_INI = VOICES_DIR / "voices.ini"
# SHA-256 of the model weights the voices were built with, before 1.3.0 kept
# it per voice. Read once, for voices that have no model_sha256 of their own.
VOICES_MODEL_STAMP = VOICES_DIR / "model.sha256"
# Engine options the Voice Manager sets (see Settings).
SETTINGS_FILE = DATA_DIR / "engine.json"

PORTS = (17853, 17854, 17855, 17856, 17857)
PORT_FILE = _local_dir() / "host.port"
LOG_FILE = _local_dir() / "host.log"

# Kept in step with CMakeLists.txt and installer/pockettts.iss.
APP_VERSION = "1.3.0"

# How a voice's embedding is made from its audio sample. 1: pocket-tts 3.0.2.
# 2: pocket-tts 4.x, which ends the sample on a short pause first, so the
# model does not carry on the sample's last word at the start of every chunk.
# A voice built with an older recipe is rebuilt from its sample.
EMBEDDING_RECIPE = 2

# Models kept in memory at once. The standard model takes about 500 MB of
# RAM, a small one far less; the least recently used one is let go.
MAX_LOADED_MODELS = 3

# Commands
CMD_PING = 0
CMD_LIST_VOICES = 1
CMD_SPEAK = 2
CMD_STOP = 3
CMD_CLONE = 4
CMD_SET_PUBLISHED = 5
CMD_DELETE_VOICE = 6
CMD_UPDATE_MODELS = 7
CMD_SHUTDOWN = 8
CMD_INFO = 9
CMD_EXPORT_VOICES = 10
CMD_IMPORT_VOICES = 11
CMD_INSPECT_PACKAGE = 12
CMD_LIST_MODELS = 13
CMD_REMOVE_MODEL = 14
CMD_SET_VOICE_MODEL = 15
CMD_GET_SETTINGS = 16
CMD_SET_SETTINGS = 17

# CMD_SPEAK flags
SPEAK_SPELL = 1   # name each character (SAPI's SpellOut action)

# Responses
RESP_OK = 0
RESP_ERROR = 1
RESP_AUDIO = 2
RESP_AUDIO_END = 3
RESP_VOICES = 4
RESP_PONG = 5
RESP_PROGRESS = 6
RESP_INFO = 7
RESP_PACKAGE = 8
RESP_MODELS = 9
RESP_SETTINGS = 10

# The model's native output format, matching src/host_protocol.h.
POCKETTTS_SAMPLE_RATE = 24000

logger = logging.getLogger("pockettts_host")

# ---------------------------------------------------------------------------
# Voice packages (.pttsvoices): sharing voices between computers
# ---------------------------------------------------------------------------
#
# A package is a plain zip holding manifest.json, one voices/<name>.safetensors
# per voice and, when the sender chose to include them, the audio samples in
# src/. The manifest records a fingerprint of the AI model the embeddings were
# produced with: a voice only sounds right on the model that made it, so an
# import re-embeds from the audio sample whenever the two models differ.

# Format 2 adds each voice's AI model (model_id, model_sha256). It is only
# written when a voice uses a model other than the standard English one, so
# a package of standard voices still opens in versions before 1.3.0.
PACKAGE_FORMAT = 2
PACKAGE_KIND = "pocket-tts-voice-package"
PACKAGE_EXT = ".pttsvoices"
PACKAGE_MANIFEST = "manifest.json"
PACKAGE_MAX_MEMBER_BYTES = 512 * 1024 * 1024
PACKAGE_MAX_VOICES = 500
# An import writes at most this much, however many voices claim to be in
# the package: several manifest entries may name one huge archive member.
PACKAGE_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
# The extension of a packaged sample becomes part of a file name and of a
# voices.ini value, so only these are carried across.
_AUDIO_SUFFIXES = frozenset(
    [".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma"])

PACKAGE_README = """Pocket TTS voice package
========================

This file is a voice package written by the Pocket TTS SAPI5 Voice Manager. It
holds one or more Pocket TTS voices: the voice embedding of each voice, in
voices/*.safetensors, and, when the sender included them, the audio samples the
voices were made from, in src/.

To install these voices, open the Pocket TTS Voice Manager on a Windows PC that
has Pocket TTS SAPI5 installed, choose "Import Voices", and pick this file. You
can also simply open this file. If the AI model on that PC differs from the
sender's, the Voice Manager rebuilds each voice automatically, as long as the
audio sample is included here.

Pocket TTS SAPI5:  https://github.com/joshknnd1982/pocket-tts-sapi5
Pocket TTS engine and models, by Kyutai:
                   https://github.com/kyutai-labs/pocket-tts

Consent
-------
Kyutai's use policy prohibits cloning or impersonating a person's voice without
that person's explicit and lawful consent. Only share and install voices the
speaker has agreed to. The default voices (Alba, Jane, George and Michael) come
from Kyutai's kyutai/tts-voices dataset; see that repository for their
individual licenses.
"""


# ---------------------------------------------------------------------------
# Generation speed
# ---------------------------------------------------------------------------
#
# How much audio this machine produces per second of work: 2.5 on a fast
# desktop, well under 1.0 on an old laptop. A client that plays audio the
# moment it arrives runs dry on the slow machines and the speech breaks up,
# so the figure is reported with every PONG and AUDIO_END frame and the SAPI
# engine uses it to decide how much audio to bank before playback starts.
#
# Time spent handing audio to the client is excluded: that is the client's
# playback rate, not this machine's generation rate.

class SpeedTracker:
    def __init__(self, alpha=0.3):
        self.alpha = alpha
        self.value = 0.0        # 0.0 = not measured yet
        self.lock = threading.Lock()

    def update(self, audio_ms, work_ms):
        if audio_ms < 200 or work_ms <= 0:
            return              # too short to say anything about throughput
        factor = audio_ms / work_ms
        with self.lock:
            self.value = (factor if self.value <= 0.0 else
                          self.alpha * factor + (1.0 - self.alpha) * self.value)

    def get(self) -> float:
        with self.lock:
            return self.value

    def packed(self) -> bytes:
        return struct.pack("<f", self.get())


SPEED = SpeedTracker()


# ---------------------------------------------------------------------------
# Voice store (voices.ini, UTF-16-LE so GetPrivateProfileStringW reads it)
# ---------------------------------------------------------------------------

class VoiceStore:
    """voices.ini: one section per voice.

    Keys: file (relative safetensors), gender (Male/Female), language (hex
    LCID, e.g. 409), published (0/1), source (relative audio in src/ used to
    re-embed the voice after a model update; may be missing).
    """

    def __init__(self):
        self.lock = threading.Lock()

    def read(self) -> dict:
        voices = {}
        if not VOICES_INI.exists():
            return voices
        try:
            text = VOICES_INI.read_text(encoding="utf-16")
        except UnicodeError:
            text = VOICES_INI.read_text(encoding="utf-8-sig")
        section = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                voices[section] = {}
            elif "=" in line and section is not None:
                key, _, value = line.partition("=")
                voices[section][key.strip().lower()] = value.strip()
        return voices

    def write(self, voices: dict) -> None:
        lines = []
        for name, props in voices.items():
            lines.append(f"[{_ini_safe(name)}]")
            for key, value in props.items():
                lines.append(f"{_ini_safe(key)}={_ini_safe(value)}")
            lines.append("")
        content = "\r\n".join(lines)
        fd, tmp = tempfile.mkstemp(dir=str(VOICES_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-16", newline="") as f:
                f.write(content)
            os.replace(tmp, VOICES_INI)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, mutate) -> None:
        with self.lock:
            voices = self.read()
            mutate(voices)
            self.write(voices)


# ---------------------------------------------------------------------------
# Engine wrapper with cancellable streaming generation
# ---------------------------------------------------------------------------

# Before speech starts, the end-of-speech signal of some voices can already
# cross its threshold, and a single word then ends before it is spoken. It is
# ignored on the first 6 frames (0.48 s), as in pocket-tts 3.2.0
# (kyutai-labs/pocket-tts#319); no word or sentence ends that soon.
MIN_FRAMES_BEFORE_EOS = 6

# Six frames are not enough for a prompt of a single word or letter, which
# is what a screen reader sends while the user arrows through text. Some
# voices then open with up to a second and a half of silence, the
# end-of-speech signal is on from the second frame, and generation stopped
# five frames after the guard, part-way into the letter or before it was
# said at all (issue #3). The signal is therefore believed only once the
# decoded audio has held speech for SPEECH_FRAMES_BEFORE_EOS frames. A frame
# is speech at SPEECH_DBFS or louder; the model's silence measures about
# -70 dBFS and its speech -35 to -15.
SPEECH_DBFS = -40.0
SPEECH_FRAMES_BEFORE_EOS = 2
# A voice too quiet ever to reach SPEECH_DBFS must still end: past this many
# frames (3.2 s) without speech the signal is believed anyway.
FRAMES_BEFORE_EOS_WITHOUT_SPEECH = 40
# The silence before the first word of a request only delays it, by up to a
# second and a half on some voices: the words are on their way as soon as
# the model starts saying them. This many frames of it are kept, so that a
# soft first sound is not lost.
FRAMES_BEFORE_SPEECH_KEPT = 2


class _SpeechOnset:
    """Where the decoded audio of one prompt turns into speech. The decoder
    measures it frame by frame; the generating thread asks about it before it
    believes the end-of-speech signal, and waits while the decoder catches
    up."""

    def __init__(self):
        self._cond = threading.Condition()
        self.decoded = 0     # frames decoded so far
        self.frame = None    # the first frame that is speech
        self.closed = False  # the decoder has stopped

    def add(self, pcm):
        """Record the next decoded frame, a float tensor in -1..1."""
        rms = float(pcm.pow(2).mean().sqrt())
        loud = rms > 0 and 20 * math.log10(rms) >= SPEECH_DBFS
        with self._cond:
            if loud and self.frame is None:
                self.frame = self.decoded
            self.decoded += 1
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def heard_by(self, frame, cancel):
        """Whether speech had begun by `frame`."""
        with self._cond:
            while (self.frame is None and self.decoded <= frame
                   and not self.closed and not cancel.is_set()):
                self._cond.wait(0.05)
            return self.frame is not None and self.frame <= frame


class Settings:
    """Engine options, kept in engine.json in the data folder.

    quantize      int8 weights for the model's transformer: Kyutai measured
                  about 27% faster on x86 with no loss of intelligibility.
    continuation  each piece of a long text is generated as a continuation
                  of the piece before it, instead of starting afresh from the
                  voice sample, so a long passage keeps one flow (pocket-tts
                  4.x's teacher_force_previous_chunk).
    """

    DEFAULTS = {"quantize": False, "continuation": False}

    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            with self.lock:
                for key in self.DEFAULTS:
                    if isinstance(data.get(key), bool):
                        self.values[key] = data[key]

    def save(self):
        with self.lock:
            text = json.dumps(self.values, indent=2)
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)

    def get(self, key) -> bool:
        with self.lock:
            return bool(self.values[key])

    def set(self, key, value: bool):
        with self.lock:
            self.values[key] = bool(value)


SETTINGS = Settings()


def voice_model_id(props: dict) -> str:
    """The AI model a voice was made with; voices from before 1.3.0 were all
    made with the standard English model."""
    model_id = props.get("model") or catalog.STANDARD_MODEL
    return model_id if model_id in catalog.BY_ID else catalog.STANDARD_MODEL


def _legacy_stamp() -> str:
    try:
        return VOICES_MODEL_STAMP.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _spell_plain(text: str) -> str:
    """Spelling for a language the English character names do not suit."""
    return ", ".join(c for c in text if not c.isspace())


class Engine:
    def __init__(self):
        self.models = {}                   # model id -> loaded TTSModel
        self.model_order = []              # least recently used first
        self.models_lock = threading.RLock()
        self.gen_lock = threading.Lock()   # pocket-tts is not thread-safe
        self.state_cache = {}              # voice name -> (model id, state)
        self.state_cache_order = []
        self.load_errors = {}              # model id -> why it did not load

    # -- models -------------------------------------------------------------

    @property
    def load_error(self):
        return self.load_errors.get(catalog.STANDARD_MODEL)

    def load(self):
        """Load the standard model at start-up. Everything else loads when a
        voice first needs it."""
        try:
            self.model_for(catalog.STANDARD_MODEL)
        except Exception:   # noqa: BLE001
            pass    # logged, and kept in load_errors for the Voice Manager

    def model_for(self, model_id: str):
        """The loaded model `model_id`, loading it first if need be."""
        with self.models_lock:
            model = self.models.get(model_id)
            if model is not None:
                self.model_order.remove(model_id)
                self.model_order.append(model_id)
                return model
            model = self._load_model(model_id)
            self.models[model_id] = model
            self.model_order.append(model_id)
            while len(self.model_order) > MAX_LOADED_MODELS:
                evicted = self.model_order.pop(0)
                self.models.pop(evicted, None)
                self._drop_states_of(evicted)
                logger.info("let go of model %s to save memory", evicted)
            return model

    def _load_model(self, model_id: str):
        from pocket_tts import TTSModel
        info = catalog.get(model_id)
        if not catalog.is_installed(MODELS_DIR, model_id):
            error = (f'The AI model "{info.title}" is not installed. Download '
                     "it in the Voice Manager, under AI Models.")
            self.load_errors[model_id] = error
            raise FileNotFoundError(error)
        config = str(catalog.config_file(MODELS_DIR, model_id))
        quantize = SETTINGS.get("quantize")
        t0 = time.monotonic()
        try:
            try:
                model = TTSModel.load_model(config=config, quantize=quantize)
            except Exception:   # noqa: BLE001
                if not quantize:
                    raise
                logger.exception("int8 quantization failed for %s; loading "
                                 "it at full precision", model_id)
                model = TTSModel.load_model(config=config)
        except Exception as exc:
            logger.exception("model %s failed to load", model_id)
            self.load_errors[model_id] = str(exc)
            raise
        self.load_errors.pop(model_id, None)
        # The English text rules (numbers, letter names) apply to English
        # models only.
        model._pockettts_english = info.english
        logger.info("model %s loaded in %.1fs (voice cloning: %s, int8: %s)",
                    model_id, time.monotonic() - t0, model.has_voice_cloning,
                    quantize)
        return model

    def unload(self, model_id=None):
        """Forget one loaded model, or all of them, and their voices."""
        with self.models_lock:
            ids = [model_id] if model_id else list(self.models)
            for mid in ids:
                self.models.pop(mid, None)
                if mid in self.model_order:
                    self.model_order.remove(mid)
                self._drop_states_of(mid)

    def loaded_ids(self):
        with self.models_lock:
            return list(self.models)

    def after_load(self):
        """Background work once the standard model is loaded: bring every
        voice in line with its model, then warm up."""
        try:
            add_new_default_voices()
        except Exception:
            logger.exception("adding the new default voices failed "
                             "(non-fatal)")
        try:
            self.sync_voices_with_models()
        except Exception:
            logger.exception("voice rebuild failed (non-fatal)")
        self.warm_up()

    # -- voices and their models -------------------------------------------

    def sync_voices_with_models(self):
        """A voice only sounds right on the weights that built it. When a
        model differs from the one its voices were last built with (a new
        version installed over an old one, model files replaced by hand), or
        the voices were embedded the way an older version did it, they are
        rebuilt from their audio samples."""
        voices = STORE.read()
        legacy = _legacy_stamp()
        by_model = {}
        for name, props in voices.items():
            by_model.setdefault(voice_model_id(props), []).append(name)
        for model_id, names in by_model.items():
            if not catalog.is_installed(MODELS_DIR, model_id):
                logger.warning("voices %s need model %s, which is not "
                               "installed", names, model_id)
                continue
            current = _model_fingerprint(model_id).get("sha256")
            stale = []
            for name in names:
                props = voices[name]
                built_with = props.get("model_sha256") or legacy
                recipe = _int(props.get("embedding"), 1)
                if built_with != current or recipe < EMBEDDING_RECIPE:
                    stale.append(name)
            if stale:
                logger.info("rebuilding %d voice(s) for model %s (%s): %s",
                            len(stale), model_id, current[:12],
                            ", ".join(stale))
                self.rebuild_voices(stale,
                                    lambda msg: logger.info("voices: %s", msg))

    def rebuild_voices(self, names, progress):
        """Re-embed the named voices from their audio samples with each
        one's own model, and record that model in voices.ini."""
        from pocket_tts.models.model_state import export_model_state
        voices = STORE.read()
        for name in names:
            props = voices.get(name)
            if props is None:
                continue
            model_id = voice_model_id(props)
            current = _model_fingerprint(model_id).get("sha256", "")
            source = props.get("source", "")
            src = (VOICES_DIR / source) if source else None
            if src is None or not src.exists():
                if props.get("model_sha256", _legacy_stamp()) == current:
                    # Same weights, older recipe: the voice still works as it
                    # is, and there is nothing to rebuild it from.
                    _mark_built(name, model_id, current)
                    continue
                progress(f'Voice "{name}" has no audio sample to rebuild it '
                         "from; leaving it as it is.")
                continue
            progress(f'Rebuilding voice "{name}" for its AI model...')
            dest = VOICES_DIR / props.get("file", name + ".safetensors")
            tmp = dest.with_name(dest.name + ".new")
            try:
                model = self.model_for(model_id)
                with self.gen_lock:
                    state = model.get_state_for_audio_prompt(src,
                                                             truncate=True)
                # Replaced in one step: the voice may be loaded to speak.
                export_model_state(state, tmp)
                os.replace(tmp, dest)
            except Exception:   # noqa: BLE001
                logger.exception("could not rebuild voice %r", name)
                progress(f'Could not rebuild voice "{name}"; leaving it as '
                         "it is.")
                continue
            self.drop_cached_state(name)
            _mark_built(name, model_id, current)

    def warm_up(self):
        """First generation initialises lazy torch state; do it off the
        critical path so the first real utterance is fast. Every model a
        published voice uses is loaded here too, up to MAX_LOADED_MODELS."""
        try:
            voices = STORE.read()
            first = {}
            for name, props in voices.items():
                if props.get("published", "0") == "1":
                    first.setdefault(voice_model_id(props), name)
            if not first and voices:
                name = next(iter(voices))
                first[voice_model_id(voices[name])] = name
            for model_id, name in list(first.items())[:MAX_LOADED_MODELS]:
                if not catalog.is_installed(MODELS_DIR, model_id):
                    continue
                state, model = self.get_voice_state(name, voices)
                produced = 0
                t0 = time.monotonic()
                with self.gen_lock:
                    for pcm in self._stream_chunks(model, state, "Ready.",
                                                   threading.Event()):
                        produced += len(pcm)
                audio_ms = produced / 2 / POCKETTTS_SAMPLE_RATE * 1000
                if model_id == catalog.STANDARD_MODEL or SPEED.get() <= 0:
                    SPEED.update(audio_ms, (time.monotonic() - t0) * 1000)
                logger.info("warm-up of %s done (x%.2f realtime)", model_id,
                            audio_ms / max(1.0, (time.monotonic() - t0) * 1000))
        except Exception:
            logger.exception("warm-up failed (non-fatal)")

    def get_voice_state(self, name: str, voices: dict):
        """(voice state, model) for the voice `name`."""
        props = voices.get(name)
        if props is None:
            raise KeyError(f"unknown voice: {name}")
        model_id = voice_model_id(props)
        model = self.model_for(model_id)
        cached = self.state_cache.get(name)
        if cached is not None and cached[0] == model_id:
            return cached[1], model
        path = VOICES_DIR / props.get("file", name + ".safetensors")
        if not path.exists():
            raise FileNotFoundError(f"voice file missing: {path}")
        state = model.get_state_for_audio_prompt(path)
        self._cache_state(name, model_id, state)
        return state, model

    def _cache_state(self, name, model_id, state):
        self.drop_cached_state(name)
        self.state_cache[name] = (model_id, state)
        self.state_cache_order.append(name)
        while len(self.state_cache_order) > 6:
            evicted = self.state_cache_order.pop(0)
            self.state_cache.pop(evicted, None)

    def drop_cached_state(self, name: str):
        self.state_cache.pop(name, None)
        if name in self.state_cache_order:
            self.state_cache_order.remove(name)

    def _drop_states_of(self, model_id):
        for name, (mid, _) in list(self.state_cache.items()):
            if mid == model_id:
                self.drop_cached_state(name)

    def clear_cache(self):
        self.state_cache.clear()
        self.state_cache_order.clear()

    # -- generation ---------------------------------------------------------

    def _stream_chunks(self, model, model_state, text, cancel, spell=False):
        """Yield int16 PCM byte chunks; honours `cancel` between model steps
        so screen-reader interruptions take effect within ~one frame."""
        import torch
        from pocket_tts.default_parameters import (
            MAX_TOKEN_PER_CHUNK, MAX_TOKEN_PER_CHUNK_TEACHER_FORCED)
        from pocket_tts.models.text_chunking import (
            prepare_text_prompt, split_into_best_sentences)

        # The English model has no tokens for numbers and mangles digit
        # strings ("224" was heard as "24"), and it garbles a letter or a
        # symbol said on its own, so those are written out in words first.
        # The rules are English, so other languages get the text as it is.
        if _model_is_english(model):
            spoken = speakable(text, spell)
        else:
            spoken = _spell_plain(text) if spell else text
        if spoken != text:
            logger.debug("written out: %r -> %r", text, spoken)
            text = spoken
        if not text.strip():
            return

        continuation = SETTINGS.get("continuation")
        options = (model.pad_with_spaces_for_short_inputs,
                   model.remove_semicolons,
                   model.append_terminal_punctuation,
                   model.capitalize_first_letter,
                   model.replace_characters)
        chunks = split_into_best_sentences(
            model.flow_lm.conditioner.tokenizer, text,
            (MAX_TOKEN_PER_CHUNK_TEACHER_FORCED if continuation
             else MAX_TOKEN_PER_CHUNK),
            options[0], *options[1:])
        previous_chunk = None
        previous_latents = []
        for n, chunk in enumerate(chunks):
            if cancel.is_set():
                return
            forced = None
            text_chunk = chunk
            if continuation and previous_latents:
                # The next piece is generated as the continuation of this
                # one: its text follows on from the previous piece's, and the
                # previous piece's frames are fed back in first.
                forced = torch.cat(previous_latents, dim=1)
                text_chunk = f"{previous_chunk} {chunk}"
            previous_chunk, previous_latents = chunk, []
            prompt, _ = prepare_text_prompt(text_chunk, *options)
            _, guess = prepare_text_prompt(chunk, *options)
            frames_after_eos = model.model_recommended_frames_after_eos
            if frames_after_eos is None:
                frames_after_eos = guess + 2
            # Later chunks keep their opening silence: it is the pause
            # between sentences.
            yield from self._stream_one(
                model, model_state, prompt, frames_after_eos, cancel,
                trim_silence=n == 0,
                latents_out=previous_latents if continuation else None,
                forced=forced)

    def _stream_one(self, model, model_state, text, frames_after_eos, cancel,
                    trim_silence=False, latents_out=None, forced=None):
        """Cancellable re-implementation of pocket-tts 4.x
        _generate_audio_stream_short_text: latents are produced on one
        thread and decoded by another, as upstream does, but both loops watch
        `cancel`. The end-of-speech signal counts only once speech has been
        heard, and with `trim_silence` the silence before it is dropped (see
        SPEECH_DBFS). `forced` holds the previous piece's frames when this
        piece continues it, and `latents_out` collects this piece's frames
        for the next one."""
        import copy as _copy
        import queue as _queue

        import torch
        from pocket_tts.modules.stateful_module import (
            increment_steps, init_states)

        model_state = _copy.deepcopy(model_state)

        prepared = model.flow_lm.conditioner.prepare(text)
        token_count = prepared.shape[1]
        n_forced = 0 if forced is None else forced.shape[1]
        max_gen_len = model._estimate_max_gen_len(token_count)
        mimi_steps = int(model.mimi.encoder_frame_rate / model.mimi.frame_rate)
        warmup = None
        if forced is not None:
            warmup = forced[:, -model._DECODER_WARMUP_FRAMES:]
        n_warmup = 0 if warmup is None else warmup.shape[1]
        mimi_seq_len = (max_gen_len + n_warmup) * mimi_steps
        frame_samples = int(model.mimi.sample_rate / model.mimi.frame_rate)
        sample_rate = model.mimi.sample_rate

        current_end = model._flow_lm_current_end(model_state)
        model._expand_kv_cache(
            model_state,
            sequence_length=current_end + token_count + n_forced + max_gen_len)
        first_input = None
        if n_forced == 0:
            model._run_flow_lm_and_increment_step(
                model_state=model_state, text_tokens=prepared)
        else:
            # Teacher forcing: BOS, then every forced frame but the last,
            # which is the input of the first generated step.
            bos = torch.full_like(forced[:, :1], float("NaN"))
            model._run_flow_lm_and_increment_step(
                model_state=model_state, text_tokens=prepared,
                backbone_input_latents=torch.cat([bos, forced[:, :-1]],
                                                 dim=1))
            first_input = forced[:, -1:]

        latents_q = _queue.Queue()
        out_q = _queue.Queue()
        onset = _SpeechOnset()

        # Gradient tracking is per thread, so the decoder thread needs its
        # own no_grad, as upstream's _decode_audio_worker has.
        @torch.no_grad()
        def decoder():
            silence = []    # frames before speech, held back
            try:
                mimi_state = init_states(model.mimi, batch_size=1,
                                         sequence_length=mimi_seq_len)
                # A fresh decoder puts a small step in its first samples,
                # heard as a click: the first 5 ms are faded in, as
                # pocket-tts 4.x does.
                fade_in = torch.linspace(0, 1, sample_rate // 200)
                if warmup is not None:
                    # A continuation: the decoder runs over the end of the
                    # previous piece first (audio dropped), so this piece's
                    # audio follows on from it with no fresh-state click.
                    model.mimi.decode_from_latent(
                        warmup * model.flow_lm.emb_std
                        + model.flow_lm.emb_mean, mimi_state)
                    increment_steps(model.mimi, mimi_state,
                                    increment=mimi_steps * warmup.shape[1])
                    fade_in = None
                finished = False
                while not finished and not cancel.is_set():
                    latent = latents_q.get()
                    if latent is None:
                        break
                    # Every frame the generator has queued is decoded in one
                    # call, as upstream does, so the decoder keeps up on
                    # slower machines.
                    latents = [latent]
                    while True:
                        try:
                            nxt = latents_q.get_nowait()
                        except _queue.Empty:
                            break
                        if nxt is None:
                            finished = True
                            break
                        latents.append(nxt)
                    decoding_input = (torch.cat(latents, dim=1)
                                      * model.flow_lm.emb_std
                                      + model.flow_lm.emb_mean)
                    audio = model.mimi.decode_from_latent(decoding_input,
                                                          mimi_state)
                    increment_steps(model.mimi, mimi_state,
                                    increment=mimi_steps * len(latents))
                    if cancel.is_set():
                        break
                    samples = audio[0, 0].clamp(-1, 1)
                    if fade_in is not None:
                        n = min(fade_in.numel(), samples.shape[-1])
                        samples[:n] *= fade_in[:n].to(samples)
                        fade_in = None
                    for start in range(0, samples.shape[-1], frame_samples):
                        frame = samples[start:start + frame_samples]
                        onset.add(frame)
                        pcm = (frame * 32767).short().numpy().tobytes()
                        if trim_silence and onset.frame is None:
                            silence.append(pcm)
                            continue
                        for held in silence[-FRAMES_BEFORE_SPEECH_KEPT:]:
                            out_q.put(("chunk", held))
                        silence.clear()
                        out_q.put(("chunk", pcm))
                # Speech never measured: a voice too quiet for SPEECH_DBFS
                # keeps all of its audio.
                if not cancel.is_set():
                    for held in silence:
                        out_q.put(("chunk", held))
            except Exception as exc:   # noqa: BLE001
                out_q.put(("error", exc))
            finally:
                onset.close()
                out_q.put(("done", None))

        decoder_thread = threading.Thread(target=decoder, daemon=True)
        decoder_thread.start()

        def producer():
            try:
                with torch.no_grad():
                    backbone = first_input
                    if backbone is None:
                        backbone = torch.full(
                            (1, 1, model.flow_lm.ldim), float("NaN"),
                            device=next(iter(model.flow_lm.parameters())).device,
                            dtype=model.flow_lm.dtype)
                    eos_step = None
                    for step in range(max_gen_len):
                        if cancel.is_set():
                            break
                        next_latent, is_eos = \
                            model._run_flow_lm_and_increment_step(
                                model_state=model_state,
                                backbone_input_latents=backbone)
                        if (is_eos.item() and eos_step is None
                                and step >= MIN_FRAMES_BEFORE_EOS
                                and (step >= FRAMES_BEFORE_EOS_WITHOUT_SPEECH
                                                 or onset.heard_by(
                                         step - SPEECH_FRAMES_BEFORE_EOS,
                                         cancel))):
                            eos_step = step
                        if (eos_step is not None
                                and step >= eos_step + frames_after_eos):
                            break
                        if latents_out is not None:
                            latents_out.append(next_latent)
                        latents_q.put(next_latent)
                        backbone = next_latent
            except Exception as exc:   # noqa: BLE001
                out_q.put(("error", exc))
            finally:
                latents_q.put(None)

        producer_thread = threading.Thread(target=producer, daemon=True)
        producer_thread.start()

        try:
            while True:
                kind, value = out_q.get()
                if kind == "chunk":
                    if not cancel.is_set():
                        yield value
                elif kind == "done":
                    break
                else:
                    raise value
        except GeneratorExit:
            cancel.set()
            raise
        finally:
            producer_thread.join(timeout=10)
            decoder_thread.join(timeout=10)

    def speak(self, voice_name, text, cancel, emit, spell=False):
        """Generate `text` with `voice_name`, calling emit(pcm_bytes). With
        `spell`, each character is named rather than the text read."""
        t0 = time.monotonic()
        voices = STORE.read()
        state, model = self.get_voice_state(voice_name, voices)
        emitted = 0
        emit_seconds = 0.0
        with self.gen_lock:
            if cancel.is_set():
                logger.debug("speak cancelled before generation started")
                return
            for pcm in self._stream_chunks(model, state, text, cancel, spell):
                if cancel.is_set():
                    break
                emitted += len(pcm)
                # emit blocks while the client's audio device drains, which
                # is playback time, not generation time.
                t_emit = time.monotonic()
                emit(pcm)
                emit_seconds += time.monotonic() - t_emit
        elapsed = time.monotonic() - t0
        audio_ms = emitted / 2 / POCKETTTS_SAMPLE_RATE * 1000
        work_ms = (elapsed - emit_seconds) * 1000
        if not cancel.is_set():
            SPEED.update(audio_ms, work_ms)
        logger.info(
            "speak: voice %r, %d chars -> %d ms audio in %d ms "
            "(%d ms generating, x%.2f realtime, machine x%.2f)%s",
            voice_name, len(text), int(audio_ms), int(elapsed * 1000),
            int(work_ms), audio_ms / work_ms if work_ms > 0 else 0.0,
            SPEED.get(), " (cancelled)" if cancel.is_set() else "")

    # -- cloning ------------------------------------------------------------

    def _embed(self, model_id, audio_path):
        """A voice state for `audio_path` with the model `model_id`."""
        model = self.model_for(model_id)
        if not model.has_voice_cloning:
            raise RuntimeError(
                "The installed model does not support voice cloning.")
        with self.gen_lock:
            return model.get_state_for_audio_prompt(Path(audio_path),
                                                    truncate=True)

    def clone(self, name, audio_path, gender, language, progress,
              model_id=None):
        model_id = model_id or catalog.STANDARD_MODEL
        info = catalog.get(model_id)
        src = Path(audio_path)
        if not src.exists():
            raise FileNotFoundError(f"audio file not found: {src}")
        safe = _sanitize_name(name)
        if not safe:
            raise ValueError("voice name is empty or invalid")
        voices = STORE.read()
        if safe.lower() in {v.lower() for v in voices}:
            raise ValueError(f'a voice named "{safe}" already exists')

        from pocket_tts.models.model_state import export_model_state
        progress(f"Analyzing the audio sample with {info.title} (this can "
                 "take a minute)...")
        state = self._embed(model_id, src)
        progress("Saving the voice...")
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        VOICES_SRC_DIR.mkdir(parents=True, exist_ok=True)
        export_model_state(state, VOICES_DIR / f"{safe}.safetensors")
        src_copy = VOICES_SRC_DIR / (safe + src.suffix.lower())
        shutil.copy(src, src_copy)
        sha = _model_fingerprint(model_id).get("sha256", "")

        def mutate(voices):
            voices[safe] = {
                "file": f"{safe}.safetensors",
                "gender": "Female" if gender == 1 else "Male",
                # A voice speaks its model's language; the Clone dialog's
                # choice is kept only for the English models.
                "language": (_clean_language(language) if info.english
                             else info.lcid),
                "published": "0",
                "source": f"src/{src_copy.name}",
                "model": model_id,
                "model_sha256": sha,
                "embedding": str(EMBEDDING_RECIPE),
            }
        STORE.update(mutate)
        self._cache_state(safe, model_id, state)
        return safe

    def set_voice_model(self, name, model_id, copy_name, progress):
        """Rebuild the voice `name` for another AI model from its audio
        sample: in place, or, when `copy_name` is given, as a new voice of
        that name, leaving the original as it is."""
        info = catalog.get(model_id)
        voices = STORE.read()
        props = voices.get(name)
        if props is None:
            raise ValueError(f'no voice named "{name}"')
        source = props.get("source", "")
        src = (VOICES_DIR / source) if source else None
        if src is None or not src.exists():
            raise ValueError(f'"{name}" has no audio sample on this computer, '
                             "so it cannot be made for another AI model. "
                             "Clone it again from the original recording.")
        if copy_name:
            target = _sanitize_name(copy_name)
            if not target:
                raise ValueError("the new voice's name is empty or invalid")
            if target.lower() in {v.lower() for v in voices}:
                raise ValueError(f'a voice named "{target}" already exists')
        else:
            target = name
            if voice_model_id(props) == model_id:
                raise ValueError(f'"{name}" already uses {info.title}.')

        from pocket_tts.models.model_state import export_model_state
        progress(f'Making "{target}" with {info.title} (this can take a '
                 "minute)...")
        state = self._embed(model_id, src)
        sha = _model_fingerprint(model_id).get("sha256", "")
        language = (props.get("language", catalog.LCID_ENGLISH)
                    if info.english else info.lcid)
        if copy_name:
            dest = VOICES_DIR / f"{target}.safetensors"
            export_model_state(state, dest)
            src_copy = VOICES_SRC_DIR / (target + src.suffix.lower())
            shutil.copy(src, src_copy)

            def mutate(voices):
                voices[target] = {
                    "file": dest.name,
                    "gender": props.get("gender", "Male"),
                    "language": language,
                    "published": "0",
                    "source": f"src/{src_copy.name}",
                    "model": model_id,
                    "model_sha256": sha,
                    "embedding": str(EMBEDDING_RECIPE),
                }
        else:
            dest = VOICES_DIR / props.get("file", name + ".safetensors")
            tmp = dest.with_name(dest.name + ".new")
            export_model_state(state, tmp)
            os.replace(tmp, dest)

            def mutate(voices):
                if target in voices:
                    voices[target].update({
                        "language": language,
                        "model": model_id,
                        "model_sha256": sha,
                        "embedding": str(EMBEDDING_RECIPE),
                    })
        STORE.update(mutate)
        self._cache_state(target, model_id, state)
        if copy_name:
            return (f'Made "{target}" with {info.title}. It is not published '
                    "yet: select it and choose Publish to SAPI.")
        return f'"{target}" now uses {info.title}.'

    # -- models: download, update, remove -----------------------------------

    def update_models(self, token, progress, model_id=None):
        """Download (or update) one AI model and rebuild its voices."""
        from huggingface_hub.utils import disable_progress_bars

        # No one can see a progress bar in the host; the Voice Manager shows
        # the progress messages instead.
        disable_progress_bars()
        model_id = model_id or catalog.STANDARD_MODEL
        info = catalog.get(model_id)
        folder = catalog.download(MODELS_DIR, model_id, token or None,
                                  progress)

        progress(f"Loading {info.title}...")
        self.unload(model_id)
        try:
            self.model_for(model_id)
        except Exception as exc:   # noqa: BLE001
            catalog.roll_back(folder)
            self.unload(model_id)
            if catalog.is_installed(MODELS_DIR, model_id):
                try:
                    self.model_for(model_id)
                except Exception:   # noqa: BLE001
                    pass
            raise RuntimeError("the new model failed to load, so the "
                               f"previous one was kept: {exc}") from None
        catalog.finish_install(folder)

        # Voice states are tied to the model weights.
        names = [n for n, p in STORE.read().items()
                 if voice_model_id(p) == model_id]
        if names:
            self.rebuild_voices(names, progress)
        progress(f"{info.title} is installed and ready.")

    def remove_model(self, model_id):
        info = catalog.get(model_id)
        if model_id == catalog.STANDARD_MODEL:
            raise ValueError(f"{info.title} is the default model and stays "
                             "installed.")
        users = [n for n, p in STORE.read().items()
                 if voice_model_id(p) == model_id]
        if users:
            raise ValueError(f"{info.title} is used by these voices: "
                             + ", ".join(users) + ". Delete them or move "
                             "them to another model first.")
        self.unload(model_id)
        catalog.remove(MODELS_DIR, model_id)
        _MODEL_FINGERPRINT_CACHE.pop(model_id, None)
        return f"{info.title} was removed."

    def apply_settings(self, quantize, continuation, progress):
        changed_quantize = quantize != SETTINGS.get("quantize")
        SETTINGS.set("quantize", quantize)
        SETTINGS.set("continuation", continuation)
        SETTINGS.save()
        if changed_quantize:
            progress("Reloading the AI models with the new setting...")
            with self.gen_lock:
                loaded = self.loaded_ids()
                self.unload()
            for model_id in loaded:
                self.model_for(model_id)
        return "The engine settings were saved."

    # -- voice packages -----------------------------------------------------

    def export_voices(self, names, dest_path, include_sources, progress):
        """Write the named voices, or every voice when `names` is empty, to a
        .pttsvoices package. Returns a summary line for the Voice Manager."""
        voices = STORE.read()
        wanted = [n for n in (names or list(voices)) if n in voices]
        skipped = [n for n in (names or []) if n not in voices]
        if not wanted:
            raise ValueError("none of the selected voices exist any more")
        if len(wanted) > PACKAGE_MAX_VOICES:
            raise ValueError(
                f"a package holds at most {PACKAGE_MAX_VOICES} voices")

        dest = Path(dest_path)
        if not dest.name:
            raise ValueError("no destination file was given")
        if dest.suffix.lower() != PACKAGE_EXT:
            dest = dest.with_name(dest.name + PACKAGE_EXT)
        dest.parent.mkdir(parents=True, exist_ok=True)

        progress("Identifying the AI models these voices were made with...")
        model = _model_fingerprint(catalog.STANDARD_MODEL)

        entries = []
        stems = set()
        tmp = dest.parent / (dest.name + ".part")
        try:
            with zipfile.ZipFile(tmp, "w", allowZip64=True) as zf:
                for name in wanted:
                    props = voices[name]
                    state = VOICES_DIR / props.get("file",
                                                   name + ".safetensors")
                    if not state.exists():
                        progress(f'Skipping "{name}": its voice file is '
                                 "missing from this computer.")
                        skipped.append(name)
                        continue
                    stem = _member_stem(name, stems)
                    state_member = f"voices/{stem}.safetensors"
                    progress(f'Adding "{name}" '
                             f"({_human_size(state.stat().st_size)})...")
                    # Voice embeddings are dense float data that deflate
                    # cannot shrink, so they go in uncompressed.
                    zf.write(state, state_member,
                             compress_type=zipfile.ZIP_STORED)
                    entry = {
                        "name": name,
                        "gender": ("Female"
                                   if props.get("gender", "").lower()
                                   == "female" else "Male"),
                        "language": _clean_language(props.get("language")),
                        "published": props.get("published", "0") == "1",
                        "state_member": state_member,
                        "state_sha256": _sha256_file(state),
                        "source_member": None,
                        "source_sha256": None,
                        "model_id": voice_model_id(props),
                        "model_sha256": (props.get("model_sha256")
                                         or _model_fingerprint(
                                             voice_model_id(props)).get(
                                                 "sha256", "")),
                        "embedding": _int(props.get("embedding"), 1),
                    }
                    source_rel = props.get("source", "")
                    source = (VOICES_DIR / source_rel) if source_rel else None
                    if include_sources and source is not None and source.exists():
                        member = f"src/{stem}{source.suffix.lower()}"
                        progress(f'Adding the audio sample for "{name}" '
                                 f"({_human_size(source.stat().st_size)})...")
                        zf.write(source, member,
                                 compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=6)
                        entry["source_member"] = member
                        entry["source_sha256"] = _sha256_file(source)
                    elif include_sources:
                        progress(f'"{name}" has no audio sample on this '
                                 "computer, so it cannot be rebuilt for a "
                                 "different AI model.")
                    entries.append(entry)

                if not entries:
                    raise ValueError(
                        "none of the selected voices could be read")

                manifest = {
                    "kind": PACKAGE_KIND,
                    "format": (1 if all(e["model_id"]
                                        == catalog.STANDARD_MODEL
                                        for e in entries)
                               else PACKAGE_FORMAT),
                    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                    "created_by": ("Pocket TTS SAPI5 Voice Manager "
                                   + APP_VERSION),
                    "pocket_tts_version": _pocket_tts_version(),
                    "sample_rate": 24000,
                    "model": model,
                    "voices": entries,
                }
                zf.writestr(PACKAGE_MANIFEST,
                            json.dumps(manifest, indent=2, ensure_ascii=False),
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=6)
                zf.writestr("README.txt", PACKAGE_README,
                            compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=6)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        with_source = sum(1 for e in entries if e["source_member"])
        parts = [f"Exported {len(entries)} voice(s) to {dest} "
                 f"({_human_size(dest.stat().st_size)})."]
        parts.append(f"{with_source} of {len(entries)} include the audio "
                     "sample they were made from.")
        if skipped:
            parts.append("Skipped: " + ", ".join(sorted(set(skipped))) + ".")
        logger.info("exported %d voice(s) to %s", len(entries), dest)
        return " ".join(parts)

    def inspect_package(self, path):
        """Describe a package without changing anything.

        Returns (summary, rows) where each row is
        (name, female, has_source, status) and status is 0 for a voice made
        with this computer's model, 1 for another model but rebuildable from
        its audio sample, and 2 for another model with no audio sample."""
        manifest = _read_manifest(Path(path))
        existing = STORE.read()

        rows = []
        clashes = []
        for entry in manifest.get("voices", []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            has_source = bool(entry.get("source_member"))
            plan = _import_plan(entry, manifest)
            if plan["target"] is None:
                status = 3
            elif plan["same"]:
                status = 0
            else:
                status = 1 if has_source else 2
            female = str(entry.get("gender", "")).lower() == "female"
            rows.append((name, female, has_source, status))
            if _sanitize_name(name) in existing:
                clashes.append(name)
        if not rows:
            raise ValueError("this package contains no voices")

        created = str(manifest.get("created_utc", "")).replace("T", " ")
        created = created.replace("Z", " UTC")
        by = str(manifest.get("created_by", "an unknown version"))
        parts = [f"{len(rows)} voice(s) in this package, written {created} "
                 f"by {by}."]
        if all(row[3] == 0 for row in rows):
            parts.append("They were made with the same AI models as this "
                         "computer has, so they will sound exactly as "
                         "intended.")
        else:
            rebuildable = sum(1 for row in rows if row[3] == 1)
            risky = sum(1 for row in rows if row[3] == 2)
            plans = [_import_plan(e, manifest)
                     for e in manifest.get("voices", [])
                     if isinstance(e, dict)]
            missing = sorted({pl["wanted"] for pl in plans
                              if pl["target"] is None})
            parts.append("Some or all of them were made with a different AI "
                         "model than this computer has.")
            if missing:
                parts.append(
                    "Some need an AI model that is not installed here; "
                    "download it under AI Models first: "
                    + ", ".join(catalog.BY_ID[m].title if m in catalog.BY_ID
                                else m for m in missing) + ".")
            if rebuildable:
                parts.append(f"{rebuildable} of them include the audio sample "
                             "and will be rebuilt for your model "
                             "automatically.")
            if risky:
                parts.append(f"{risky} of them have no audio sample and may "
                             "not sound correct; ask the sender to export "
                             "again with the audio samples included.")
        if clashes:
            parts.append("Already on this computer: "
                         + ", ".join(sorted(set(clashes)))
                         + ". Choose below what should happen to those.")
        return " ".join(parts), rows

    def import_voices(self, path, names, publish, collision, progress):
        """Install voices from a package. `collision` says what to do with a
        name that already exists: 0 skip it, 1 import under a new name,
        2 replace the existing voice."""
        archive = Path(path)
        manifest = _read_manifest(archive)

        wanted = set(names or [])
        entries = [e for e in manifest.get("voices", [])
                   if isinstance(e, dict)
                   and (not wanted
                        or str(e.get("name", "")).strip() in wanted)]
        if not entries:
            raise ValueError("none of the selected voices are in this package")

        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        VOICES_SRC_DIR.mkdir(parents=True, exist_ok=True)

        imported = []
        renamed = []
        replaced = []
        clashed = []    # left alone because the name is already taken
        failed = []     # something went wrong with that voice
        rebuilt = []
        risky = []

        budget = [PACKAGE_MAX_TOTAL_BYTES]
        with zipfile.ZipFile(archive) as zf:
            for entry in entries:
                name = str(entry.get("name", "")).strip()
                safe = _sanitize_name(name)
                if not safe:
                    progress(f'Skipping "{name}": that name cannot be used '
                             "for a voice.")
                    failed.append(name or "(unnamed)")
                    continue

                voices = STORE.read()
                # Windows file names ignore case, so "alba" and "Alba" are the
                # same voice however differently the package spells it.
                by_lower = {existing.lower(): existing for existing in voices}
                clash = by_lower.get(safe.lower())
                target = safe
                replacing = None
                if clash is not None:
                    if collision == 0:
                        progress(f'Skipping "{clash}": a voice with that name '
                                 "already exists.")
                        clashed.append(clash)
                        continue
                    if collision == 2:
                        target = clash
                        replacing = dict(voices[clash])
                        progress(f'Replacing the existing voice "{clash}"...')

                try:
                    if clash is not None and collision == 1:
                        target = _unique_name(safe, voices)
                        progress(f'"{clash}" already exists, so this one is '
                                 f'being imported as "{target}".')
                    plan = _import_plan(entry, manifest)
                    if plan["target"] is None:
                        info = catalog.BY_ID.get(plan["wanted"])
                        raise RuntimeError(
                            "it needs the AI model "
                            + (info.title if info else plan["wanted"])
                            + ", which is not installed. Download it under "
                            "AI Models, then import again.")
                    self._install_package_voice(
                        zf, entry, target, replacing, plan, publish,
                        progress, budget)
                except Exception as exc:   # noqa: BLE001
                    logger.exception("importing %r failed", name)
                    progress(f'Could not import "{target}": {exc}')
                    failed.append(target)
                    continue

                imported.append(target)
                if replacing is not None:
                    replaced.append(target)
                elif clash is not None:
                    renamed.append(target)
                if not plan["same"]:
                    if entry.get("source_member"):
                        rebuilt.append(target)
                    else:
                        risky.append(target)

        if not imported:
            reasons = []
            if clashed:
                reasons.append(
                    "these already exist on this computer, and the box was "
                    "set to skip them: " + ", ".join(clashed))
            if failed:
                reasons.append("these could not be read from the package: "
                               + ", ".join(failed))
            raise RuntimeError(
                "no voices were imported. "
                + ("; ".join(reasons) + "." if reasons
                   else "See the progress list for details."))

        parts = ["Imported " + str(len(imported)) + " voice(s): "
                 + ", ".join(imported) + "."]
        parts.append("They are published to SAPI and available to every "
                     "application now."
                     if publish else
                     "They are not published yet: select a voice and choose "
                     "Publish to SAPI to make it available to other "
                     "applications.")
        if renamed:
            parts.append("Renamed to avoid a clash: " + ", ".join(renamed)
                         + ".")
        if replaced:
            parts.append("Replaced: " + ", ".join(replaced) + ".")
        if rebuilt:
            parts.append("Rebuilt for your AI model: " + ", ".join(rebuilt)
                         + ".")
        if risky:
            parts.append("Made with a different AI model and supplied without "
                         "an audio sample, so they may not sound correct: "
                         + ", ".join(risky) + ".")
        if clashed:
            parts.append("Skipped because a voice of that name already "
                         "exists: " + ", ".join(clashed) + ".")
        if failed:
            parts.append("Could not be imported: " + ", ".join(failed)
                         + ". See the progress list for the reason.")
        logger.info("imported %d voice(s) from %s", len(imported), archive)
        return " ".join(parts)

    def _install_package_voice(self, zf, entry, target, replacing, plan,
                               publish, progress, budget):
        """Unpack one voice out of an open package and register it.

        Everything that can fail -- unpacking, the integrity and format
        checks, and re-embedding the voice for a different AI model -- happens
        inside a staging directory. The voice store is only touched once all
        of it has succeeded, so a package that fails half way through cannot
        damage a voice the user already had. The embedding is put in place
        last, because it is the part that cannot be recreated."""
        state_info = _package_member(zf, entry.get("state_member"))
        source_ref = entry.get("source_member")
        source_info = _package_member(zf, source_ref) if source_ref else None

        staging = Path(tempfile.mkdtemp(dir=str(VOICES_DIR),
                                        prefix="import-"))
        try:
            progress(f'Unpacking "{target}"...')
            staged_state = staging / "voice.safetensors"
            _extract_member(zf, state_info, staged_state,
                            entry.get("state_sha256"), budget)
            _validate_state_file(staged_state)

            staged_source = None
            suffix = ""
            if source_info is not None:
                # The extension ends up in a file name and in voices.ini, so
                # only known audio extensions are carried over from the
                # package; anything else is treated as a WAV.
                suffix = Path(source_info.filename).suffix.lower()
                if suffix not in _AUDIO_SUFFIXES:
                    suffix = ".wav"
                staged_source = staging / ("sample" + suffix)
                _extract_member(zf, source_info, staged_source,
                                entry.get("source_sha256"), budget)

            model_id = plan["target"]
            built_with = plan["sha"]
            recipe = _int(entry.get("embedding"), 1)
            if not plan["same"]:
                if staged_source is not None:
                    progress(f'Rebuilding "{target}" for your AI model '
                             "(this can take a minute)...")
                    from pocket_tts.models.model_state import export_model_state
                    state = self._embed(model_id, staged_source)
                    export_model_state(state, staged_state)
                    _validate_state_file(staged_state)
                    built_with = _model_fingerprint(model_id).get("sha256", "")
                    recipe = EMBEDDING_RECIPE
                else:
                    progress(f'"{target}" was made with a different AI model '
                             "and came without its audio sample, so it may "
                             "not sound correct.")

            # Nothing below here is allowed to fail in a way that loses data.
            state_name = f"{target}.safetensors"
            state_dest = VOICES_DIR / state_name
            source_name = (target + suffix) if staged_source is not None else ""
            source_dest = ((VOICES_SRC_DIR / source_name)
                           if staged_source is not None else None)

            if staged_source is not None:
                os.replace(staged_source, source_dest)
            os.replace(staged_state, state_dest)

            def mutate(voices):
                props = {
                    "file": state_name,
                    "gender": ("Female"
                               if str(entry.get("gender", "")).lower()
                               == "female" else "Male"),
                    "language": _clean_language(entry.get("language")),
                    "published": "1" if publish else "0",
                    "model": model_id,
                    "model_sha256": built_with,
                    "embedding": str(recipe),
                }
                if source_name:
                    props["source"] = f"src/{source_name}"
                voices[target] = props

            STORE.update(mutate)
            self.drop_cached_state(target)

            if replacing:
                keep = {str(state_dest).lower()}
                if source_dest is not None:
                    keep.add(str(source_dest).lower())
                for key in ("file", "source"):
                    rel = replacing.get(key)
                    if not rel:
                        continue
                    old = VOICES_DIR / rel
                    if str(old).lower() in keep:
                        continue
                    if key == "source" and source_dest is None:
                        progress(f'The "{target}" being replaced had an audio '
                                 "sample and this package has none, so that "
                                 "sample is being removed along with it.")
                    try:
                        old.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("could not remove replaced file %s",
                                       old)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# The installer writes voices.ini only on a fresh install, so that an upgrade
# never touches the user's voices. Default voices added in a later version
# are listed in defaults.ini, which every install overwrites, and are added
# to voices.ini once: a default voice the user deleted stays deleted.
DEFAULTS_INI = VOICES_DIR / "defaults.ini"
DEFAULTS_OFFERED = VOICES_DIR / "defaults_offered.txt"
# The default voices of every version before 1.3.0, which their installers
# already offered.
_CLASSIC_DEFAULTS = ("Alba", "Jane", "George", "Michael")


def add_new_default_voices():
    if not DEFAULTS_INI.exists():
        return
    try:
        text = DEFAULTS_INI.read_text(encoding="utf-16")
    except UnicodeError:
        text = DEFAULTS_INI.read_text(encoding="utf-8-sig")
    defaults = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            defaults[section] = {}
        elif "=" in line and section is not None:
            key, _, value = line.partition("=")
            defaults[section][key.strip().lower()] = value.strip()
    try:
        offered = {line.strip().lower() for line in
                   DEFAULTS_OFFERED.read_text(encoding="utf-8").splitlines()
                   if line.strip()}
    except OSError:
        offered = {name.lower() for name in _CLASSIC_DEFAULTS}
    added = []

    def mutate(voices):
        present = {name.lower() for name in voices}
        for name, props in defaults.items():
            if name.lower() in offered:
                continue
            offered.add(name.lower())
            if name.lower() in present:
                continue
            if not (VOICES_DIR / props.get("file", "")).exists():
                continue
            voices[name] = dict(props)
            added.append(name)
    STORE.update(mutate)
    DEFAULTS_OFFERED.write_text("\n".join(sorted(offered)) + "\n",
                                encoding="utf-8")
    if added:
        logger.info("added the new default voices %s", ", ".join(added))


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _model_is_english(model) -> bool:
    return getattr(model, "_pockettts_english", True)


def _mark_built(name, model_id, sha):
    def mutate(voices):
        if name in voices:
            voices[name]["model"] = model_id
            voices[name]["model_sha256"] = sha
            voices[name]["embedding"] = str(EMBEDDING_RECIPE)
    STORE.update(mutate)


# Everything str.splitlines() treats as a line break. Any of these inside a
# name or a value would split one voices.ini line into two when the file is
# read back, letting a value invent a whole section, so they never get written.
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _ini_safe(value) -> str:
    """A name or value that cannot break out of its voices.ini line."""
    text = str(value)
    for char in _LINE_BREAKS:
        text = text.replace(char, " ")
    return text.strip()


def _clean_language(value) -> str:
    """A hex LCID from an untrusted manifest, or the English default.

    This lands in voices.ini verbatim and is read back by the SAPI enumerator,
    so only the shape the format actually allows is accepted."""
    text = str(value or "").strip()
    return text if re.fullmatch(r"[0-9A-Fa-f]{1,8}", text) else "409"


def _sha256_file(path, block=1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _human_size(count) -> str:
    size = float(count)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "bytes":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def _pocket_tts_version() -> str:
    try:
        from importlib.metadata import version
        return version("pocket-tts")
    except Exception:   # noqa: BLE001
        return "unknown"


_MODEL_FINGERPRINT_CACHE = {}


def _model_fingerprint(model_id: str = catalog.STANDARD_MODEL) -> dict:
    """Size and SHA-256 of a model's weights.

    A voice embedding only sounds right on the weights that produced it, so
    voices.ini and a package record this and an import compares it. Hashing
    220 MB costs about a second, so the answer is cached per (size, mtime)."""
    path = catalog.weights_file(MODELS_DIR, model_id)
    try:
        stat = path.stat()
    except OSError:
        return {"file": path.name, "sha256": "", "size": 0, "id": model_id}
    key = (str(path).lower(), stat.st_size, int(stat.st_mtime))
    cached = _MODEL_FINGERPRINT_CACHE.get(model_id)
    if cached is None or cached[0] != key:
        cached = (key, {"file": path.name, "sha256": _sha256_file(path),
                        "size": stat.st_size, "id": model_id})
        _MODEL_FINGERPRINT_CACHE[model_id] = cached
    return cached[1]


def _is_english_lcid(value) -> bool:
    try:
        return (int(str(value), 16) & 0x3FF) == 0x09
    except ValueError:
        return False


def _import_plan(entry: dict, manifest: dict) -> dict:
    """Which local model a packaged voice goes onto, and whether its
    embedding can be used as it is.

    wanted  the model the voice was made with (packages before format 2:
            the standard English model)
    sha     the weights it was made with
    target  the installed model it will use, or None when it needs one that
            is not installed here (an English voice falls back to the
            standard model and is rebuilt; another language cannot)
    same    True when the embedding fits `target` exactly"""
    wanted = str(entry.get("model_id") or catalog.STANDARD_MODEL)
    sha = str(entry.get("model_sha256")
              or (manifest.get("model") or {}).get("sha256") or "")
    target = None
    if wanted in catalog.BY_ID and catalog.is_installed(MODELS_DIR, wanted):
        target = wanted
    elif (_is_english_lcid(entry.get("language", "409"))
          and catalog.is_installed(MODELS_DIR, catalog.STANDARD_MODEL)):
        target = catalog.STANDARD_MODEL
    same = (target == wanted and bool(sha)
            and sha == _model_fingerprint(target).get("sha256"))
    return {"wanted": wanted, "sha": sha, "target": target, "same": same}


_MEMBER_BAD_CHARS = set('\\/:*?"<>|')


def _member_stem(name: str, used: set) -> str:
    """A file-name stem for `name` inside a package, unique within it."""
    stem = "".join("_" if (c in _MEMBER_BAD_CHARS or ord(c) < 32) else c
                   for c in name).strip(" .")[:60]
    if not stem:
        stem = "voice"
    candidate = stem
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}_{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def _unique_name(base: str, voices: dict) -> str:
    # Compared without case, and against the disk as well: Windows file names
    # are case-insensitive, so "alba" and "Alba" are one file.
    taken = {name.lower() for name in voices}
    for counter in range(2, 1000):
        candidate = _sanitize_name(f"{base} ({counter})")
        if (candidate and candidate.lower() not in taken
                and not (VOICES_DIR / f"{candidate}.safetensors").exists()):
            return candidate
    raise ValueError(f'too many voices are already named like "{base}"')


def _read_manifest(archive: Path) -> dict:
    if not archive.exists():
        raise FileNotFoundError(f"the package was not found: {archive}")
    try:
        with zipfile.ZipFile(archive) as zf:
            with zf.open(PACKAGE_MANIFEST) as handle:
                raw = handle.read(4 * 1024 * 1024)
        manifest = json.loads(raw.decode("utf-8"))
    except KeyError:
        raise ValueError("this file is not a Pocket TTS voice package: there "
                         "is no manifest inside it.") from None
    except (zipfile.BadZipFile, OSError):
        raise ValueError("this file is not a Pocket TTS voice package: it is "
                         "not a readable archive.") from None
    except (UnicodeError, ValueError):
        raise ValueError("this package has a damaged manifest.") from None
    if not isinstance(manifest, dict) or manifest.get("kind") != PACKAGE_KIND:
        raise ValueError("this file is not a Pocket TTS voice package.")
    fmt = manifest.get("format")
    if (not isinstance(fmt, int) or isinstance(fmt, bool)
            or fmt > PACKAGE_FORMAT):
        raise ValueError(
            f"this package uses package format {fmt}, which this version does "
            "not understand. Please update Pocket TTS SAPI5.")
    voices = manifest.get("voices")
    if not isinstance(voices, list) or not voices:
        raise ValueError("this package contains no voices.")
    if len(voices) > PACKAGE_MAX_VOICES:
        raise ValueError("this package claims to hold more voices than the "
                         "Voice Manager will install at once.")
    # A name is the key the Voice Manager selects a voice by, so entries whose
    # name could not survive the round trip are dropped here rather than
    # listed and then not found.
    usable = [entry for entry in voices
              if isinstance(entry, dict)
              and isinstance(entry.get("name"), str)
              and entry["name"].strip()
              and len(entry["name"].encode("utf-8")) <= 255]
    if not usable:
        raise ValueError("this package contains no usable voices.")
    manifest["voices"] = usable
    return manifest


def _package_member(zf, member):
    """Resolve a member named by the manifest, refusing anything unsafe.

    Only the manifest's own references are ever opened, and a reference that
    is absolute, drive-qualified or contains a path segment of "." or ".."
    is rejected outright, so a hostile package cannot write outside the
    staging directory."""
    if not member or not isinstance(member, str):
        raise ValueError("the package manifest has an invalid file reference")
    parts = member.replace("\\", "/").split("/")
    if (member.startswith(("/", "\\")) or ":" in member
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError(f"the package contains an unsafe path: {member!r}")
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise ValueError(f"the package is missing {member!r}") from None
    if info.is_dir():
        raise ValueError(f"{member!r} is a directory, not a file")
    if info.file_size > PACKAGE_MAX_MEMBER_BYTES:
        raise ValueError(f"{member!r} is far larger than a voice file should "
                         f"be ({info.file_size} bytes)")
    return info


def _extract_member(zf, info, dest: Path, expected_sha=None,
                    budget=None) -> None:
    if budget is not None:
        budget[0] -= info.file_size
        if budget[0] < 0:
            raise ValueError(
                "this package would write far more data than a set of voices "
                "ever needs; it is being refused")
    digest = hashlib.sha256()
    with zf.open(info) as source, open(dest, "wb") as out:
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
    if expected_sha and digest.hexdigest() != str(expected_sha):
        raise ValueError(f"{info.filename} is damaged: its checksum does not "
                         "match the manifest")


def _validate_state_file(path: Path) -> None:
    """Confirm a file really is a Pocket TTS voice embedding before it is put
    anywhere the engine would load it from."""
    import safetensors
    try:
        with safetensors.safe_open(str(path), framework="pt") as handle:
            keys = list(handle.keys())
    except Exception as exc:   # noqa: BLE001
        raise ValueError(f"the voice file cannot be read ({exc})") from None
    if not keys:
        raise ValueError("the voice file is empty")
    if any(key.count("/") != 1 for key in keys):
        raise ValueError("the voice file is not a Pocket TTS voice")


def _read_name_list(payload, offset):
    (count,) = struct.unpack_from("<I", payload, offset)
    offset += 4
    if count > PACKAGE_MAX_VOICES:
        raise ValueError("too many voice names in the request")
    names = []
    for _ in range(count):
        name, offset = _read_str16(payload, offset)
        names.append(name)
    return names, offset


# On Windows these address a device rather than a file, whatever extension
# follows them, so a voice may not be named after one.
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{i}" for i in range(1, 10)]
    + [f"lpt{i}" for i in range(1, 10)])


def _sanitize_name(name: str) -> str:
    """A voice name safe to use as an ini section and as a file name.

    Voice names arrive from the Clone dialog and from imported packages, and
    the name becomes <name>.safetensors on disk, so anything that would change
    which file is addressed has to go."""
    bad = set('[]=;\t"\\/|<>:*?') | set(_LINE_BREAKS)
    cleaned = "".join(c for c in name if c not in bad and ord(c) >= 32).strip()
    # A leading or trailing dot or space is dropped by the file system, and a
    # name of nothing but dots would be a relative path.
    cleaned = cleaned.strip(". ")[:60].strip(". ")
    if not cleaned:
        return ""
    if cleaned.split(".")[0].lower() in _RESERVED_NAMES:
        cleaned += "_"
    return cleaned


STORE = VoiceStore()
ENGINE = Engine()


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed")
        buf += part
    return buf


def _read_str16(payload, offset):
    (length,) = struct.unpack_from("<H", payload, offset)
    offset += 2
    value = payload[offset:offset + length].decode("utf-8", "replace")
    return value, offset + length


def _clip_utf8(text: str, limit: int) -> bytes:
    """UTF-8 bytes for `text`, never longer than `limit` bytes and never
    cut in the middle of a character."""
    # "replace" because the text can come from a stranger's manifest, and
    # an unencodable character must not take the connection down.
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return encoded
    return encoded[:limit].decode("utf-8", "ignore").encode("utf-8")


def _read_str32(payload, offset):
    (length,) = struct.unpack_from("<I", payload, offset)
    offset += 4
    value = payload[offset:offset + length].decode("utf-8", "replace")
    return value, offset + length


class Connection:
    def __init__(self, sock, addr, server):
        self.sock = sock
        self.server = server
        self.send_lock = threading.Lock()
        self.cancel = threading.Event()
        self.speak_thread = None
        self.addr = addr

    def send(self, resp_type, payload=b""):
        frame = struct.pack("<II", resp_type, len(payload)) + payload
        with self.send_lock:
            self.sock.sendall(frame)

    def send_text(self, resp_type, text):
        self.send(resp_type, text.encode("utf-8"))

    def run(self):
        try:
            while True:
                header = _recv_exact(self.sock, 8)
                cmd, size = struct.unpack("<II", header)
                payload = _recv_exact(self.sock, size) if size else b""
                if not self.dispatch(cmd, payload):
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self.cancel.set()
            if self.speak_thread and self.speak_thread.is_alive():
                self.speak_thread.join(timeout=5)
            try:
                self.sock.close()
            except OSError:
                pass

    def dispatch(self, cmd, payload):
        if cmd == CMD_PING:
            self.send(RESP_PONG, SPEED.packed())
        elif cmd == CMD_STOP:
            logger.debug("STOP received from %s", self.addr)
            self.cancel.set()
            self.send(RESP_OK)
        elif cmd == CMD_SPEAK:
            self.handle_speak(payload)
        elif cmd == CMD_LIST_VOICES:
            self.handle_list()
        elif cmd == CMD_CLONE:
            self.handle_clone(payload)
        elif cmd == CMD_SET_PUBLISHED:
            self.handle_set_published(payload)
        elif cmd == CMD_DELETE_VOICE:
            self.handle_delete(payload)
        elif cmd == CMD_UPDATE_MODELS:
            self.handle_update(payload)
        elif cmd == CMD_INFO:
            self.handle_info()
        elif cmd == CMD_EXPORT_VOICES:
            self.handle_export(payload)
        elif cmd == CMD_IMPORT_VOICES:
            self.handle_import(payload)
        elif cmd == CMD_INSPECT_PACKAGE:
            self.handle_inspect(payload)
        elif cmd == CMD_LIST_MODELS:
            self.handle_list_models()
        elif cmd == CMD_REMOVE_MODEL:
            self.handle_remove_model(payload)
        elif cmd == CMD_SET_VOICE_MODEL:
            self.handle_set_voice_model(payload)
        elif cmd == CMD_GET_SETTINGS:
            self.send(RESP_SETTINGS, struct.pack(
                "<BB", SETTINGS.get("quantize"), SETTINGS.get("continuation")))
        elif cmd == CMD_SET_SETTINGS:
            self.handle_set_settings(payload)
        elif cmd == CMD_SHUTDOWN:
            self.send(RESP_OK)
            self.server.shutdown_requested.set()
            return False
        else:
            self.send_text(RESP_ERROR, f"unknown command {cmd}")
        return True

    def handle_speak(self, payload):
        try:
            voice, offset = _read_str16(payload, 0)
            text, offset = _read_str32(payload, offset)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed SPEAK payload")
            return
        # Engines before 1.2.2 send no flags byte.
        flags = payload[offset] if offset < len(payload) else 0
        spell = bool(flags & SPEAK_SPELL)
        # A previous utterance on this connection must be finished or
        # cancelled before a new one starts.
        if self.speak_thread and self.speak_thread.is_alive():
            self.cancel.set()
            self.speak_thread.join(timeout=15)
        self.cancel = threading.Event()
        cancel = self.cancel

        def work():
            try:
                ENGINE.speak(voice, text, cancel,
                             lambda pcm: self.send(RESP_AUDIO, pcm), spell)
                self.send(RESP_AUDIO_END, SPEED.packed())
            except Exception as exc:   # noqa: BLE001
                logger.exception("speak failed")
                try:
                    self.send_text(RESP_ERROR, str(exc))
                    self.send(RESP_AUDIO_END, SPEED.packed())
                except OSError:
                    pass

        self.speak_thread = threading.Thread(target=work, daemon=True)
        self.speak_thread.start()

    def handle_list(self):
        voices = STORE.read()
        parts = [struct.pack("<I", len(voices))]
        for name, props in voices.items():
            encoded = name.encode("utf-8")[:255]
            gender = 1 if props.get("gender", "").lower() == "female" else 0
            try:
                lang = int(props.get("language", "409"), 16)
            except ValueError:
                lang = 0x409
            published = 1 if props.get("published", "0") == "1" else 0
            has_src = 1 if props.get("source") else 0
            model = voice_model_id(props).encode("utf-8")
            parts.append(struct.pack("<H", len(encoded)) + encoded
                         + struct.pack("<BIBB", gender, lang, published,
                                       has_src)
                         + struct.pack("<H", len(model)) + model)
        self.send(RESP_VOICES, b"".join(parts))

    def handle_clone(self, payload):
        try:
            name, offset = _read_str16(payload, 0)
            path, offset = _read_str16(payload, offset)
            lang, offset = _read_str16(payload, offset)
            (gender,) = struct.unpack_from("<B", payload, offset)
            offset += 1
            # Managers before 1.3.0 send no model: the standard one.
            model_id = (_read_str16(payload, offset)[0]
                        if offset < len(payload) else "")
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed CLONE payload")
            return
        try:
            ENGINE.clone(name, path, gender, lang,
                         lambda msg: self.send_text(RESP_PROGRESS, msg),
                         model_id or None)
            self.send(RESP_OK)
        except Exception as exc:   # noqa: BLE001
            logger.exception("clone failed")
            self.send_text(RESP_ERROR, str(exc))

    def handle_set_published(self, payload):
        try:
            name, offset = _read_str16(payload, 0)
            (published,) = struct.unpack_from("<B", payload, offset)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed payload")
            return

        found = []

        def mutate(voices):
            if name in voices:
                voices[name]["published"] = "1" if published else "0"
                found.append(True)
        try:
            STORE.update(mutate)
        except Exception as exc:   # noqa: BLE001
            self.send_text(RESP_ERROR, str(exc))
            return
        if found:
            self.send(RESP_OK)
        else:
            self.send_text(RESP_ERROR, f'no voice named "{name}"')

    def handle_delete(self, payload):
        try:
            name, _ = _read_str16(payload, 0)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed payload")
            return
        removed = {}

        def mutate(voices):
            if name in voices:
                removed.update(voices.pop(name))
        try:
            STORE.update(mutate)
        except Exception as exc:   # noqa: BLE001
            self.send_text(RESP_ERROR, str(exc))
            return
        if not removed:
            self.send_text(RESP_ERROR, f'no voice named "{name}"')
            return
        ENGINE.drop_cached_state(name)
        for rel in (removed.get("file"), removed.get("source")):
            if rel:
                try:
                    (VOICES_DIR / rel).unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not delete %s", rel)
        self.send(RESP_OK)

    def handle_update(self, payload):
        model_id = ""
        try:
            token, offset = _read_str16(payload, 0)
            if offset < len(payload):
                model_id, _ = _read_str16(payload, offset)
        except (struct.error, IndexError):
            token = ""
        try:
            ENGINE.update_models(
                token, lambda msg: self.send_text(RESP_PROGRESS, msg),
                model_id or None)
            self.send(RESP_OK)
        except Exception as exc:   # noqa: BLE001
            logger.exception("model update failed")
            hint = ""
            text = str(exc)
            if "401" in text or "403" in text or "gated" in text.lower():
                hint = ("  The Kyutai model requires a (free) Hugging Face "
                        "account: accept the terms at "
                        "https://huggingface.co/kyutai/pocket-tts and paste "
                        "a read token into the update dialog.")
            self.send_text(RESP_ERROR, text + hint)

    def handle_info(self):
        version = _pocket_tts_version()
        loaded = ENGINE.loaded_ids()
        state = ("ready, models loaded: " + ", ".join(loaded) if loaded
                 else f"model not loaded: {ENGINE.load_error}")
        info = (f"Pocket TTS SAPI5 {APP_VERSION}; pocket-tts {version}; "
                f"engine {state}; data dir {DATA_DIR}; "
                "sample rate 24000 Hz")
        self.send_text(RESP_INFO, info)

    def handle_list_models(self):
        loaded = set(ENGINE.loaded_ids())
        voices = STORE.read()
        used = {}
        for props in voices.values():
            mid = voice_model_id(props)
            used[mid] = used.get(mid, 0) + 1
        parts = [struct.pack("<I", len(catalog.CATALOG))]
        for info in catalog.CATALOG:
            fields = [_clip_utf8(text, 4000) for text in
                      (info.id, info.title, info.description,
                       ENGINE.load_errors.get(info.id, ""))]
            record = b"".join(struct.pack("<H", len(f)) + f for f in fields)
            record += struct.pack(
                "<BBIII",
                1 if catalog.is_installed(MODELS_DIR, info.id) else 0,
                1 if info.id in loaded else 0,
                info.size_mb, int(info.lcid, 16), used.get(info.id, 0))
            parts.append(record)
        self.send(RESP_MODELS, b"".join(parts))

    def handle_remove_model(self, payload):
        try:
            model_id, _ = _read_str16(payload, 0)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed REMOVE_MODEL payload")
            return
        try:
            self.send_text(RESP_OK, ENGINE.remove_model(model_id))
        except ValueError as exc:
            logger.info("not removing model %s: %s", model_id, exc)
            self.send_text(RESP_ERROR, str(exc))
        except Exception as exc:   # noqa: BLE001
            logger.exception("removing model %s failed", model_id)
            self.send_text(RESP_ERROR, str(exc))

    def handle_set_voice_model(self, payload):
        try:
            name, offset = _read_str16(payload, 0)
            model_id, offset = _read_str16(payload, offset)
            (make_copy,) = struct.unpack_from("<B", payload, offset)
            copy_name, _ = _read_str16(payload, offset + 1)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed SET_VOICE_MODEL payload")
            return
        try:
            summary = ENGINE.set_voice_model(
                name, model_id, copy_name if make_copy else "",
                lambda msg: self.send_text(RESP_PROGRESS, msg))
            self.send_text(RESP_OK, summary)
        except Exception as exc:   # noqa: BLE001
            logger.exception("changing the model of %r failed", name)
            self.send_text(RESP_ERROR, str(exc))

    def handle_set_settings(self, payload):
        try:
            quantize, continuation = struct.unpack_from("<BB", payload, 0)
        except struct.error:
            self.send_text(RESP_ERROR, "malformed SET_SETTINGS payload")
            return
        try:
            summary = ENGINE.apply_settings(
                bool(quantize), bool(continuation),
                lambda msg: self.send_text(RESP_PROGRESS, msg))
            self.send_text(RESP_OK, summary)
        except Exception as exc:   # noqa: BLE001
            logger.exception("changing the settings failed")
            self.send_text(RESP_ERROR, str(exc))

    def handle_export(self, payload):
        try:
            dest, offset = _read_str16(payload, 0)
            (include_sources,) = struct.unpack_from("<B", payload, offset)
            names, _ = _read_name_list(payload, offset + 1)
        except (struct.error, IndexError, ValueError):
            self.send_text(RESP_ERROR, "malformed EXPORT payload")
            return
        try:
            summary = ENGINE.export_voices(
                names, dest, bool(include_sources),
                lambda msg: self.send_text(RESP_PROGRESS, msg))
            self.send_text(RESP_OK, summary)
        except Exception as exc:   # noqa: BLE001
            logger.exception("export failed")
            self.send_text(RESP_ERROR, str(exc))

    def handle_import(self, payload):
        try:
            path, offset = _read_str16(payload, 0)
            publish, collision = struct.unpack_from("<BB", payload, offset)
            names, _ = _read_name_list(payload, offset + 2)
        except (struct.error, IndexError, ValueError):
            self.send_text(RESP_ERROR, "malformed IMPORT payload")
            return
        try:
            summary = ENGINE.import_voices(
                path, names, bool(publish), int(collision),
                lambda msg: self.send_text(RESP_PROGRESS, msg))
            self.send_text(RESP_OK, summary)
        except Exception as exc:   # noqa: BLE001
            logger.exception("import failed")
            self.send_text(RESP_ERROR, str(exc))

    def handle_inspect(self, payload):
        try:
            path, _ = _read_str16(payload, 0)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed INSPECT payload")
            return
        try:
            summary, rows = ENGINE.inspect_package(path)
        except Exception as exc:   # noqa: BLE001
            logger.info("inspecting %r failed: %s", path, exc)
            self.send_text(RESP_ERROR, str(exc))
            return
        try:
            text = _clip_utf8(summary, 60000)
            parts = [struct.pack("<H", len(text)) + text,
                     struct.pack("<I", len(rows))]
            for name, female, has_source, status in rows:
                encoded = _clip_utf8(name, 255)
                parts.append(struct.pack("<H", len(encoded)) + encoded
                             + struct.pack("<BBB", 1 if female else 0,
                                           1 if has_source else 0,
                                           int(status)))
        except Exception as exc:   # noqa: BLE001
            logger.exception("describing the package failed")
            self.send_text(RESP_ERROR, str(exc))
            return
        self.send(RESP_PACKAGE, b"".join(parts))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self):
        self.shutdown_requested = threading.Event()
        self.listener = None
        self.port = None

    def bind(self):
        for port in PORTS:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
                sock.listen(8)
            except OSError:
                sock.close()
                continue
            self.listener = sock
            self.port = port
            return True
        return False

    def serve(self):
        PORT_FILE.write_text(str(self.port), encoding="ascii")
        logger.info("listening on 127.0.0.1:%d", self.port)
        self.listener.settimeout(1.0)
        while not self.shutdown_requested.is_set():
            try:
                sock, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = Connection(sock, addr, self)
            threading.Thread(target=conn.run, daemon=True).start()
        logger.info("shutting down")
        try:
            self.listener.close()
        except OSError:
            pass


def _already_running() -> bool:
    """Another host instance owns one of our ports and answers PING."""
    candidates = []
    try:
        candidates.append(int(PORT_FILE.read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        pass
    candidates.extend(p for p in PORTS if p not in candidates)
    for port in candidates:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3) as s:
                s.sendall(struct.pack("<II", CMD_PING, 0))
                header = _recv_exact(s, 8)
                resp, _ = struct.unpack("<II", header)
                if resp == RESP_PONG:
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
#
# Windows ranks a process that has no window of its own by the program that
# started it: while that program is in focus the host runs at full speed, and
# once the program closes the host counts as background work, which Windows
# may move to a slower CPU speed or to the power-saving cores. Reopening the
# program does not undo that, because the running host is not the new
# program's child, so on a PC with little speed to spare the speech broke up
# from the second session on, until the host was restarted. The host is
# foreground work for whoever is listening, and tells Windows so explicitly,
# which holds however it was started.

_PROCESS_POWER_THROTTLING = 4           # PROCESS_INFORMATION_CLASS
_POWER_THROTTLING_CURRENT_VERSION = 1
_POWER_THROTTLING_EXECUTION_SPEED = 0x1


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


def _opt_out_of_background_throttling():
    from ctypes import wintypes

    # A private kernel32, so these prototypes do not leak into ctypes.windll.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        set_information = kernel32.SetProcessInformation
    except AttributeError:
        logger.warning("scheduling: this Windows cannot opt out of power "
                       "throttling")
        return
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                ctypes.c_void_p, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    # Taking control of execution speed and leaving it switched off is the
    # documented way to ask for HighQoS.
    state = _PowerThrottlingState(_POWER_THROTTLING_CURRENT_VERSION,
                                  _POWER_THROTTLING_EXECUTION_SPEED, 0)
    if set_information(kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING,
                       ctypes.byref(state), ctypes.sizeof(state)):
        logger.info("scheduling: running as foreground work (HighQoS)")
    else:
        # Windows 10 before 1709 has no power throttling to opt out of.
        logger.warning("scheduling: could not opt out of power throttling "
                       "(error %d)", ctypes.get_last_error())


# ---------------------------------------------------------------------------
# Standard streams
# ---------------------------------------------------------------------------
#
# PocketTTSHost.exe is a renamed pythonw.exe, so the host has no console and
# sys.stdout and sys.stderr are None. Libraries that write to them without
# checking then fail: huggingface_hub's download progress bar raised
# "'NoneType' object has no attribute 'write'" and aborted every model update.
# The output has nowhere to go, so it is discarded.

def _ensure_std_streams():
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main():
    _ensure_std_streams()
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2,
                                  encoding="utf-8")
    level = getattr(logging,
                    os.environ.get("POCKETTTS_LOG_LEVEL", "INFO").upper(),
                    logging.INFO)
    logging.basicConfig(
        level=level, handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("host starting; data dir %s; python %s", DATA_DIR, sys.version)

    if _already_running():
        logger.info("another host instance is already running; exiting")
        return 0

    server = Server()
    if not server.bind():
        logger.error("all ports busy but no host answered PING; exiting")
        return 1

    # Give this process a recognisable description for Task Manager users.
    try:
        ctypes.windll.kernel32.SetConsoleTitleW("Pocket TTS engine host")
    except OSError:
        pass
    _opt_out_of_background_throttling()

    try:
        for config in catalog.rewrite_data_dir(MODELS_DIR, DATA_DIR):
            logger.info("pointed %s at %s", config, DATA_DIR)
    except OSError:
        logger.warning("could not rewrite the model configs", exc_info=True)
    ENGINE.load()
    threading.Thread(target=ENGINE.after_load, daemon=True).start()
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
