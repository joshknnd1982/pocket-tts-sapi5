# Pocket TTS SAPI5

A native Windows SAPI5 wrapper for **[Pocket TTS](https://github.com/kyutai-labs/pocket-tts)**, the neural text-to-speech engine by **[Kyutai](https://kyutai.org)** — with voice cloning built in. It makes Pocket TTS voices, including voices you clone yourself from a WAV or MP3 sample, available to every SAPI5 application on Windows: screen readers (NVDA, JAWS, Windows Narrator), reading tools (Balabolka, Bookworm), and anything else that speaks through SAPI5.

> **Note:** This is early-stage software under active development. Please report any problems you encounter by [opening an issue](../../issues).

**What's new:** see the [changelog](CHANGELOG.md). **Download:** the [Releases](../../releases) page.

## About Pocket TTS

Pocket TTS is a lightweight neural text-to-speech model created by [Kyutai](https://kyutai.org), a non-profit AI research lab in Paris. All credit for the speech engine and the AI models goes to Kyutai and the Pocket TTS authors: **Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour, and Alexandre Défossez**.

- 100-million-parameter model that runs entirely on your CPU — no cloud, no GPU, no API keys
- ~200 ms latency to the first audio chunk, several times faster than realtime on a modern CPU
- Small English models down to 1.9 million parameters, up to 25 times faster than realtime
- English, French, German, Spanish, Italian, Portuguese and Dutch
- Voice cloning from a short audio sample
- [Pocket TTS on GitHub](https://github.com/kyutai-labs/pocket-tts) (MIT license) · [Model card on Hugging Face](https://huggingface.co/kyutai/pocket-tts) (weights CC-BY-4.0) · [Tech report](https://kyutai.org/blog/2026-01-13-pocket-tts) · [Paper](https://arxiv.org/abs/2509.06926)

This project wraps that engine in a native SAPI5 interface; the wrapper itself is an independent community project and is not affiliated with or endorsed by Kyutai.

## Features

- **32-bit and 64-bit SAPI5 support** from a single always-warm engine process
- **Fast**: ~100 ms to first audio once warm; speech cancellation in ~20 ms (screen-reader friendly)
- **Voice cloning**: clone any voice from 10+ seconds of clean audio using the accessible Voice Manager
- **Every Pocket TTS model**: Kyutai's Pocket TTS 4.0 engine with all 20 of its models — the October 2026 English model, the small fast English models, and six other languages — and a choice of model for every voice (see [Choosing an AI model](#choosing-an-ai-model))
- **Fast voices for screen readers**: Alba, Jane, George and Michael come twice, as standard voices and as "Fast" voices on a 7-million-parameter model that answers in well under a tenth of a second
- **Share voices**: export any or all of your voices — the built-in ones and the ones you cloned — to a single `.pttsvoices` file, and import voices other people send you; voices made with a different AI model are rebuilt automatically
- **Numbers are spoken as numbers**: "224" is "two hundred twenty-four", not "24" — page numbers, times, prices, dates, phone numbers and the like are all written out in words before the AI model sees them (see [How numbers are spoken](#how-numbers-are-spoken))
- **Arrow through text letter by letter**: a single letter, digit or punctuation mark — what a screen reader sends as you arrow left and right — is spoken by its name, clearly and promptly, in upper or lower case (see [How letters and symbols are spoken](#how-letters-and-symbols-are-spoken))
- **Instant publish / unpublish / delete** of voices in the SAPI voice list — no reboots, no registry hacking
- **AI model downloads and updates** from Hugging Face inside the Voice Manager, with the voices of a model rebuilt automatically
- **Engine options**: int8 quantization (about 30% faster) and Kyutai's continuation mode for smoother long passages
- **Rate (0.33x–3x), pitch, and volume** control via [sonic](https://github.com/waywardgeek/sonic) time-stretching, applied instantly even mid-utterance
- **Fully accessible**: the Voice Manager uses only standard Win32 dialogs, every control in the tab order
- **No Python required** for end users — the installer bundles a self-contained runtime
- **Detailed logs** for debugging (see [Logs](#logs-and-troubleshooting))

## Download and install

**Download `PocketTTS_SAPI5_Setup.exe` from the [Releases](../../releases) page.**

The installer bundles everything: both SAPI5 engine DLLs, the Voice Manager, Kyutai's Pocket TTS 4.0 engine with two English models — the standard October 2026 model (`english_2026-10`) and the fast 7-million-parameter model (`english_7m_refl_2026-10`) — eight default voices (Alba, Jane, George and Michael, each also as a "Fast" voice), and a self-contained Python runtime. The other models are a download away in the Voice Manager. During setup you can choose to put the Voice Manager on your desktop and to start the speech engine automatically at sign-in (recommended — speech then starts instantly).

After installation the Pocket TTS voices appear in every SAPI5 application. In NVDA: NVDA menu → Preferences → Settings → Speech → synthesizer **SAPI5**, then pick a Pocket TTS voice.

Installing a new version over an old one keeps your cloned voices. When the engine starts with a different AI model than a voice was made with, or with a newer way of making voices (1.3.0 brought one), it rebuilds that voice from its audio sample, which takes a few seconds per voice.

> **Why is the installer required?** GitHub does not allow files over 100 MB in a repository, and the AI models (~220 MB and ~67 MB) and the bundled Python runtime (~600 MB) are far past that. Cloning this repository gives you all source code, the compiled wrapper binaries, and the default voice files — the model and runtime ship in the installer, or can be fetched with the scripts in `installer\` (see [Building from source](#building-from-source)).

## Cloning your own voices

Open the **Pocket TTS Voice Manager**, choose **Clone New Voice**, pick a WAV or MP3 with at least 10 seconds (up to 30 seconds is used) of clean, single-speaker speech, name the voice, choose the AI model to make it with (the standard English model unless you pick another), and clone. Test it with **Test Voice**, then **Publish to SAPI** to make it available to all applications. Unpublish or delete it at any time.

> **Important — consent:** Kyutai's use policy prohibits voice impersonation or cloning without the speaker's **explicit and lawful consent**. Only clone voices you have permission to clone. See the [prohibited-use section](https://github.com/kyutai-labs/pocket-tts#prohibited-use) of the Pocket TTS README.

## Choosing an AI model

Every voice is made with one of Kyutai's AI models, and speaks with it. The Voice Manager's list says which model each voice uses. The models:

| Model | Parameters | Download | Good for |
|---|---|---|---|
| English, standard (`english_2026-10`) | 100 million | 219 MB, installed | The best quality |
| English, small (`english_18m_refl_2026-10`) | 18 million | 112 MB | Close to standard, about twice as fast |
| English, smaller (`english_7m_refl_2026-10`) | 7 million | 67 MB, installed | Screen readers; the "Fast" voices use it |
| English, tiny (`english_2m_refl_2026-10`) | 2 million | 47 MB | The slowest computers |
| The same three without reward tuning (`english_18m_2026-10` and so on) | | | Kyutai's small models before REFL fine-tuning |
| English, drifting sampler (`english_drifting_26-09`) | 100 million | 218 MB | A different voice character |
| French, German, Spanish, Italian, Portuguese, Dutch | 100 million | 219 MB each | Those languages |
| The same six, large (24 layers) | | 440 MB each | Higher quality, slower |

Speed on the test machine (a desktop PC), reading one 8-second sentence with Alba: the standard model generated 3.4 seconds of speech per second of work and its first sound came after 0.18 seconds; the 18M model 6.4 times and 0.09 seconds; the 7M model 9.4 times and 0.07 seconds; the 2M model 12 times and 0.04 seconds. The Windows speech recognizer wrote down the sentence correctly from every one of them.

**To make a voice with another model**, select it in the Voice Manager and choose **Change AI Model**. Pick the model; leave **Keep this voice as it is, and make a copy** ticked to get a second voice (for example "Alba Fast" next to "Alba"), or untick it to move the voice. The voice is rebuilt from its audio sample, so a voice imported without its sample cannot be moved. **Clone New Voice** has the same model choice.

A voice made with a French, German, Spanish, Italian, Portuguese or Dutch model is listed in SAPI under that language and reads text in it. The number and letter rules below are English, so they apply to the English models only.

**Engine options**, in **AI Models**:

- **Faster engine** quantizes the models' transformer to 8-bit integers (Kyutai's `quantize` option): about 30% faster on the test machine, and the speech recognizer heard the same words. Saving the option reloads the models.
- **Smooth long passages** generates each piece of a long text as the continuation of the piece before (Kyutai's `teacher_force_previous_chunk`), instead of starting each one afresh from the voice sample, so a long passage keeps one flow. It costs a little speed.

Both are off to begin with. They are kept in `C:\ProgramData\PocketTTS\engine.json`.

## Sharing voices with other people

Voices live on your own PC, so the Voice Manager can pack them into one file you can send to somebody else — and unpack one they send you.

**To export.** Open the Voice Manager and choose **Export Voices**. Every voice is selected to begin with, so exporting all of them is just Export; use the list (or **Select All** / **Select None**) to send only some. Leave **Include the audio samples** ticked — it makes the file bigger, but it is what lets the other person's copy rebuild the voices if their AI model differs from yours. Choose where to save it and press **Export**. You get a single `.pttsvoices` file, ready to email or upload.

**To import.** Choose **Import Voices** and pick the `.pttsvoices` file — or just open the file itself, which starts the Voice Manager on the import screen. The Voice Manager reads it first and tells you what is inside: how many voices, who made it, and whether they were made with the same AI model you have. Pick the voices you want, decide what should happen if a name is already taken (import under a new name, skip it, or replace the voice you have), and press **Import**.

**About AI models.** A voice is a set of numbers derived from both the audio sample *and* the exact model weights it was made with, so a voice built on a different model will not sound right. The Voice Manager handles this for you: the package records the model each voice was made with, and an import puts each voice on that model when you have it, the same weights included. When they differ, it rebuilds the voice from its included audio sample. An English voice whose model you do not have is rebuilt for the standard English model; a voice in another language needs its language model, so the import names the model to download under **AI Models** first. If the sender left the audio samples out and your models differ, the voice is still imported but flagged as possibly not sounding correct — ask them to export again with the samples included, or download their model with **AI Models**. A package of standard English voices also opens in versions before 1.3.0.

**What is in the file.** A `.pttsvoices` file is an ordinary zip: `manifest.json`, one `voices/*.safetensors` embedding per voice, the audio samples under `src/` when included, and a `README.txt`. Every file is checksummed, and an import verifies the checksums, confirms each voice really is a Pocket TTS voice, and unpacks into a staging area first, so a damaged or hand-edited package cannot disturb the voices you already have.

> **Important — consent again:** only share and install voices whose speaker has agreed to it. Importing a voice does not make it yours to use however you like.

## Downloading and updating AI models (and getting a Hugging Face token)

The Voice Manager's **AI Models** button lists every Pocket TTS model, installed or not. Select one to read about it below the list; **Download or Update** downloads it from Hugging Face, or fetches Kyutai's current files for a model you have, and rebuilds the voices that use it; **Remove Model** deletes a model no voice uses (the standard English model always stays). Each model is downloaded at the exact revision Kyutai's Pocket TTS pins for it, together with Kyutai's own settings for it. The models with voice cloning are free, but Kyutai asks you to accept its terms first, so a download needs a Hugging Face access token. Here is exactly how to get one:

1. **Create a free Hugging Face account** at [huggingface.co/join](https://huggingface.co/join) (skip if you have one) and sign in.
2. **Accept the model terms**: open the model page at [huggingface.co/kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts). Near the top there is a form titled "You need to agree to share your contact information to access this model". Fill in the two fields (your organization or just "Personal", and what you want to use it for) and press **Agree and access repository**. The page will then say "You have been granted access to this model". This one acceptance covers every Pocket TTS model.
3. **Create a read token**: go to [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), choose **Create new token**, pick the **Read** token type, give it any name (for example "pocket-tts"), and press **Create token**. Copy the token — it starts with `hf_`.
4. **Download**: in the Voice Manager, press **AI Models**, select the model, paste the token into the "Hugging Face access token" field, and press **Download or Update**. Progress is announced in the dialog; when it finishes, the model is installed and the voices that use it have been rebuilt. If the download fails nothing is changed, and if the new files will not load, the previous ones are put back and the dialog says so.

The token stays on Hugging Face's side of the connection; this software only uses it for the download and does not store it.

## Training your own model

Kyutai has released the full training code, so you can train a brand-new Pocket TTS model (for example for a new language or a specialized domain) and use it with this wrapper:

1. Start with Kyutai's training guide: [`training/README.md`](https://github.com/kyutai-labs/pocket-tts/blob/main/training/README.md) in the Pocket TTS repository. Training requires a GPU machine and a dataset of transcribed speech; the guide covers data preparation (manifest files with forced alignment), the training configuration, and running the trainer.
2. Training produces a **config YAML** plus **model weights** (a `.safetensors` export, or a `.pt` checkpoint you can export). The Pocket TTS CLI can test them directly: `pocket-tts generate --config your_model.yaml`.
3. To use a custom model with this wrapper, replace the files of one model's folder, for example `C:\ProgramData\PocketTTS\models\english\` for the standard English model:
   - `model.safetensors` — your trained weights
   - `tokenizer.model` or `tokenizer.json` — the tokenizer used in training
   - `config.yaml` — your model config, with `weights_path`, `weights_path_without_voice_cloning` and `lookup_table.tokenizer_path` pointing at those files (see the installed `config.yaml` for the expected shape)

   Then restart the engine (sign out and back in, or end `PocketTTSHost.exe` in Task Manager — it restarts on demand). Voice embeddings are tied to the model weights, so the engine notices the new weights and rebuilds every voice of that model from its audio sample; a voice without one (imported without samples) keeps its old embedding and will not sound right until you clone it again. A **Download or Update** of that model in **AI Models** puts Kyutai's files back.
4. Community-trained models can also be published on Hugging Face and shared — see [Models trained by the community](https://github.com/kyutai-labs/pocket-tts#models-trained-by-the-community) for how Kyutai lists them.

## Building from source

Requirements: Windows 10+, Visual Studio 2022 (or Build Tools) with C++, CMake 3.15+, Python 3.10 x64, Inno Setup 6.

1. Clone [kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts) into `bin\pocket-tts` and check out the commit `installer\prepare_runtime.ps1` pins (`28770c06`, Pocket TTS 4.0.1 with Kyutai's fixes to 2026-11-06); the script does both itself when the folder is missing.
2. Create a Python 3.10 venv at `%USERPROFILE%\.pockettts\venv` and run `pip install "bin\pocket-tts[audio]"`.
3. Accept Kyutai's terms at [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts) (see the token guide above) and set `HF_TOKEN` to your read token: `installer\prepare_voices.py` downloads the models the installer ships (`english` and `english_7m_refl`) at the revisions Pocket TTS pins, writes their configs from Pocket TTS's own, and embeds the default voices with them. To use models you already have instead, pass their folder, laid out as `C:\ProgramData\PocketTTS\models` is: `prepare_voices.py C:\ProgramData\PocketTTS\models`.
4. Build:

```batch
build_all.bat
build_installer.bat
```

`build_all.bat` produces the SAPI DLLs and the Voice Manager in `output\`. `build_installer.bat` assembles the embedded Python runtime (`installer\prepare_runtime.ps1`), stages the models and embeds the default voices with them (`installer\prepare_voices.py`), and compiles `output\PocketTTS_SAPI5_Setup.exe`.

## Architecture

```
SAPI application (32- or 64-bit)
  └─ PocketTTSSAPI.dll        SAPI5 engine + dynamic voice enumerator (TokenEnums)
       └─ TCP 127.0.0.1       framed protocol, host launched on demand
            └─ PocketTTSHost.exe (embedded Python + pocket-tts 4.0, models kept warm in RAM)
                 ├─ C:\ProgramData\PocketTTS\models\<id>\      one folder per AI model:
                 │                                               weights, tokenizer, config.yaml
                 ├─ C:\ProgramData\PocketTTS\voices\           voices.ini + voice states
                 └─ C:\ProgramData\PocketTTS\engine.json        engine options
```

Voices live in `voices.ini`, each with the id of the model it was made with and the SHA-256 of that model's weights; the host loads a model when a voice first needs it and keeps up to three in memory (`host\pockettts_models.py` lists the models it knows). The engine's generation loop is a cancellable copy of Pocket TTS 4.0's, so a screen reader's interruption stops speech within a frame. The enumerator DLL reads `voices.ini` directly, so publishing or removing a voice takes effect immediately with no registry changes. Exporting and importing `.pttsvoices` packages happens in the host process too, since it is the component that owns the voice store and the model needed to rebuild an imported voice. Rate, pitch, and volume are applied on the client side with sonic, so the model always speaks at its natural pace and rate changes are instant.

### Speech on slower machines

Windows plays each piece of audio the moment the engine hands it over, so a PC that generates speech more slowly than it plays it will run the sound device dry part-way through a sentence and the speech breaks up. Three things keep that from happening:

- **One request per phrase.** Applications hand SAPI a sentence in several fragments — Narrator splits even "Pat, 7 of 88, selected," into three. Fragments that sound alike are merged into a single request, because each request costs a full model set-up. A bookmark, a silence, or any change of rate, pitch, volume or emphasis ends the merge, so applications that rely on those still get them in place.
- **A playback lead-in.** The host measures how much audio this machine generates per second of work and reports it with every reply. When it cannot keep ahead of playback, the engine banks a little audio before letting Windows start playing, so playback has a cushion to run on. Machines that are comfortably faster than realtime bank nothing and keep their ~200 ms latency. Set `POCKETTTS_LEAD_IN_MS` to fix the lead-in at a number of milliseconds, or to `0` to switch it off.
- **Full speed, whichever program started the engine.** Windows ranks a process that has no window of its own by the program that started it, and once that program closes it treats the process as background work, which it may run at a lower CPU speed or on the power-saving cores. The engine host tells Windows it is foreground work, so it keeps full speed after the program that started it has closed, and for every program that speaks through it afterwards.

A machine slower than about 0.5x realtime cannot read long passages without gaps whatever the engine does; `host.log` records the measured figure on every utterance.

### How numbers are spoken

The Pocket TTS model has no vocabulary entry for a number: it receives "224" as three unrelated tokens, "2", "2" and "4", and has to guess what to say. It guesses badly. It reads the digits one at a time ("224" comes out as a quick "two two four"), and a repeated or neighbouring digit is sometimes merged or dropped: "224" was heard as "24" (issue #2), and in testing "707" as "seventy seven" and "999" as "ninety nine". Words are something the model knows well, so the engine host writes every number out in words before the text reaches the model (`host\pockettts_text.py`; English models only, since the words are English):

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

A digit run with a leading zero, or seven or more digits with no separators, is treated as an identifier (a code, a phone or account number) and read digit by digit in groups; a four-digit number from 1100 to 2099 is read as a year. The rules have a test suite that needs nothing but Python: `python host\test_pockettts_text.py`.

### How letters and symbols are spoken

A screen reader moving through text with the arrow keys sends one character at a time: "s", "S", "7", ",". The model was never taught to say a letter on its own, and it guessed: "s" came out garbled or with extra sounds, "Sess" (issue #3), and a punctuation mark on its own came out as nothing. So when a request is a single character, the engine host gives the model that character's name instead, spelled the way the model says it clearly:

| Sent | Spoken |
|---|---|
| `s` · `S` | ess |
| `a` · `h` · `j` · `w` · `z` | eh · aitch · jay · double you · zee |
| `7` | seven |
| `,` · `?` · `(` · `~` | comma · question mark · left parenthesis · tilde |
| `é` · `ñ` · `α` | E acute · enn tilde · alpha |
| `cap S` | cap, ess |

The spellings were chosen by listening to the model, with a speech recognizer transcribing 24 takes of each candidate across the four default voices: "s" on its own was heard correctly 14 times out of 24 and "ess" 22, "j" 12 and "jay" 24, "i" 9 and "aye" 18. E and X are the two letters the model says best as they are.

Upper- and lower-case letters have the same name; the screen reader already marks a capital its own way, with a higher pitch, a beep, or the word "cap", and all three come through. When an application asks for text to be spelled out (SAPI's `<spell>` tag, which NVDA uses for single characters), every character is named the same way. A letter inside a word or a sentence is left alone, and so is any longer text, except for three names screen readers give punctuation that the model said as other words: "paren" (heard as "Perron"), "colon" ("Colin") and "caret" ("correct") are respelled so that they come out right wherever they appear.

The other half of the problem was where speech stopped. The model signals the end of speech, and the engine stops shortly after it. On a one-letter prompt that signal was often already on during the silence some voices open with — up to a second and a half for Jane and Michael — and the engine stopped part-way into the letter, or before it. The signal is now believed only once the audio has actually held speech, and the silence before the first word of a request is trimmed to 160 ms, so the letter is heard as soon as the model says it.

## Logs and troubleshooting

Everything logs, to make bug reports useful:

| Component | Log location |
|---|---|
| SAPI engine DLLs & Voice Manager | `%LOCALAPPDATA%\PocketTTS\logs\<application>-<x86/x64>.log` |
| Engine host process | `%LOCALAPPDATA%\PocketTTS\host.log` (set `POCKETTTS_LOG_LEVEL=DEBUG` for more) |
| Installer | `C:\ProgramData\PocketTTS\logs\setup.log` |

When reporting an issue, please include your Windows version, whether the application is 32- or 64-bit, and the relevant logs.

## Credits and licenses

- **Pocket TTS engine and models**: [Kyutai](https://kyutai.org) — Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour, Alexandre Défossez. Code MIT, model weights CC-BY-4.0. The model configs the engine writes are Kyutai's own, from the Pocket TTS package.
- **Default voice samples**: from [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices); see that repository for per-voice licenses.
- **Time-stretching**: [sonic](https://github.com/waywardgeek/sonic) by Bill Cox (Apache-2.0).
- **SAPI5 plumbing** (the COM class factory, registry helpers, voice tokens and token enumerator): adapted from [gozaltech/BstSpeech-sapi](https://github.com/gozaltech/BstSpeech-sapi); the files are listed in [NOTICE.md](NOTICE.md).
- **SAPI5 wrapper, engine host, and Voice Manager**: this repository.

The SAPI5 wrapper, engine host, and Voice Manager written for this project are licensed under the MIT License (see [LICENSE](LICENSE)); the third-party material credited above, including the SAPI5 plumbing files listed in [NOTICE.md](NOTICE.md), is not covered by that licence and stays under its owners' terms.
