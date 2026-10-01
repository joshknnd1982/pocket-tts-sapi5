# Pocket TTS SAPI5

A native Windows SAPI5 wrapper for **[Pocket TTS](https://github.com/kyutai-labs/pocket-tts)**, the neural text-to-speech engine by **[Kyutai](https://kyutai.org)** — with voice cloning built in. It makes Pocket TTS voices, including voices you clone yourself from a WAV or MP3 sample, available to every SAPI5 application on Windows: screen readers (NVDA, JAWS, Windows Narrator), reading tools (Balabolka, Bookworm), and anything else that speaks through SAPI5.

> **Note:** This is early-stage software under active development. Please report any problems you encounter by [opening an issue](../../issues).

## About Pocket TTS

Pocket TTS is a lightweight neural text-to-speech model created by [Kyutai](https://kyutai.org), a non-profit AI research lab in Paris. All credit for the speech engine and the AI models goes to Kyutai and the Pocket TTS authors: **Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour, and Alexandre Défossez**.

- 100-million-parameter model that runs entirely on your CPU — no cloud, no GPU, no API keys
- ~200 ms latency to the first audio chunk, several times faster than realtime on a modern CPU
- Voice cloning from a short audio sample
- [Pocket TTS on GitHub](https://github.com/kyutai-labs/pocket-tts) (MIT license) · [Model card on Hugging Face](https://huggingface.co/kyutai/pocket-tts) (weights CC-BY-4.0) · [Tech report](https://kyutai.org/blog/2026-01-13-pocket-tts) · [Paper](https://arxiv.org/abs/2509.06926)

This project wraps that engine in a native SAPI5 interface; the wrapper itself is an independent community project and is not affiliated with or endorsed by Kyutai.

## Features

- **32-bit and 64-bit SAPI5 support** from a single always-warm engine process
- **Fast**: ~100 ms to first audio once warm; speech cancellation in ~20 ms (screen-reader friendly)
- **Voice cloning**: clone any voice from 10+ seconds of clean audio using the accessible Voice Manager
- **Share voices**: export any or all of your voices — the built-in ones and the ones you cloned — to a single `.pttsvoices` file, and import voices other people send you; voices made with a different AI model are rebuilt automatically
- **Numbers are spoken as numbers**: "224" is "two hundred twenty-four", not "24" — page numbers, times, prices, dates, phone numbers and the like are all written out in words before the AI model sees them (see [How numbers are spoken](#how-numbers-are-spoken))
- **Instant publish / unpublish / delete** of voices in the SAPI voice list — no reboots, no registry hacking
- **AI model updates** from Hugging Face inside the Voice Manager, with all cloned voices rebuilt automatically
- **Rate (0.33x–3x), pitch, and volume** control via [sonic](https://github.com/waywardgeek/sonic) time-stretching, applied instantly even mid-utterance
- **Fully accessible**: the Voice Manager uses only standard Win32 dialogs, every control in the tab order
- **No Python required** for end users — the installer bundles a self-contained runtime
- **Detailed logs** for debugging (see [Logs](#logs-and-troubleshooting))

## Download and install

**Download `PocketTTS_SAPI5_Setup.exe` from the [Releases](../../releases) page.**

The installer bundles everything: both SAPI5 engine DLLs, the Voice Manager, Kyutai's September 2026 English Pocket TTS model (`english_2026-09`), four default voices (Alba, Jane, George, Michael), and a self-contained Python runtime. During setup you can choose to put the Voice Manager on your desktop and to start the speech engine automatically at sign-in (recommended — speech then starts instantly).

After installation the Pocket TTS voices appear in every SAPI5 application. In NVDA: NVDA menu → Preferences → Settings → Speech → synthesizer **SAPI5**, then pick a Pocket TTS voice.

Installing a new version over an old one keeps your cloned voices. When the engine starts with a different AI model than your voices were made with, it rebuilds every voice that has its audio sample for the new model, which takes a few seconds per voice.

> **Why is the installer required?** GitHub does not allow files over 100 MB in a repository, and the AI model (~220 MB) and the bundled Python runtime (~600 MB) are far past that. Cloning this repository gives you all source code, the compiled wrapper binaries, and the default voice files — the model and runtime ship in the installer, or can be fetched with the scripts in `installer\` (see [Building from source](#building-from-source)).

## Cloning your own voices

Open the **Pocket TTS Voice Manager**, choose **Clone New Voice**, pick a WAV or MP3 with at least 10 seconds (up to 30 seconds is used) of clean, single-speaker speech, name the voice, and clone. Test it with **Test Voice**, then **Publish to SAPI** to make it available to all applications. Unpublish or delete it at any time.

> **Important — consent:** Kyutai's use policy prohibits voice impersonation or cloning without the speaker's **explicit and lawful consent**. Only clone voices you have permission to clone. See the [prohibited-use section](https://github.com/kyutai-labs/pocket-tts#prohibited-use) of the Pocket TTS README.

## Sharing voices with other people

Voices live on your own PC, so the Voice Manager can pack them into one file you can send to somebody else — and unpack one they send you.

**To export.** Open the Voice Manager and choose **Export Voices**. Every voice is selected to begin with, so exporting all of them is just Export; use the list (or **Select All** / **Select None**) to send only some. Leave **Include the audio samples** ticked — it makes the file bigger, but it is what lets the other person's copy rebuild the voices if their AI model differs from yours. Choose where to save it and press **Export**. You get a single `.pttsvoices` file, ready to email or upload.

**To import.** Choose **Import Voices** and pick the `.pttsvoices` file — or just open the file itself, which starts the Voice Manager on the import screen. The Voice Manager reads it first and tells you what is inside: how many voices, who made it, and whether they were made with the same AI model you have. Pick the voices you want, decide what should happen if a name is already taken (import under a new name, skip it, or replace the voice you have), and press **Import**.

**About AI models.** A voice is a set of numbers derived from both the audio sample *and* the exact model weights it was made with, so a voice built on a different model will not sound right. The Voice Manager handles this for you: it compares the model the package was made with against yours and, when they differ, rebuilds each voice from its included audio sample. If the sender left the audio samples out and your models differ, the voice is still imported but flagged as possibly not sounding correct — ask them to export again with the samples included, or match their model with **Update AI Models**.

**What is in the file.** A `.pttsvoices` file is an ordinary zip: `manifest.json`, one `voices/*.safetensors` embedding per voice, the audio samples under `src/` when included, and a `README.txt`. Every file is checksummed, and an import verifies the checksums, confirms each voice really is a Pocket TTS voice, and unpacks into a staging area first, so a damaged or hand-edited package cannot disturb the voices you already have.

> **Important — consent again:** only share and install voices whose speaker has agreed to it. Importing a voice does not make it yours to use however you like.

## Updating the AI model (and getting a Hugging Face token)

The Voice Manager's **Update AI Models** button downloads the newest Pocket TTS English model from Hugging Face and automatically rebuilds all of your cloned voices for it. The full model with voice cloning is free, but Kyutai asks you to accept its terms first, so the update needs a Hugging Face access token. Here is exactly how to get one:

1. **Create a free Hugging Face account** at [huggingface.co/join](https://huggingface.co/join) (skip if you have one) and sign in.
2. **Accept the model terms**: open the model page at [huggingface.co/kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts). Near the top there is a form titled "You need to agree to share your contact information to access this model". Fill in the two fields (your organization or just "Personal", and what you want to use it for) and press **Agree and access repository**. The page will then say "You have been granted access to this model".
3. **Create a read token**: go to [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), choose **Create new token**, pick the **Read** token type, give it any name (for example "pocket-tts"), and press **Create token**. Copy the token — it starts with `hf_`.
4. **Run the update**: in the Voice Manager, press **Update AI Models**, paste the token into the "Hugging Face access token" field, and press **Update Now**. Progress is announced in the dialog; when it finishes, all your voices — cloned and default — have been rebuilt for the new model. If the download is damaged or the new model will not load, the previous model is put back and the dialog says so.

The token stays on Hugging Face's side of the connection; this software only uses it for the download and does not store it.

## Training your own model

Kyutai has released the full training code, so you can train a brand-new Pocket TTS model (for example for a new language or a specialized domain) and use it with this wrapper:

1. Start with Kyutai's training guide: [`training/README.md`](https://github.com/kyutai-labs/pocket-tts/blob/main/training/README.md) in the Pocket TTS repository. Training requires a GPU machine and a dataset of transcribed speech; the guide covers data preparation (manifest files with forced alignment), the training configuration, and running the trainer.
2. Training produces a **config YAML** plus **model weights** (a `.safetensors` export, or a `.pt` checkpoint you can export). The Pocket TTS CLI can test them directly: `pocket-tts generate --config your_model.yaml`.
3. To use a custom model with this wrapper, replace the files in `C:\ProgramData\PocketTTS\models\english\`:
   - `model.safetensors` — your trained weights
   - `tokenizer.model` — the sentencepiece tokenizer used in training
   - `config.yaml` — your model config, with `weights_path` and `lookup_table.tokenizer_path` pointing at those two files (see the shipped `config.yaml` for the expected shape)
   
   Then restart the engine (sign out and back in, or end `PocketTTSHost.exe` in Task Manager — it restarts on demand). Voice embeddings are tied to the model weights, so the engine notices the new weights and rebuilds every voice from its audio sample; a voice without one (imported without samples) keeps its old embedding and will not sound right until you clone it again.
4. Community-trained models can also be published on Hugging Face and shared — see [Models trained by the community](https://github.com/kyutai-labs/pocket-tts#models-trained-by-the-community) for how Kyutai lists them.

## Building from source

Requirements: Windows 10+, Visual Studio 2022 (or Build Tools) with C++, CMake 3.15+, Python 3.10 x64, Inno Setup 6.

1. Clone [kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts) into `bin\pocket-tts`.
2. Create a Python 3.10 venv at `%USERPROFILE%\.pockettts\venv` and run `pip install "bin\pocket-tts[audio]"`.
3. Put the English model's `model.safetensors` and `tokenizer.model` in `C:\ProgramData\PocketTTS\models\english\`, or in any folder you then pass to `installer\prepare_voices.py`. The weights are `languages/english/model.safetensors` in [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts) — accept the terms there first (see the token guide above); the tokenizer is `languages/english/tokenizer.model` in [kyutai/pocket-tts-without-voice-cloning](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning). The model's `config.yaml` is kept in this repository, in `installer\staging\models\english\`.
4. Build:

```batch
build_all.bat
build_installer.bat
```

`build_all.bat` produces the SAPI DLLs and the Voice Manager in `output\`. `build_installer.bat` assembles the embedded Python runtime (`installer\prepare_runtime.ps1`), stages the model and embeds the default voices with it (`installer\prepare_voices.py`), and compiles `output\PocketTTS_SAPI5_Setup.exe`.

## Architecture

```
SAPI application (32- or 64-bit)
  └─ PocketTTSSAPI.dll        SAPI5 engine + dynamic voice enumerator (TokenEnums)
       └─ TCP 127.0.0.1       framed protocol, host launched on demand
            └─ PocketTTSHost.exe (embedded Python + pocket-tts, model kept warm in RAM)
                 ├─ C:\ProgramData\PocketTTS\models\english\   model weights
                 └─ C:\ProgramData\PocketTTS\voices\           voices.ini + voice states
```

Voices live in `voices.ini`; the enumerator DLL reads it directly, so publishing or removing a voice takes effect immediately with no registry changes. Exporting and importing `.pttsvoices` packages happens in the host process too, since it is the component that owns the voice store and the model needed to rebuild an imported voice. Rate, pitch, and volume are applied on the client side with sonic, so the model always speaks at its natural pace and rate changes are instant.

### Speech on slower machines

Windows plays each piece of audio the moment the engine hands it over, so a PC that generates speech more slowly than it plays it will run the sound device dry part-way through a sentence and the speech breaks up. Three things keep that from happening:

- **One request per phrase.** Applications hand SAPI a sentence in several fragments — Narrator splits even "Pat, 7 of 88, selected," into three. Fragments that sound alike are merged into a single request, because each request costs a full model set-up. A bookmark, a silence, or any change of rate, pitch, volume or emphasis ends the merge, so applications that rely on those still get them in place.
- **A playback lead-in.** The host measures how much audio this machine generates per second of work and reports it with every reply. When it cannot keep ahead of playback, the engine banks a little audio before letting Windows start playing, so playback has a cushion to run on. Machines that are comfortably faster than realtime bank nothing and keep their ~200 ms latency. Set `POCKETTTS_LEAD_IN_MS` to fix the lead-in at a number of milliseconds, or to `0` to switch it off.
- **Full speed, whichever program started the engine.** Windows ranks a process that has no window of its own by the program that started it, and once that program closes it treats the process as background work, which it may run at a lower CPU speed or on the power-saving cores. The engine host tells Windows it is foreground work, so it keeps full speed after the program that started it has closed, and for every program that speaks through it afterwards.

A machine slower than about 0.5x realtime cannot read long passages without gaps whatever the engine does; `host.log` records the measured figure on every utterance.

### How numbers are spoken

The Pocket TTS model has no vocabulary entry for a number: it receives "224" as three unrelated tokens, "2", "2" and "4", and has to guess what to say. It guesses badly. It reads the digits one at a time ("224" comes out as a quick "two two four"), and a repeated or neighbouring digit is sometimes merged or dropped: "224" was heard as "24" (issue #2), and in testing "707" as "seventy seven" and "999" as "ninety nine". Words are something the model knows well, so the engine host writes every number out in words before the text reaches the model (`host\pockettts_text.py`):

| Text | Spoken |
|---|---|
| `224` | two hundred twenty-four |
| `1,250` · `3.14` · `-5` | one thousand two hundred fifty · three point one four · minus five |
| `21st` · `50%` | twenty-first · fifty percent |
| `$5.50` · `£1` | five dollars and fifty cents · one pound |
| `3:45 PM` · `10:00` · `1:23:45` | three forty-five P M · ten o'clock · one hour twenty-three minutes forty-five seconds |
| `1999` · `2024` · `the 1990s` | nineteen ninety-nine · twenty twenty-four · the nineteen nineties |
| `2026-09-30` | September thirtieth, twenty twenty-six |
| `10-20` | ten to twenty |
| `007` · `555-123-4567` · `1234567` | zero zero seven · five five five, one two three, four five six seven · one two three, four five six seven |
| `v2.0.1` · `B52` · `5kg` | v two point zero point one · B fifty-two · five kg |

A digit run with a leading zero, or seven or more digits with no separators, is treated as an identifier (a code, a phone or account number) and read digit by digit in groups; a four-digit number from 1100 to 2099 is read as a year. Everything that is not a number is passed to the model untouched. The rules have a test suite that needs nothing but Python: `python host\test_pockettts_text.py`.

## Logs and troubleshooting

Everything logs, to make bug reports useful:

| Component | Log location |
|---|---|
| SAPI engine DLLs & Voice Manager | `%LOCALAPPDATA%\PocketTTS\logs\<application>-<x86/x64>.log` |
| Engine host process | `%LOCALAPPDATA%\PocketTTS\host.log` (set `POCKETTTS_LOG_LEVEL=DEBUG` for more) |
| Installer | `C:\ProgramData\PocketTTS\logs\setup.log` |

When reporting an issue, please include your Windows version, whether the application is 32- or 64-bit, and the relevant logs.

## Credits and licenses

- **Pocket TTS engine and models**: [Kyutai](https://kyutai.org) — Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour, Alexandre Défossez. Code MIT, model weights CC-BY-4.0.
- **Default voice samples**: from [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices); see that repository for per-voice licenses.
- **Time-stretching**: [sonic](https://github.com/waywardgeek/sonic) by Bill Cox (Apache-2.0).
- **SAPI5 plumbing** (the COM class factory, registry helpers, voice tokens and token enumerator): adapted from [gozaltech/BstSpeech-sapi](https://github.com/gozaltech/BstSpeech-sapi); the files are listed in [NOTICE.md](NOTICE.md).
- **SAPI5 wrapper, engine host, and Voice Manager**: this repository.

The SAPI5 wrapper, engine host, and Voice Manager written for this project are licensed under the MIT License (see [LICENSE](LICENSE)); the third-party material credited above, including the SAPI5 plumbing files listed in [NOTICE.md](NOTICE.md), is not covered by that licence and stays under its owners' terms.
