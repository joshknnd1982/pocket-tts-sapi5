#include <new>
#include <string>
#include <cmath>
#include <algorithm>
#include <vector>
#include <sperror.h>
#include "utils.hpp"
#include "ISpTTSEngineImpl.hpp"
#include "host_client.h"
#include "audio_post.h"
#include "debug_log.h"

namespace PocketTts {
namespace sapi {

namespace {

constexpr int MIN_RATE = -10;
constexpr int MAX_RATE = 10;

HostClient* g_hostClient = nullptr;

// SAPI rate -10..10 -> 0.33x..3x playback speed via sonic.
float rate_to_speed(int rate)
{
    rate = std::clamp(rate, MIN_RATE, MAX_RATE);
    return std::pow(3.0f, static_cast<float>(rate) / 10.0f);
}

// SAPI pitch -10..10 -> one octave down..up.
float pitch_to_multiplier(int pitch)
{
    pitch = std::clamp(pitch, -10, 10);
    return std::pow(2.0f, static_cast<float>(pitch) / 12.0f);
}

// Playback lead-in. The site starts playing the moment it is handed audio,
// so on a machine that generates speech slower than it plays, the sound
// device runs dry over and over and the speech breaks up. Banking a little
// audio first gives playback a cushion to run on. Machines that generate
// comfortably faster than realtime need none, and get none: the first
// audio still reaches the site within a couple of hundred milliseconds.
constexpr float LEAD_IN_MS_PER_CHAR = 60.0f;   // this model's speaking rate
constexpr float LEAD_IN_FAST_ENOUGH = 1.25f;   // no lead-in above this
constexpr float LEAD_IN_MAX_AUDIO_MS = 1000.0f;
constexpr float LEAD_IN_MAX_WALL_MS = 1200.0f;  // cap on the delay before speech
constexpr float LEAD_IN_MIN_MS = 100.0f;        // below this it is not worth it

// Merging stops here so one request stays interruptible and bounded.
constexpr size_t MAX_MERGED_CHARS = 8000;

struct SpeakContext {
    ISpTTSEngineSite* caller = nullptr;
    AudioPost post;
    ULONGLONG bytes_written = 0;
    bool aborted = false;
    int frag_rate_adj = 0;      // fragment RateAdj, combined with live GetRate
    float frag_volume = 1.0f;   // fragment volume scale 0..1
    float speed = 1.0f;
    float pitch = 1.0f;
    std::vector<int16_t> processed;
    std::vector<int16_t> bank;  // audio held back until playback starts
    size_t lead_in_samples = 0;
    bool playing = true;
};

bool site_write(SpeakContext* ctx, const int16_t* samples, size_t count)
{
    if (count == 0) {
        return true;
    }
    // The site consumes the whole buffer on success; pcbWritten is not
    // reliable and looping on it truncates speech.
    ULONG written = 0;
    const ULONG bytes = static_cast<ULONG>(count * sizeof(int16_t));
    HRESULT hr = ctx->caller->Write(samples, bytes, &written);
    if (FAILED(hr)) {
        DEBUG_LOG("write_pcm: Write failed 0x%08X", hr);
        return false;
    }
    return true;
}

bool write_pcm(SpeakContext* ctx, const int16_t* samples, size_t count)
{
    if (count == 0) {
        return true;
    }
    ctx->bytes_written += count * sizeof(int16_t);
    if (ctx->playing) {
        return site_write(ctx, samples, count);
    }
    ctx->bank.insert(ctx->bank.end(), samples, samples + count);
    if (ctx->bank.size() < ctx->lead_in_samples) {
        return true;
    }
    ctx->playing = true;
    const bool ok = site_write(ctx, ctx->bank.data(), ctx->bank.size());
    ctx->bank.clear();
    ctx->bank.shrink_to_fit();
    return ok;
}

// An utterance can end before the lead-in target is reached; hand over
// whatever is banked.
bool flush_bank(SpeakContext* ctx)
{
    const bool banked = !ctx->playing && !ctx->bank.empty();
    ctx->playing = true;
    if (!banked) {
        return true;
    }
    const bool ok = site_write(ctx, ctx->bank.data(), ctx->bank.size());
    ctx->bank.clear();
    ctx->bank.shrink_to_fit();
    return ok;
}

// POCKETTTS_LEAD_IN_MS overrides the automatic choice: 0 disables the
// lead-in, a positive value fixes it at that many milliseconds.
long lead_in_override()
{
    wchar_t buf[16];
    const DWORD n = GetEnvironmentVariableW(L"POCKETTTS_LEAD_IN_MS", buf, 16);
    if (n == 0 || n >= 16) {
        return -1;
    }
    return wcstol(buf, nullptr, 10);
}

// Playback consumes `speed` seconds of model audio per second while the host
// produces `factor` of them, so an utterance runs a deficit that has to be
// banked before it starts. Capped, because waiting is its own annoyance.
size_t lead_in_samples_for(size_t chars, float speed, float factor)
{
    float ms = 0.0f;
    const long override_ms = lead_in_override();
    if (override_ms >= 0) {
        ms = static_cast<float>(override_ms);
    } else if (factor > 0.0f && speed > 0.0f && chars > 0) {
        const float effective = factor / speed;
        if (effective >= LEAD_IN_FAST_ENOUGH) {
            return 0;
        }
        const float playback_ms = chars * LEAD_IN_MS_PER_CHAR / speed;
        const float needed = playback_ms * (1.0f / effective - 1.0f);
        const float cap = (std::min)(LEAD_IN_MAX_AUDIO_MS,
                                     LEAD_IN_MAX_WALL_MS * effective);
        ms = std::clamp(needed, 0.0f, cap);
        if (ms < LEAD_IN_MIN_MS) {
            return 0;
        }
    }
    return static_cast<size_t>(ms * POCKETTTS_SAMPLE_RATE / 1000.0f);
}

// Consecutive fragments that sound alike are spoken in one request. Every
// request costs a full model set-up, and Narrator alone splits a short
// announcement like "Pat, 7 of 88, selected," into three fragments: sent
// separately they arrive as three utterances with a pause between each.
bool same_prosody(const SPVTEXTFRAG* a, const SPVTEXTFRAG* b)
{
    return a->State.eAction == b->State.eAction
        && a->State.LangID == b->State.LangID
        && a->State.EmphAdj == b->State.EmphAdj
        && a->State.RateAdj == b->State.RateAdj
        && a->State.Volume == b->State.Volume
        && a->State.PitchAdj.MiddleAdj == b->State.PitchAdj.MiddleAdj
        && a->State.PitchAdj.RangeAdj == b->State.PitchAdj.RangeAdj
        && a->State.pPhoneIds == nullptr && b->State.pPhoneIds == nullptr;
}

bool speak_callback(const char* data, uint32_t size, void* user)
{
    auto* ctx = static_cast<SpeakContext*>(user);
    if (!ctx || !ctx->caller) {
        return false;
    }

    const DWORD actions = ctx->caller->GetActions();
    if (actions & SPVES_ABORT) {
        ctx->aborted = true;
        return false;
    }
    if (actions & SPVES_SKIP) {
        ctx->caller->CompleteSkip(0);
        ctx->aborted = true;
        return false;
    }
    if (actions & SPVES_RATE) {
        long rate = 0;
        if (SUCCEEDED(ctx->caller->GetRate(&rate))) {
            ctx->speed = rate_to_speed(static_cast<int>(rate) + ctx->frag_rate_adj);
            ctx->post.setSpeed(ctx->speed);
        }
    }
    if (actions & SPVES_VOLUME) {
        unsigned short volume = 100;
        if (SUCCEEDED(ctx->caller->GetVolume(&volume))) {
            ctx->post.configure(ctx->speed, ctx->pitch,
                                (volume / 100.0f) * ctx->frag_volume);
        }
    }

    ctx->processed.clear();
    ctx->post.process(reinterpret_cast<const int16_t*>(data),
                      size / sizeof(int16_t), ctx->processed);
    return write_pcm(ctx, ctx->processed.data(), ctx->processed.size());
}

}  // namespace

void InitHostClient()
{
    if (!g_hostClient) {
        g_hostClient = new (std::nothrow) HostClient();
    }
}

void CleanupHostClient()
{
    delete g_hostClient;
    g_hostClient = nullptr;
}

void ShutdownHost()
{
    if (g_hostClient) {
        g_hostClient->shutdownServer();
    }
}

ISpTTSEngineImpl::ISpTTSEngineImpl() = default;
ISpTTSEngineImpl::~ISpTTSEngineImpl() = default;

STDMETHODIMP ISpTTSEngineImpl::SetObjectToken(ISpObjectToken* pToken)
{
    DEBUG_LOG("=== SetObjectToken ===");
    if (!pToken) {
        return E_INVALIDARG;
    }

    try {
        ISpDataKeyPtr attr;
        if (FAILED(pToken->OpenKey(L"Attributes", &attr))) {
            return E_INVALIDARG;
        }

        utils::out_ptr<wchar_t> name(CoTaskMemFree);
        if (FAILED(attr->GetStringValue(L"Name", name.address()))) {
            return E_INVALIDARG;
        }

        voice_name_utf8_ = utils::wstring_to_string(name.get());
        token_ = pToken;
        DEBUG_LOG("SetObjectToken: voice = %s", voice_name_utf8_.c_str());
        return S_OK;
    }
    catch (const std::bad_alloc&) {
        return E_OUTOFMEMORY;
    }
    catch (...) {
        return E_UNEXPECTED;
    }
}

STDMETHODIMP ISpTTSEngineImpl::GetObjectToken(ISpObjectToken** ppToken)
{
    if (!ppToken) {
        return E_POINTER;
    }
    *ppToken = nullptr;

    if (token_) {
        token_.AddRef();
        *ppToken = token_.GetInterfacePtr();
        return S_OK;
    }
    return E_UNEXPECTED;
}

STDMETHODIMP ISpTTSEngineImpl::GetOutputFormat(
    const GUID* /*pTargetFmtId*/,
    const WAVEFORMATEX* /*pTargetWaveFormatEx*/,
    GUID* pOutputFormatId,
    WAVEFORMATEX** ppCoMemOutputWaveFormatEx)
{
    if (!pOutputFormatId || !ppCoMemOutputWaveFormatEx) {
        return E_POINTER;
    }

    *pOutputFormatId = SPDFID_WaveFormatEx;
    *ppCoMemOutputWaveFormatEx = nullptr;

    auto* pwfex = static_cast<WAVEFORMATEX*>(CoTaskMemAlloc(sizeof(WAVEFORMATEX)));
    if (!pwfex) {
        return E_OUTOFMEMORY;
    }

    pwfex->wFormatTag = WAVE_FORMAT_PCM;
    pwfex->nChannels = POCKETTTS_CHANNELS;
    pwfex->nSamplesPerSec = POCKETTTS_SAMPLE_RATE;
    pwfex->wBitsPerSample = POCKETTTS_BITS;
    pwfex->nBlockAlign = pwfex->nChannels * pwfex->wBitsPerSample / 8;
    pwfex->nAvgBytesPerSec = pwfex->nSamplesPerSec * pwfex->nBlockAlign;
    pwfex->cbSize = 0;

    *ppCoMemOutputWaveFormatEx = pwfex;
    return S_OK;
}

STDMETHODIMP ISpTTSEngineImpl::Speak(
    DWORD dwSpeakFlags,
    REFGUID /*rguidFormatId*/,
    const WAVEFORMATEX* /*pWaveFormatEx*/,
    const SPVTEXTFRAG* pTextFragList,
    ISpTTSEngineSite* pOutputSite)
{
    DEBUG_LOG("=== Speak (flags 0x%08X) ===", dwSpeakFlags);

    if (!pTextFragList || !pOutputSite) {
        return E_INVALIDARG;
    }
    if (!g_hostClient) {
        return E_FAIL;
    }
    if (voice_name_utf8_.empty()) {
        return SPERR_UNINITIALIZED;
    }

    try {
        long sapi_rate = 0;
        pOutputSite->GetRate(&sapi_rate);

        unsigned short sapi_volume = 100;
        pOutputSite->GetVolume(&sapi_volume);

        ULONGLONG event_interest = 0;
        pOutputSite->GetEventInterest(&event_interest);
        const bool send_sentence_events = (event_interest & (1ULL << SPEI_SENTENCE_BOUNDARY)) != 0;
        const bool send_word_events = (event_interest & (1ULL << SPEI_WORD_BOUNDARY)) != 0;

        SpeakContext ctx;
        ctx.caller = pOutputSite;

        size_t speakable_chars = 0;
        for (const SPVTEXTFRAG* f = pTextFragList; f; f = f->pNext) {
            if (f->State.eAction == SPVA_Speak || f->State.eAction == SPVA_SpellOut) {
                speakable_chars += f->ulTextLen;
            }
        }
        const float host_speed = g_hostClient->speedFactor();
        ctx.lead_in_samples = lead_in_samples_for(
            speakable_chars, rate_to_speed(static_cast<int>(sapi_rate)), host_speed);
        ctx.playing = (ctx.lead_in_samples == 0);
        if (!ctx.playing) {
            DEBUG_LOG("lead-in: banking %u ms of audio (host x%.2f realtime)",
                      static_cast<unsigned>(ctx.lead_in_samples * 1000 /
                                            POCKETTTS_SAMPLE_RATE),
                      host_speed);
        }

        for (const SPVTEXTFRAG* frag = pTextFragList; frag; ) {
            const DWORD actions = pOutputSite->GetActions();
            if (actions & SPVES_ABORT) {
                break;
            }
            if (actions & SPVES_SKIP) {
                pOutputSite->CompleteSkip(0);
                break;
            }
            if (actions & SPVES_RATE) {
                pOutputSite->GetRate(&sapi_rate);
            }
            if (actions & SPVES_VOLUME) {
                pOutputSite->GetVolume(&sapi_volume);
            }

            // Only fragments that are actually speech may reach the model;
            // bookmarks and silence would otherwise be read aloud.
            if (frag->State.eAction == SPVA_Bookmark) {
                if (frag->ulTextLen > 0 && frag->pTextStart) {
                    std::wstring bookmark_text(frag->pTextStart, frag->ulTextLen);
                    long bookmark_id = 0;
                    try {
                        bookmark_id = std::stol(bookmark_text);
                    } catch (...) {
                    }
                    SPEVENT event = {};
                    event.eEventId = SPEI_TTS_BOOKMARK;
                    event.elParamType = SPET_LPARAM_IS_STRING;
                    event.ullAudioStreamOffset = ctx.bytes_written;
                    event.lParam = reinterpret_cast<LPARAM>(bookmark_text.c_str());
                    event.wParam = bookmark_id;
                    pOutputSite->AddEvents(&event, 1);
                } else {
                    SPEVENT event = {};
                    event.eEventId = SPEI_TTS_BOOKMARK;
                    event.elParamType = SPET_LPARAM_IS_UNDEFINED;
                    event.ullAudioStreamOffset = ctx.bytes_written;
                    pOutputSite->AddEvents(&event, 1);
                }
                frag = frag->pNext;
                continue;
            }

            if (frag->State.eAction == SPVA_Silence) {
                const ULONG ms = frag->State.SilenceMSecs;
                if (ms > 0 && ms <= 60000) {
                    std::vector<int16_t> silence(
                        static_cast<size_t>(POCKETTTS_SAMPLE_RATE) * ms / 1000, 0);
                    if (!write_pcm(&ctx, silence.data(), silence.size())) {
                        break;
                    }
                }
                frag = frag->pNext;
                continue;
            }

            if (frag->State.eAction != SPVA_Speak && frag->State.eAction != SPVA_SpellOut) {
                frag = frag->pNext;
                continue;
            }
            if (frag->ulTextLen == 0 || !frag->pTextStart) {
                frag = frag->pNext;
                continue;
            }

            // Gather the run of fragments this one request will speak. A
            // bookmark, a silence or any change of prosody ends the run, so
            // an application that relies on those still gets them in place.
            std::vector<const SPVTEXTFRAG*> run{frag};
            const SPVTEXTFRAG* after_run = frag->pNext;
            std::wstring wide_text(frag->pTextStart, frag->ulTextLen);
            if (frag->State.eAction == SPVA_Speak) {
                while (after_run && after_run->pTextStart && after_run->ulTextLen > 0
                       && same_prosody(frag, after_run)
                       && wide_text.size() + after_run->ulTextLen <= MAX_MERGED_CHARS) {
                    // SAPI usually leaves the separating whitespace in the
                    // fragments; supply it where it did not.
                    if (!wide_text.empty() && !iswspace(wide_text.back())
                        && !iswspace(after_run->pTextStart[0])) {
                        wide_text.push_back(L' ');
                    }
                    wide_text.append(after_run->pTextStart, after_run->ulTextLen);
                    run.push_back(after_run);
                    after_run = after_run->pNext;
                }
            }

            const bool spell = frag->State.eAction == SPVA_SpellOut;
            if (spell) {
                // The host names each character. The spacing is for hosts
                // before 1.2.2, which do not know the spell flag.
                std::wstring spelled;
                spelled.reserve(wide_text.size() * 2);
                for (wchar_t c : wide_text) {
                    if (!iswspace(c)) {
                        spelled.push_back(c);
                        spelled.push_back(L' ');
                    }
                }
                wide_text = spelled;
            }

            const std::string text = utils::wstring_to_string(wide_text);
            if (text.empty() ||
                text.find_first_not_of(" \t\r\n") == std::string::npos) {
                frag = after_run;
                continue;  // pocket-tts rejects empty prompts
            }

            for (const SPVTEXTFRAG* part : run) {
                if (send_sentence_events) {
                    SPEVENT event = {};
                    event.eEventId = SPEI_SENTENCE_BOUNDARY;
                    event.elParamType = SPET_LPARAM_IS_UNDEFINED;
                    event.ullAudioStreamOffset = ctx.bytes_written;
                    event.lParam = part->ulTextSrcOffset;
                    event.wParam = part->ulTextLen;
                    pOutputSite->AddEvents(&event, 1);
                }

                if (send_word_events) {
                    const wchar_t* text_start = part->pTextStart;
                    const ULONG text_len = part->ulTextLen;
                    bool in_word = false;
                    ULONG word_start = 0;
                    for (ULONG i = 0; i <= text_len; ++i) {
                        const bool is_word_char = (i < text_len) &&
                            (iswalnum(text_start[i]) || text_start[i] == L'\'' || text_start[i] == L'-');
                        if (is_word_char && !in_word) {
                            word_start = i;
                            in_word = true;
                        } else if (!is_word_char && in_word) {
                            SPEVENT event = {};
                            event.eEventId = SPEI_WORD_BOUNDARY;
                            event.elParamType = SPET_LPARAM_IS_UNDEFINED;
                            event.ullAudioStreamOffset = ctx.bytes_written;
                            event.lParam = part->ulTextSrcOffset + word_start;
                            event.wParam = i - word_start;
                            pOutputSite->AddEvents(&event, 1);
                            in_word = false;
                        }
                    }
                }
            }

            const int combined_rate = std::clamp(
                static_cast<int>(sapi_rate + frag->State.RateAdj), MIN_RATE, MAX_RATE);
            ctx.frag_rate_adj = frag->State.RateAdj;
            ctx.frag_volume = std::clamp(frag->State.Volume / 100.0f, 0.0f, 1.0f);
            ctx.pitch = pitch_to_multiplier(frag->State.PitchAdj.MiddleAdj);
            ctx.speed = rate_to_speed(combined_rate);

            const float volume = (sapi_volume / 100.0f) * ctx.frag_volume;
            ctx.post.configure(ctx.speed, ctx.pitch, volume);

            DEBUG_LOG("fragment: %u parts, speed %.2f pitch %.2f volume %.2f "
                      "text \"%s\"", static_cast<unsigned>(run.size()),
                      ctx.speed, ctx.pitch, volume, text.c_str());

            g_hostClient->speak(voice_name_utf8_, text, speak_callback, &ctx,
                                spell);

            if (ctx.aborted) {
                break;
            }
            frag = after_run;
        }

        if (!ctx.aborted) {
            ctx.processed.clear();
            ctx.post.finish(ctx.processed);
            write_pcm(&ctx, ctx.processed.data(), ctx.processed.size());
            flush_bank(&ctx);
        }

        DEBUG_LOG("=== Speak done (%llu bytes) ===", ctx.bytes_written);
        return S_OK;
    }
    catch (const std::bad_alloc&) {
        return E_OUTOFMEMORY;
    }
    catch (...) {
        return E_UNEXPECTED;
    }
}

}
}
