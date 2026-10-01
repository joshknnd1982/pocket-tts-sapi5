# Notices

The code written for this project is licensed under the MIT License (see [LICENSE](LICENSE)). The material
below is not covered by that licence and stays under its own terms.

## Pocket TTS engine and models (Kyutai)

The speech engine and the AI models this project wraps are by [Kyutai](https://kyutai.org): Manu Orsini, Simon
Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour and Alexandre Défossez. All credit for the speech
engine and the AI models goes to Kyutai and the Pocket TTS authors. This project is an independent community
project and is not affiliated with or endorsed by Kyutai. The terms, as the README states them:

- **Code**: MIT license ([Pocket TTS on GitHub](https://github.com/kyutai-labs/pocket-tts)).
- **Model weights**: CC-BY-4.0 ([model card on Hugging Face](https://huggingface.co/kyutai/pocket-tts)). Attribution:
  Kyutai and the Pocket TTS authors named above. The weights and the tokenizer are not stored in this repository;
  the installer ships them.
- **Use policy**: Kyutai's use policy prohibits voice impersonation or cloning without the speaker's explicit and
  lawful consent (see the [prohibited-use section](https://github.com/kyutai-labs/pocket-tts#prohibited-use) of the
  Pocket TTS README).

### Files here that are Kyutai's, or follow Kyutai's code

- `installer/staging/models/english/config.yaml` says in its header that it is adapted from
  `pocket_tts/config/english_2026-09.yaml` in Kyutai's pocket-tts 3.3.0.
- `host/pockettts_host.py` says in its comments that three parts of it follow Kyutai's pocket-tts code:
  `MIN_FRAMES_BEFORE_EOS` (the guard added in pocket-tts 3.2.0), `_use_trained_gelu` (the feed-forward change made
  in pocket-tts 3.1.0) and `Engine._stream_one` (a cancellable re-implementation of pocket-tts
  `_generate_audio_stream_short_text`, pinned to pocket-tts 3.0.2). The rest of that file was written for this
  project.

The configuration file and those three parts are not covered by this project's MIT License; they stay under
Kyutai's terms for its code (MIT, as stated above). Kyutai's permission notice, as published in the `LICENSE` file
of [kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts), reads:

    Permission is hereby granted, free of charge, to any
    person obtaining a copy of this software and associated
    documentation files (the "Software"), to deal in the
    Software without restriction, including without
    limitation the rights to use, copy, modify, merge,
    publish, distribute, sublicense, and/or sell copies of
    the Software, and to permit persons to whom the Software
    is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice
    shall be included in all copies or substantial portions
    of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
    ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
    TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
    PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
    SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
    OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR
    IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
    DEALINGS IN THE SOFTWARE.

## Default voice samples (Kyutai)

The default voices (Alba, Jane, George and Michael) are from [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices);
see that repository for per-voice licenses. This repository holds the four audio samples
(`installer/staging/voices/src/*.wav`) and the voice embeddings made from them with Kyutai's model weights
(`installer/staging/voices/*.safetensors`). None of them are covered by this project's MIT License; each stays
under the terms of its source.

## Time-stretching (sonic)

[sonic](https://github.com/waywardgeek/sonic) by Bill Cox (Apache-2.0). It is fetched when the engine is built
(`CMakeLists.txt`) and compiled into the SAPI engine DLLs and the Voice Manager, including the prebuilt copies in
`output/`. It is not covered by this project's MIT License.

## Software bundled by the installer

The installer (`installer/pockettts.iss`) also bundles a self-contained Python runtime, the pocket-tts package and
the packages it depends on (`installer/prepare_runtime.ps1`: pocket-tts, torch CPU and soundfile). That software is
not stored in this repository, is not covered by this project's MIT License, and stays under its own licences.
