"""The Pocket TTS AI models this wrapper knows, and how one gets installed.

Every model lives in its own folder under C:\\ProgramData\\PocketTTS\\models:

    models\\<id>\\model.safetensors     weights (with voice cloning)
    models\\<id>\\tokenizer.json        or tokenizer.model, as the model needs
    models\\<id>\\config.yaml           Kyutai's config for that model, with its
                                       file references pointed at the two above

The configs come from the pocket-tts package itself (pocket_tts/config/*.yaml),
so a model is set up exactly as Kyutai ships it, and the Hugging Face revision
each one pins is the revision that is downloaded. The folder "english" holds
the standard English model, as it did before there was more than one model.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# The path the configs shipped by the installer use; the host rewrites it when
# the data folder is somewhere else (see rewrite_data_dir).
CANONICAL_DATA_DIR = "C:/ProgramData/PocketTTS"

STANDARD_MODEL = "english"

# LCIDs, as hex strings like voices.ini keeps them.
LCID_ENGLISH = "409"


@dataclass(frozen=True)
class ModelInfo:
    id: str            # folder name and the voices.ini "model" value
    upstream: str      # the pocket-tts config name (pocket_tts/config/<upstream>.yaml)
    title: str         # what the Voice Manager shows
    description: str
    lcid: str          # language of the voices it makes, hex
    size_mb: int       # download size, roughly

    @property
    def english(self) -> bool:
        return self.lcid == LCID_ENGLISH


# In the order the Voice Manager lists them: recommended first.
CATALOG = (
    ModelInfo("english", "english_2026-10", "English, standard",
              "Kyutai's main English model of October 2026, 100 million "
              "parameters. The best quality; it needs a reasonably fast "
              "computer.", LCID_ENGLISH, 219),
    ModelInfo("english_18m_refl", "english_18m_refl_2026-10",
              "English, small (18 million parameters, reward-tuned)",
              "About five times smaller than the standard model and much "
              "faster, with quality close to it. Kyutai fine-tuned it further "
              "with rewards (REFL).", LCID_ENGLISH, 112),
    ModelInfo("english_7m_refl", "english_7m_refl_2026-10",
              "English, smaller (7 million parameters, reward-tuned)",
              "Very fast, for slow computers and for the quickest response "
              "while navigating. Kyutai fine-tuned it further with rewards "
              "(REFL).", LCID_ENGLISH, 67),
    ModelInfo("english_2m_refl", "english_2m_refl_2026-10",
              "English, tiny (2 million parameters, reward-tuned)",
              "The fastest model, for the slowest computers; speech is less "
              "natural. Kyutai fine-tuned it further with rewards (REFL).",
              LCID_ENGLISH, 47),
    ModelInfo("english_18m", "english_18m_2026-10",
              "English, small (18 million parameters)",
              "The small model before reward tuning.", LCID_ENGLISH, 112),
    ModelInfo("english_7m", "english_7m_2026-10",
              "English, smaller (7 million parameters)",
              "The smaller model before reward tuning.", LCID_ENGLISH, 67),
    ModelInfo("english_2m", "english_2m_2026-10",
              "English, tiny (2 million parameters)",
              "The tiny model before reward tuning.", LCID_ENGLISH, 47),
    ModelInfo("english_drifting", "english_drifting_26-09",
              "English, drifting sampler (September 2026)",
              "A standard-size English model whose sampler was trained a "
              "different way (drifting). An alternative voice character.",
              LCID_ENGLISH, 218),
    ModelInfo("french", "french", "French", "Kyutai's French model.",
              "40C", 219),
    ModelInfo("german", "german", "German", "Kyutai's German model.",
              "407", 219),
    ModelInfo("spanish", "spanish", "Spanish", "Kyutai's Spanish model.",
              "C0A", 219),
    ModelInfo("italian", "italian", "Italian", "Kyutai's Italian model.",
              "410", 219),
    ModelInfo("portuguese", "portuguese", "Portuguese",
              "Kyutai's Portuguese model.", "816", 219),
    ModelInfo("dutch", "dutch", "Dutch", "Kyutai's Dutch model.", "413",
              219),
    ModelInfo("french_24l", "french_24l", "French, large (24 layers)",
              "Higher quality than the French model, but slower.", "40C",
              440),
    ModelInfo("german_24l", "german_24l", "German, large (24 layers)",
              "Higher quality than the German model, but slower.", "407",
              440),
    ModelInfo("spanish_24l", "spanish_24l", "Spanish, large (24 layers)",
              "Higher quality than the Spanish model, but slower.", "C0A",
              440),
    ModelInfo("italian_24l", "italian_24l", "Italian, large (24 layers)",
              "Higher quality than the Italian model, but slower.", "410",
              440),
    ModelInfo("portuguese_24l", "portuguese_24l",
              "Portuguese, large (24 layers)",
              "Higher quality than the Portuguese model, but slower.", "816",
              440),
    ModelInfo("dutch_24l", "dutch_24l", "Dutch, large (24 layers)",
              "Higher quality than the Dutch model, but slower.", "413",
              440),
)

BY_ID = {m.id: m for m in CATALOG}


def get(model_id: str) -> ModelInfo:
    info = BY_ID.get(model_id or STANDARD_MODEL)
    if info is None:
        raise KeyError(f"unknown AI model: {model_id}")
    return info


def model_dir(models_dir: Path, model_id: str) -> Path:
    return models_dir / get(model_id).id


def weights_file(models_dir: Path, model_id: str) -> Path:
    return model_dir(models_dir, model_id) / "model.safetensors"


def config_file(models_dir: Path, model_id: str) -> Path:
    return model_dir(models_dir, model_id) / "config.yaml"


def is_installed(models_dir: Path, model_id: str) -> bool:
    return (weights_file(models_dir, model_id).exists()
            and config_file(models_dir, model_id).exists())


def installed(models_dir: Path) -> list:
    return [m for m in CATALOG if is_installed(models_dir, m.id)]


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------

def upstream_config(model_id: str) -> dict:
    """Kyutai's config for the model, as the pocket-tts package ships it."""
    import yaml
    from pocket_tts.utils.config import CONFIGS_DIR
    path = Path(CONFIGS_DIR) / f"{get(model_id).upstream}.yaml"
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def parse_hf(url: str):
    """hf://<owner>/<repo>/<path>[@revision] -> (repo_id, filename, revision)."""
    if not url.startswith("hf://"):
        raise ValueError(f"not a Hugging Face reference: {url}")
    body = url[len("hf://"):]
    revision = None
    if "@" in body:
        body, revision = body.rsplit("@", 1)
    owner, repo, filename = body.split("/", 2)
    return f"{owner}/{repo}", filename, revision


