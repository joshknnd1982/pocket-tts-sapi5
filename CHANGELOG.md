# Pocket TTS SAPI5 changelog

Everything a user can notice, newest version first. Each item says how to try it.

**Reporting a problem.** Open an issue at https://github.com/joshknnd1982/pocket-tts-sapi5/issues and attach the logs: `%LOCALAPPDATA%\PocketTTS\host.log` (the engine), `%LOCALAPPDATA%\PocketTTS\logs\` (the SAPI voices and the Voice Manager) and `C:\ProgramData\PocketTTS\logs\setup.log` (the installer). Say which Windows, which screen reader or program, and which voice and AI model.

**Your files.** Voices, AI models and engine options live in `C:\ProgramData\PocketTTS\`. Installing a new version keeps your voices.

## 1.3.0 (2026-11-07, release)

Kyutai's Pocket TTS 4.0 engine, its new AI models, and a choice of model for every voice.

### New: Kyutai's Pocket TTS 4.0 engine

The engine is now Kyutai's Pocket TTS 4.0.1, with their fixes up to 6 November 2026 (it was 3.0.2). It brings the October 2026 English model, the small models below, and Kyutai's improvements to cloning and to the start of each sentence: a short fade removes a faint click, and a voice sample is trimmed to end on a short pause, so the model no longer carries on the sample's last word at the start of a sentence.

- How to test: install over 1.2.2 and use any Pocket TTS voice. The first start rebuilds every voice that has its audio sample, a few seconds each; `%LOCALAPPDATA%\PocketTTS\host.log` lists them. Your cloned voices keep their names and stay published.
- Tell us: any voice that sounds different from before in a way you do not like.

### New: the October 2026 English model

The standard English model is now `english_2026-10`, which Kyutai retrained so that it reads numbers and years at the end of a sentence again. It is the model `languages/english` on Hugging Face now points to.

- How to test: Test Voice with Alba, "The train leaves at 9:45 and the year was 1999."

### New: fast voices for screen readers

Kyutai's small English models (18, 7 and 2 million parameters) generate speech 2 to 4 times faster than the standard model, and the first sound arrives in under a tenth of a second. The installer adds four fast voices on the 7 million parameter model: Alba Fast, Jane Fast, George Fast and Michael Fast. On the test machine the standard model ran at 3.4 times realtime with 0.18 seconds to the first sound, the 7 million model at 9.4 times with 0.07 seconds, and the 2 million model at 12 times with 0.04 seconds.

- How to test: in NVDA, NVDA menu, Preferences, Settings, Speech, synthesizer SAPI5, voice "Alba Fast". Arrow through text and read a long document.
- Tell us: whether the fast voices are clear enough for everyday use on your computer.

### New: AI Models in the Voice Manager

"Update AI Models" is now "AI Models". It lists all 20 Pocket TTS models: the standard English model, the small English models (each with and without Kyutai's reward tuning), the "drifting" English model, and French, German, Spanish, Italian, Portuguese and Dutch, each also in a larger 24-layer version. Each says whether it is installed, how many voices use it, and how big the download is. Download or Update fetches one (with your Hugging Face token), Remove Model deletes one no voice uses.

- How to test: Voice Manager, AI Models, arrow through the list; the box below describes the selected model. Pick German, paste your token, Download or Update. Then Remove Model.

### New: choose the AI model of a voice

Clone New Voice has an "AI model" list, so a new voice can be made with any installed model. The new "Change AI Model" button rebuilds a voice for another model from its audio sample, either in place or as a copy (the default), so you can have both "Alba" and a faster "Alba Fast". The voice list says which model each voice uses. A voice made with a French, German, Spanish, Italian, Portuguese or Dutch model is listed in SAPI under that language.

- How to test: select Jane, Change AI Model, pick "English, tiny (2 million parameters, reward-tuned)", Make Voice. A voice "Jane Tiny" appears; publish it and choose it in your screen reader.

### New: engine options

The AI Models dialog has two options, both off to begin with:

- Faster engine: int8 quantization of the models, about 30 percent faster on the test machine, with speech just as clear. Changing it reloads the models.
- Smooth long passages: each sentence of a long text is generated as the continuation of the one before (Kyutai's new continuation mode), instead of starting afresh from the voice sample.
- How to test: AI Models, tick an option, Save Engine Options, then read a long document.
- Tell us: whether either option should be on by default.

### Changed: sharing voices

A `.pttsvoices` file now records each voice's AI model. On import a voice goes onto the same model when it is installed, is rebuilt for the standard English model when it is English and its model is missing, and is refused with the model's name when it needs a language model you have not downloaded. A package of standard English voices still opens in 1.2.x.

- How to test: Export Voices with a fast voice included, then Import Voices on the same or another computer.

### Other changes

- The installer's Start menu has "Pocket TTS What's New", which opens this file.
- The English number and letter rules (1.2.1 and 1.2.2) apply to the English models only; text for the other languages goes to the model as it is.

## 1.2.2 (2026-10-12, release)

Letters, digits and punctuation are spoken clearly when you arrow through text (issue #3).

## 1.2.1 (2026-09-30, release)

Numbers are spoken as numbers: "224" is "two hundred twenty-four", not "24" (issue #2).

## 1.2.0 (2026-09-26, release)

Kyutai's September 2026 English model, and model updates that work.

## 1.1.2 (2026-09-15, release)

No more stuttering after reopening a program: the engine keeps full speed.

## 1.1.1 (2026-09-07, release)

No more broken-up speech on slower PCs.

## 1.1.0 (2026-09-03, release)

Share voices between computers with `.pttsvoices` files.

## 1.0.0 (2026-09-01, release)

First release.
