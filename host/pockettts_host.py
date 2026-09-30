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
from pockettts_text import expand_numbers  # noqa: E402

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
# SHA-256 of the model weights the voices were last built with.
VOICES_MODEL_STAMP = VOICES_DIR / "model.sha256"
CONFIG_YAML = MODELS_DIR / "english" / "config.yaml"

PORTS = (17853, 17854, 17855, 17856, 17857)
PORT_FILE = _local_dir() / "host.port"
LOG_FILE = _local_dir() / "host.log"

# Kept in step with CMakeLists.txt and installer/pockettts.iss.
APP_VERSION = "1.2.1"

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

PACKAGE_FORMAT = 1
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


def _use_trained_gelu():
    """Kyutai trained the released weights with the tanh approximation of
    GELU, but pocket-tts 3.0.2 computes the exact GELU in the transformer
    feed-forward block. pocket-tts 3.1.0 switched to the tanh form
    (kyutai-labs/pocket-tts#278); the bundled 3.0.2 does the same here."""
    import torch.nn.functional as F
    from pocket_tts.modules.transformer import StreamingTransformerLayer

    def _ff_block(self, x):
        x_orig = x
        x = self.norm2(x)
        update = self.linear2(F.gelu(self.linear1(x), approximate="tanh"))
        return x_orig.to(update) + self.layer_scale_2(update)

    StreamingTransformerLayer._ff_block = _ff_block