def tokenizer_name(config: dict) -> str:
    """The local file name of the model's tokenizer."""
    source = config["flow_lm"]["lookup_table"]["tokenizer_path"]
    return "tokenizer" + Path(parse_hf(source)[1] if source.startswith("hf://")
                              else source).suffix


def local_config(config: dict, folder: Path) -> dict:
    """`config` with every file reference pointed at `folder`."""
    import copy
    local = copy.deepcopy(config)
    folder_text = str(Path(folder).resolve()).replace("\\", "/")
    weights = f"{folder_text}/model.safetensors"
    local["weights_path"] = weights
    # This wrapper only installs the weights with voice cloning, so both
    # references point at them.
    local["weights_path_without_voice_cloning"] = weights
    table = local["flow_lm"]["lookup_table"]
    table["tokenizer_path"] = f"{folder_text}/{tokenizer_name(config)}"
    return local


def write_config(config: dict, path: Path, header: str = "") -> None:
    import yaml
    text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + text, encoding="utf-8")
    os.replace(tmp, path)


def config_header(model_id: str) -> str:
    info = get(model_id)
    return (f"# {info.title}: Kyutai's pocket_tts/config/{info.upstream}.yaml,\n"
            "# written by Pocket TTS SAPI5 with its files in this folder.\n\n")


