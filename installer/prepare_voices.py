"""Builds the model and default-voice payload the installer bundles.

For every model the installer ships (SHIPPED_MODELS), stages Kyutai's weights
and tokenizer in installer/staging/models/<id>/ with a config.yaml that points
at C:/ProgramData/PocketTTS/models/<id>/ (the host rewrites that path at start-up
when the data folder lives elsewhere). Then embeds the default voice samples,
which come from Kyutai's public kyutai/tts-voices repository, with those
*staged* weights, so the embeddings always match the shipped models, and writes
them with voices.ini and defaults.ini into installer/staging/voices.

Run with the dev venv from the repository root:

    %USERPROFILE%\\.pockettts\\venv\\Scripts\\python.exe installer\\prepare_voices.py [MODELS_DIR]

MODELS_DIR is a models folder that already holds the shipped models, laid out
as the host keeps them (<id>\\model.safetensors and its tokenizer), such as
C:\\ProgramData\\PocketTTS\\models. Without it, the models are downloaded from
Hugging Face, which needs a token that has accepted Kyutai's terms at
huggingface.co/kyutai/pocket-tts: set HF_TOKEN, or sign in with `hf auth login`.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "host"))
import pockettts_models as catalog  # noqa: E402

STAGING = ROOT / "installer" / "staging"
CANONICAL_MODELS = Path(catalog.CANONICAL_DATA_DIR) / "models"

# The models in the installer; any other one is a download in the Voice Manager.
SHIPPED_MODELS = ("english", "english_7m_refl")

# Embeddings are made with this recipe; it matches the host's EMBEDDING_RECIPE.
EMBEDDING_RECIPE = 2

# name -> (hf path in kyutai/tts-voices, gender)
SAMPLES = {
    "Alba": ("alba-mackenna/casual.wav", "Female"),
    "Jane": ("vctk/p339_023_enhanced.wav", "Female"),
    "George": ("vctk/p315_023_enhanced.wav", "Male"),
    "Michael": ("vctk/p360_023_enhanced.wav", "Male"),
}

# (voice name, sample, model): the standard voices, and a fast set on the 7M
# model for quick response while navigating with a screen reader.
DEFAULT_VOICES = (
    [(name, name, "english") for name in SAMPLES]
    + [(f"{name} Fast", name, "english_7m_refl") for name in SAMPLES]
)


def stage_model(model_id, source_models):
    folder = STAGING / "models" / model_id
    folder.mkdir(parents=True, exist_ok=True)
    config = catalog.upstream_config(model_id)
    tokenizer = catalog.tokenizer_name(config)
    if source_models is not None:
        print(f"Staging {model_id} from {source_models / model_id}...")
        for name in ("model.safetensors", tokenizer):
            shutil.copy(source_models / model_id / name, folder / name)
    else:
        token = os.environ.get("HF_TOKEN") or None
        with tempfile.TemporaryDirectory() as tmp:
            downloaded = catalog.download(Path(tmp), model_id, token, print)
            for name in ("model.safetensors", tokenizer):
                shutil.copy(downloaded / name, folder / name)
    for stale in folder.glob("tokenizer.*"):
        if stale.name != tokenizer:
            stale.unlink()
    # The shipped config refers to where the installer puts the files.
    catalog.write_config(
        catalog.local_config(config, CANONICAL_MODELS / model_id),
        folder / "config.yaml", catalog.config_header(model_id))
    return folder


def load_staged(model_id):
    from pocket_tts import TTSModel
    folder = STAGING / "models" / model_id
    config = catalog.local_config(catalog.upstream_config(model_id), folder)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        catalog.write_config(config, path)
        model = TTSModel.load_model(config=str(path))
    if not model.has_voice_cloning:
        raise SystemExit(f"{model_id} has no voice cloning; use the weights "
                         "from kyutai/pocket-tts")
    return model


def sha256(path):
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    from pocket_tts.models.model_state import export_model_state

    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    for stale in (STAGING / "models").glob("*"):
        if stale.is_dir() and stale.name not in SHIPPED_MODELS:
            shutil.rmtree(stale)
    for model_id in SHIPPED_MODELS:
        stage_model(model_id, source)

    voices_dir = STAGING / "voices"
    src_dir = voices_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    for stale in voices_dir.glob("*.safetensors"):
        stale.unlink()

    samples = {}
    for name, (hf_path, _) in SAMPLES.items():
        wav = src_dir / (name + Path(hf_path).suffix.lower())
        if not wav.exists():
            from huggingface_hub import hf_hub_download
            print(f"Downloading {name} from {hf_path} ...")
            shutil.copy(hf_hub_download(repo_id="kyutai/tts-voices",
                                        filename=hf_path), wav)
        samples[name] = wav

    ini_lines = []
    for model_id in SHIPPED_MODELS:
        print(f"Loading the staged {model_id}...")
        model = load_staged(model_id)
        model_sha = sha256(STAGING / "models" / model_id / "model.safetensors")
        for name, sample, voice_model in DEFAULT_VOICES:
            if voice_model != model_id:
                continue
            print(f"Embedding {name} ...")
            state = model.get_state_for_audio_prompt(samples[sample],
                                                     truncate=True)
            export_model_state(state, voices_dir / f"{name}.safetensors")
            ini_lines += [
                f"[{name}]",
                f"file={name}.safetensors",
                f"gender={SAMPLES[sample][1]}",
                "language=409",
                "published=1",
                f"source=src/{samples[sample].name}",
                f"model={model_id}",
                f"model_sha256={model_sha}",
                f"embedding={EMBEDDING_RECIPE}",
                "",
            ]

    # voices.ini is installed only on a fresh install; defaults.ini on every
    # install, so that the host can add default voices new in this version.
    for filename in ("voices.ini", "defaults.ini"):
        with open(voices_dir / filename, "w", encoding="utf-16",
                  newline="") as handle:
            handle.write("\r\n".join(ini_lines))
    print("Done. Staged payload in", STAGING)


if __name__ == "__main__":
    sys.exit(main())