class Engine:
    def __init__(self):
        self.model = None
        self.gen_lock = threading.Lock()   # pocket-tts is not thread-safe
        self.state_cache = {}              # voice name -> model state
        self.state_cache_order = []
        self.load_error = None

    def load(self):
        try:
            from pocket_tts import TTSModel
            _use_trained_gelu()
            if not CONFIG_YAML.exists():
                raise FileNotFoundError(
                    f"Model config not found: {CONFIG_YAML}. "
                    "Reinstall or run a model update.")
            self._fix_config_paths()
            t0 = time.monotonic()
            self.model = TTSModel.load_model(config=str(CONFIG_YAML))
            logger.info("model loaded in %.1fs (voice cloning: %s)",
                        time.monotonic() - t0, self.model.has_voice_cloning)
            self.load_error = None
        except Exception as exc:
            logger.exception("model load failed")
            self.load_error = str(exc)

    def _fix_config_paths(self):
        """The bundled config references C:/ProgramData/PocketTTS; rewrite it
        when the data directory lives elsewhere (relocated ProgramData or a
        POCKETTTS_DATA_DIR override)."""
        canonical = "C:/ProgramData/PocketTTS"
        actual = str(DATA_DIR).replace("\\", "/")
        if actual.lower() == canonical.lower():
            return
        try:
            text = CONFIG_YAML.read_text(encoding="utf-8")
            if canonical in text:
                CONFIG_YAML.write_text(
                    text.replace(canonical, actual), encoding="utf-8")
                logger.info("rewrote model config paths to %s", actual)
        except OSError:
            logger.warning("could not rewrite config paths", exc_info=True)

    def after_load(self):
        """Background work once the model is loaded: bring the voices in
        line with it, then warm up."""
        try:
            self.sync_voices_with_model()
        except Exception:
            logger.exception("voice rebuild failed (non-fatal)")
        self.warm_up()

    def sync_voices_with_model(self):
        """A voice only sounds right on the weights that built it. When the
        model differs from the one the voices were last built with (a new
        version installed over an old one, or model files replaced by hand),
        rebuild them from their audio samples."""
        if self.model is None or not self.model.has_voice_cloning:
            return
        current = _model_fingerprint().get("sha256")
        try:
            recorded = VOICES_MODEL_STAMP.read_text(encoding="ascii").strip()
        except OSError:
            recorded = ""
        if not current or recorded == current:
            return
        logger.info("voices were built with model %s, the model is now %s; "
                    "rebuilding them", recorded[:12] or "(unrecorded)",
                    current[:12])
        self.rebuild_voices(lambda msg: logger.info("voices: %s", msg))

    def rebuild_voices(self, progress):
        """Re-embed every voice that still has its audio sample with the
        loaded model, and record that model as the voices' model."""
        from pocket_tts.models.model_state import export_model_state
        for name, props in STORE.read().items():
            source = props.get("source", "")
            if not source:
                progress(f'Voice "{name}" has no source audio; leaving as is.')
                continue
            src = VOICES_DIR / source
            if not src.exists():
                progress(f'Source audio for "{name}" is missing; leaving as is.')
                continue
            progress(f'Rebuilding voice "{name}" for the new model...')
            dest = VOICES_DIR / props.get("file", name + ".safetensors")
            tmp = dest.with_name(dest.name + ".new")
            try:
                with self.gen_lock:
                    state = self.model.get_state_for_audio_prompt(
                        src, truncate=True)
                # Replaced in one step: a voice may be loaded to speak meanwhile.
                export_model_state(state, tmp)
                os.replace(tmp, dest)
            except Exception:   # noqa: BLE001
                logger.exception("could not rebuild voice %r", name)
                progress(f'Could not rebuild voice "{name}"; leaving as is.')
        self.clear_cache()
        sha = _model_fingerprint().get("sha256")
        if sha:
            try:
                VOICES_MODEL_STAMP.write_text(sha + "\n", encoding="ascii")
            except OSError:
                logger.warning("could not record the voices' model",
                               exc_info=True)

    def warm_up(self):
        """First generation initialises lazy torch state; do it off the
        critical path so the first real utterance is fast."""
        try:
            voices = STORE.read()
            name = next(iter(voices), None)
            if name is None:
                return
            state = self.get_voice_state(name, voices)
            produced = 0
            t0 = time.monotonic()
            with self.gen_lock:
                for pcm in self._stream_chunks(state, "Ready.",
                                               threading.Event()):
                    produced += len(pcm)
            audio_ms = produced / 2 / POCKETTTS_SAMPLE_RATE * 1000
            SPEED.update(audio_ms, (time.monotonic() - t0) * 1000)
            logger.info("warm-up generation done (machine x%.2f realtime)",
                        SPEED.get())
        except Exception:
            logger.exception("warm-up failed (non-fatal)")

    def get_voice_state(self, name: str, voices: dict):
        if name in self.state_cache:
            return self.state_cache[name]
        props = voices.get(name)
        if props is None:
            raise KeyError(f"unknown voice: {name}")
        path = VOICES_DIR / props.get("file", name + ".safetensors")
        if not path.exists():
            raise FileNotFoundError(f"voice file missing: {path}")
        state = self.model.get_state_for_audio_prompt(path)
        self.state_cache[name] = state
        self.state_cache_order.append(name)
        while len(self.state_cache_order) > 6:
            evicted = self.state_cache_order.pop(0)
            self.state_cache.pop(evicted, None)
        return state

    def drop_cached_state(self, name: str):
        self.state_cache.pop(name, None)
        if name in self.state_cache_order:
            self.state_cache_order.remove(name)

    def clear_cache(self):
        self.state_cache.clear()
        self.state_cache_order.clear()

    # -- generation ---------------------------------------------------------

    def _stream_chunks(self, model_state, text, cancel):
        """Yield int16 PCM byte chunks; honours `cancel` between model steps
        so screen-reader interruptions take effect within ~one frame."""
        from pocket_tts.default_parameters import MAX_TOKEN_PER_CHUNK
        from pocket_tts.models.text_chunking import (
            prepare_text_prompt, split_into_best_sentences)

        # The model has no tokens for numbers and mangles digit strings ("224"
        # was heard as "24"), so numbers are written out in words first.
        spoken = expand_numbers(text)
        if spoken != text:
            logger.debug("numbers written out: %r -> %r", text, spoken)
            text = spoken

        model = self.model
        chunks = split_into_best_sentences(
            model.flow_lm.conditioner.tokenizer, text, MAX_TOKEN_PER_CHUNK,
            model.pad_with_spaces_for_short_inputs,
            remove_semicolons=model.remove_semicolons)
        for chunk in chunks:
            if cancel.is_set():
                return
            prompt, guess = prepare_text_prompt(
                chunk, model.pad_with_spaces_for_short_inputs,
                model.remove_semicolons)
            frames_after_eos = model.model_recommended_frames_after_eos
            if frames_after_eos is None:
                frames_after_eos = guess + 2
            yield from self._stream_one(model_state, prompt,
                                        frames_after_eos, cancel)

    def _stream_one(self, model_state, text, frames_after_eos, cancel):
        """Cancellable re-implementation of pocket-tts
        _generate_audio_stream_short_text (pinned to pocket-tts 3.0.2, plus
        the MIN_FRAMES_BEFORE_EOS guard from 3.2.0): latents are produced on
        this thread and decoded by a worker so throughput matches upstream,
        but both loops watch `cancel`."""
        import copy as _copy
        import queue as _queue

        import torch
        from pocket_tts.modules.stateful_module import (
            increment_steps, init_states)

        model = self.model
        model_state = _copy.deepcopy(model_state)

        prepared = model.flow_lm.conditioner.prepare(text)
        token_count = prepared.tokens.shape[1]
        max_gen_len = model._estimate_max_gen_len(token_count)
        mimi_steps = int(model.mimi.encoder_frame_rate / model.mimi.frame_rate)
        mimi_seq_len = max_gen_len * mimi_steps

        current_end = model._flow_lm_current_end(model_state)
        model._expand_kv_cache(
            model_state, sequence_length=current_end + token_count + max_gen_len)
        model._run_flow_lm_and_increment_step(
            model_state=model_state, text_tokens=prepared.tokens)

        latents_q = _queue.Queue()
        out_q = _queue.Queue()

        def decoder():
            try:
                mimi_state = init_states(model.mimi, batch_size=1,
                                         sequence_length=mimi_seq_len)
                while True:
                    latent = latents_q.get()
                    if latent is None or cancel.is_set():
                        break
                    decoding_input = (latent * model.flow_lm.emb_std
                                      + model.flow_lm.emb_mean)
                    frame = model.mimi.decode_from_latent(
                        decoding_input, mimi_state)
                    increment_steps(model.mimi, mimi_state,
                                    increment=mimi_steps)
                    pcm = (frame[0, 0].clamp(-1, 1) * 32767).short()
                    out_q.put(("chunk", pcm.numpy().tobytes()))
            except Exception as exc:   # noqa: BLE001
                out_q.put(("error", exc))
            finally:
                out_q.put(("done", None))

        decoder_thread = threading.Thread(target=decoder, daemon=True)
        decoder_thread.start()

        def producer():
            try:
                with torch.no_grad():
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
                                and step >= MIN_FRAMES_BEFORE_EOS):
                            eos_step = step
                        if (eos_step is not None
                                and step >= eos_step + frames_after_eos):
                            break
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

    def speak(self, voice_name, text, cancel, emit):
        """Generate `text` with `voice_name`, calling emit(pcm_bytes)."""
        if self.model is None:
            raise RuntimeError(self.load_error or "model not loaded")
        t0 = time.monotonic()
        voices = STORE.read()
        state = self.get_voice_state(voice_name, voices)
        emitted = 0
        emit_seconds = 0.0
        with self.gen_lock:
            if cancel.is_set():
                logger.debug("speak cancelled before generation started")
                return
            for pcm in self._stream_chunks(state, text, cancel):
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

    def clone(self, name, audio_path, gender, language, progress):
        if self.model is None:
            raise RuntimeError(self.load_error or "model not loaded")
        if not self.model.has_voice_cloning:
            raise RuntimeError(
                "The installed model does not support voice cloning.")
        src = Path(audio_path)
        if not src.exists():
            raise FileNotFoundError(f"audio file not found: {src}")
        safe = _sanitize_name(name)
        if not safe:
            raise ValueError("voice name is empty or invalid")
        voices = STORE.read()
        if safe in voices:
            raise ValueError(f'a voice named "{safe}" already exists')

        from pocket_tts.models.model_state import export_model_state
        progress("Analyzing the audio sample (this can take a minute)...")
        with self.gen_lock:
            state = self.model.get_state_for_audio_prompt(src, truncate=True)
        progress("Saving the voice...")
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        VOICES_SRC_DIR.mkdir(parents=True, exist_ok=True)
        export_model_state(state, VOICES_DIR / f"{safe}.safetensors")
        src_copy = VOICES_SRC_DIR / (safe + src.suffix.lower())
        shutil.copy(src, src_copy)

        def mutate(voices):
            voices[safe] = {
                "file": f"{safe}.safetensors",
                "gender": "Female" if gender == 1 else "Male",
                "language": language or "409",
                "published": "0",
                "source": f"src/{src_copy.name}",
            }
        STORE.update(mutate)
        self.state_cache[safe] = state
        self.state_cache_order.append(safe)
        return safe

    # -- model update -------------------------------------------------------

    def update_models(self, token, progress):
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import disable_progress_bars

        # No one can see a progress bar in the host; the Voice Manager shows
        # the progress messages instead.
        disable_progress_bars()
        token = token or None
        english = MODELS_DIR / "english"
        english.mkdir(parents=True, exist_ok=True)

        progress("Downloading the latest English model from Hugging Face...")
        model_file = hf_hub_download(
            repo_id="kyutai/pocket-tts",
            filename="languages/english/model.safetensors", token=token)
        progress("Downloading the tokenizer...")
        tok_file = hf_hub_download(
            repo_id="kyutai/pocket-tts-without-voice-cloning",
            filename="languages/english/tokenizer.model", token=token)

        progress("Installing the new model files...")
        # The previous files are kept as *.old until the new model loads.
        installed = []
        for src, name in ((model_file, "model.safetensors"),
                          (tok_file, "tokenizer.model")):
            dest = english / name
            shutil.copy(src, english / (name + ".new"))
            if dest.exists():
                os.replace(dest, english / (name + ".old"))
            os.replace(english / (name + ".new"), dest)
            installed.append(dest)

        progress("Loading the new model...")
        with self.gen_lock:
            self.clear_cache()
            self.load()
        # A failed load keeps the previous model in memory and sets load_error.
        if self.model is None or self.load_error:
            error = self.load_error
            for dest in installed:
                old = dest.with_name(dest.name + ".old")
                if old.exists():
                    os.replace(old, dest)
            with self.gen_lock:
                self.load()
            raise RuntimeError("the new model failed to load, so the previous "
                               f"one was kept: {error}")
        for dest in installed:
            dest.with_name(dest.name + ".old").unlink(missing_ok=True)

        # Voice states are tied to the model weights.
        self.rebuild_voices(progress)
        progress("Model update complete.")

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

        progress("Identifying the AI model these voices were made with...")
        model = _model_fingerprint()

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
                    "format": PACKAGE_FORMAT,
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
        package_sha = str((manifest.get("model") or {}).get("sha256") or "")
        local_sha = str(_model_fingerprint().get("sha256") or "")
        same_model = bool(package_sha) and package_sha == local_sha
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
            status = 0 if same_model else (1 if has_source else 2)
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
        if same_model:
            parts.append("They were made with the same AI model as this "
                         "computer, so they will sound exactly as intended.")
        else:
            rebuildable = sum(1 for row in rows if row[3] == 1)
            risky = sum(1 for row in rows if row[3] == 2)
            parts.append("They were made with a different AI model than this "
                         "computer.")
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
        package_sha = str((manifest.get("model") or {}).get("sha256") or "")
        local_sha = str(_model_fingerprint().get("sha256") or "")
        same_model = bool(package_sha) and package_sha == local_sha

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
                    self._install_package_voice(
                        zf, entry, target, replacing, same_model, publish,
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
                if not same_model:
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

    def _install_package_voice(self, zf, entry, target, replacing, same_model,
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

            if not same_model:
                if staged_source is not None:
                    if self.model is None:
                        raise RuntimeError(
                            self.load_error
                            or "the AI model is not loaded, so this voice "
                               "cannot be rebuilt for it")
                    if not self.model.has_voice_cloning:
                        raise RuntimeError(
                            "this voice was made with a different AI model, "
                            "and the installed model cannot rebuild voices")
                    progress(f'Rebuilding "{target}" for your AI model '
                             "(this can take a minute)...")
                    from pocket_tts.models.model_state import export_model_state
                    with self.gen_lock:
                        state = self.model.get_state_for_audio_prompt(
                            staged_source, truncate=True)
                    export_model_state(state, staged_state)
                    _validate_state_file(staged_state)
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
        import pocket_tts
        return str(getattr(pocket_tts, "__version__", "unknown"))
    except Exception:   # noqa: BLE001
        return "unknown"


_MODEL_FINGERPRINT_CACHE = {}


def _model_fingerprint() -> dict:
    """Size and SHA-256 of the active model weights.

    A voice embedding only sounds right on the weights that produced it, so a
    package records this and an import compares it. Hashing 220 MB costs about
    a second, so the answer is cached per (size, mtime)."""
    path = MODELS_DIR / "english" / "model.safetensors"
    try:
        stat = path.stat()
    except OSError:
        return {"file": path.name, "sha256": "", "size": 0}
    key = (str(path).lower(), stat.st_size, int(stat.st_mtime))
    cached = _MODEL_FINGERPRINT_CACHE.get(key)
    if cached is None:
        cached = {"file": path.name, "sha256": _sha256_file(path),
                  "size": stat.st_size}
        _MODEL_FINGERPRINT_CACHE.clear()
        _MODEL_FINGERPRINT_CACHE[key] = cached
    return cached


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
    if not isinstance(fmt, int) or fmt > PACKAGE_FORMAT:
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
            text, _ = _read_str32(payload, offset)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed SPEAK payload")
            return
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
                             lambda pcm: self.send(RESP_AUDIO, pcm))
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
            parts.append(struct.pack("<H", len(encoded)) + encoded
                         + struct.pack("<BIBB", gender, lang, published,
                                       has_src))
        self.send(RESP_VOICES, b"".join(parts))

    def handle_clone(self, payload):
        try:
            name, offset = _read_str16(payload, 0)
            path, offset = _read_str16(payload, offset)
            lang, offset = _read_str16(payload, offset)
            (gender,) = struct.unpack_from("<B", payload, offset)
        except (struct.error, IndexError):
            self.send_text(RESP_ERROR, "malformed CLONE payload")
            return
        try:
            ENGINE.clone(name, path, gender, lang,
                         lambda msg: self.send_text(RESP_PROGRESS, msg))
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
        try:
            token, _ = _read_str16(payload, 0)
        except (struct.error, IndexError):
            token = ""
        try:
            ENGINE.update_models(
                token, lambda msg: self.send_text(RESP_PROGRESS, msg))
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
        import pocket_tts
        version = getattr(pocket_tts, "__version__", "3.x")
        state = "ready" if ENGINE.model is not None else (
            f"model not loaded: {ENGINE.load_error}")
        info = (f"Pocket TTS SAPI5 {APP_VERSION}; pocket-tts {version}; "
                f"engine {state}; data dir {DATA_DIR}; "
                "sample rate 24000 Hz")
        self.send_text(RESP_INFO, info)

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

    ENGINE.load()
    threading.Thread(target=ENGINE.after_load, daemon=True).start()
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