def rewrite_data_dir(models_dir: Path, data_dir: Path) -> list:
    """Configs written for C:/ProgramData/PocketTTS point at the real data
    folder when it lives elsewhere (a relocated ProgramData, or the
    POCKETTTS_DATA_DIR override). Returns the configs it changed."""
    actual = str(data_dir).replace("\\", "/")
    if actual.lower() == CANONICAL_DATA_DIR.lower():
        return []
    changed = []
    for config in models_dir.glob("*/config.yaml"):
        try:
            text = config.read_text(encoding="utf-8")
        except OSError:
            continue
        if CANONICAL_DATA_DIR in text:
            config.write_text(text.replace(CANONICAL_DATA_DIR, actual),
                              encoding="utf-8")
            changed.append(config)
    return changed


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download(models_dir: Path, model_id: str, token, progress) -> Path:
    """Download the model's weights and tokenizer at the revisions Kyutai's
    config pins, and install them with a config that points at them.

    The new files are fetched first; the model's folder is only changed once
    all of them are on disk, and the files they replace are kept as *.old
    until the caller has loaded the new model (see finish_install and
    roll_back). Returns the model's folder."""
    from huggingface_hub import hf_hub_download

    info = get(model_id)
    config = upstream_config(model_id)
    folder = model_dir(models_dir, model_id)

    repo, filename, revision = parse_hf(config["weights_path"])
    progress(f"Downloading {info.title} (about {info.size_mb} MB) from "
             "Hugging Face...")
    weights = hf_hub_download(repo_id=repo, filename=filename,
                              revision=revision, token=token or None)

    tok_source = config["flow_lm"]["lookup_table"]["tokenizer_path"]
    progress("Downloading its tokenizer...")
    repo, filename, revision = parse_hf(tok_source)
    tokenizer = hf_hub_download(repo_id=repo, filename=filename,
                                revision=revision, token=token or None)

    progress("Installing the model files...")
    # Only now, so a download that fails leaves no empty folder behind.
    folder.mkdir(parents=True, exist_ok=True)
    staged = folder / "config.yaml.new"
    write_config(local_config(config, folder), staged, config_header(model_id))
    for src, name in ((weights, "model.safetensors"),
                      (tokenizer, tokenizer_name(config)),
                      (str(staged), "config.yaml")):
        dest = folder / name
        incoming = folder / (name + ".incoming")
        shutil.copy(src, incoming)
        if dest.exists():
            os.replace(dest, folder / (name + ".old"))
        os.replace(incoming, dest)
    staged.unlink(missing_ok=True)
    # A tokenizer of the other kind left from an earlier version of the model
    # would only confuse whoever looks in the folder.
    for other in ("tokenizer.model", "tokenizer.json"):
        if other != tokenizer_name(config) and (folder / other).exists():
            os.replace(folder / other, folder / (other + ".old"))
    return folder


def finish_install(folder: Path) -> None:
    for old in folder.glob("*.old"):
        old.unlink(missing_ok=True)


def roll_back(folder: Path) -> None:
    """Put back the files download() replaced, and drop the new ones that
    replaced nothing."""
    olds = {p.name[:-4] for p in folder.glob("*.old")}
    for name in ("model.safetensors", "config.yaml", "tokenizer.json",
                 "tokenizer.model"):
        if name in olds:
            os.replace(folder / (name + ".old"), folder / name)
        elif not olds:
            # Nothing was replaced: this was a fresh install, so it goes.
            (folder / name).unlink(missing_ok=True)


def remove(models_dir: Path, model_id: str) -> None:
    folder = model_dir(models_dir, model_id)
    if folder.exists():
        shutil.rmtree(folder)
