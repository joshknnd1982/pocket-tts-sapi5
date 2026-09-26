"""Builds the model and default-voice payload the installer bundles.

Stages the English model weights and tokenizer next to the tracked
installer/staging/models/english/config.yaml, then embeds a handful of
permissively licensed voice samples from Kyutai's public kyutai/tts-voices
repository with those *staged* weights (so the embeddings always match the
shipped model), and writes them plus voices.ini into installer/staging/voices.

Run with the dev venv (or the assembled runtime) from the repository root:
    %USERPROFILE%\\.pockettts\\venv\\Scripts\\python.exe installer\\prepare_voices.py [MODEL_DIR]

MODEL_DIR holds model.safetensors and tokenizer.model; it defaults to
C:\\ProgramData\\PocketTTS\\models\\english.
"""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "installer" / "staging"
DEFAULT_MODEL_DIR = Path(r"C:/ProgramData/PocketTTS/models/english")
# The path the shipped config.yaml refers to its files by.
CONFIG_MODEL_DIR = "C:/ProgramData/PocketTTS/models/english"

# name -> (hf path in kyutai/tts-voices, gender)
DEFAULT_VOICES = {
    "Alba": ("alba-mackenna/casual.wav", "Female"),
    "Jane": ("vctk/p339_023_enhanced.wav", "Female"),
    "George": ("vctk/p315_023_enhanced.wav", "Male"),
    "Michael": ("vctk/p360_023_enhanced.wav", "Male"),
}


def _host_module():
    """The engine host, so voices are embedded exactly as it embeds them."""
    spec = importlib.util.spec_from_file_location(
        "pockettts_host", ROOT / "host" / "pockettts_host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    return host


def main():
    from pocket_tts import TTSModel
    from pocket_tts.models.model_state import export_model_state

    model_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MODEL_DIR
    models_dir = STAGING / "models" / "english"
    config = models_dir / "config.yaml"
    if not config.exists():
        raise SystemExit(f"missing {config} (it is tracked in git)")

    print(f"Staging the model files from {model_dir}...")
    for filename in ("model.safetensors", "tokenizer.model"):
        shutil.copy(model_dir / filename, models_dir / filename)

    voices_dir = STAGING / "voices"
    src_dir = voices_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    print("Loading the staged model...")
    _host_module()._use_trained_gelu()
    staged = str(models_dir).replace("\\", "/")
    text = config.read_text(encoding="utf-8").replace(CONFIG_MODEL_DIR, staged)
    with tempfile.TemporaryDirectory() as tmp:
        local_config = Path(tmp) / "config.yaml"
        local_config.write_text(text, encoding="utf-8")
        model = TTSModel.load_model(config=str(local_config))
    if not model.has_voice_cloning:
        raise SystemExit("the staged model has no voice cloning; use the "
                         "weights from kyutai/pocket-tts")

    ini_lines = []
    for name, (hf_path, gender) in DEFAULT_VOICES.items():
        suffix = Path(hf_path).suffix.lower()
        wav = src_dir / (name + suffix)
        if not wav.exists():
            from huggingface_hub import hf_hub_download
            print(f"Downloading {name} from {hf_path} ...")
            shutil.copy(hf_hub_download(repo_id="kyutai/tts-voices",
                                        filename=hf_path), wav)
        print(f"Embedding {name} ...")
        state = model.get_state_for_audio_prompt(wav, truncate=True)
        export_model_state(state, voices_dir / f"{name}.safetensors")
        ini_lines += [
            f"[{name}]",
            f"file={name}.safetensors",
            f"gender={gender}",
            "language=409",
            "published=1",
            f"source=src/{wav.name}",
            "",
        ]

    with open(voices_dir / "voices.ini", "w", encoding="utf-16", newline="") as f:
        f.write("\r\n".join(ini_lines))

    # The shipped config keeps its C:/ProgramData/PocketTTS paths; the host
    # rewrites them at startup if the data directory differs on a machine.
    print("Done. Staged payload in", STAGING)


if __name__ == "__main__":
    sys.exit(main())
