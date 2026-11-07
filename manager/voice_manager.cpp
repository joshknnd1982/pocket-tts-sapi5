// Pocket TTS Voice Manager
//
// Accessible Win32 dialog application for cloning, testing, publishing and
// removing Pocket TTS SAPI5 voices, for moving a voice to another AI model,
// and for downloading, updating and removing the AI models. All
// functionality is reachable with the keyboard; only standard dialog
// controls are used so screen readers announce everything correctly.

#include <winsock2.h>
#include <windows.h>
#include <commctrl.h>
#include <commdlg.h>
#include <mmsystem.h>
#include <shellapi.h>
#include <shlobj.h>

#include <atomic>
#include <string>
#include <vector>

#include "../src/host_client.h"
#include "../src/utils.hpp"
#include "../src/debug_log.h"
#include "resource.h"

#pragma comment(lib, "comctl32.lib")
#pragma comment(lib, "comdlg32.lib")
#pragma comment(lib, "winmm.lib")

namespace {

constexpr UINT WM_APP_STATUS = WM_APP + 1;   // lparam: std::wstring* (owned)
constexpr UINT WM_APP_DONE = WM_APP + 2;     // wparam: 1 success, lparam: std::wstring* error (owned, may be null)
constexpr UINT WM_APP_VOICES = WM_APP + 3;   // lparam: std::vector<HostVoice>* (owned)
constexpr UINT WM_APP_PACKAGE = WM_APP + 4;  // lparam: PackageInfo* (owned)
constexpr UINT WM_APP_MODELS = WM_APP + 5;   // lparam: ModelsSnapshot* (owned)

HINSTANCE g_instance = nullptr;
HostClient g_client;
std::vector<HostVoice> g_voices;
std::vector<HostModel> g_models;      // every model the engine knows
std::wstring g_selectedVoice;
bool g_busy = false;
std::vector<PackageVoice> g_package;  // contents of the package being imported
std::vector<std::wstring> g_exportNames;  // voices shown in the export list
std::wstring g_inspectedPath;         // package the import list belongs to
HWND g_inspectFocus = nullptr;        // control focused when a read began
std::wstring g_importPath;            // package named on the command line
bool g_inspecting = false;            // a package is being read right now

void post_status(HWND dlg, const std::wstring& text)
{
    PostMessageW(dlg, WM_APP_STATUS, 0, reinterpret_cast<LPARAM>(new std::wstring(text)));
}

void set_text(HWND dlg, int id, const std::wstring& text)
{
    SetDlgItemTextW(dlg, id, text.c_str());
}

void append_log(HWND dlg, int id, const std::wstring& line)
{
    HWND edit = GetDlgItem(dlg, id);
    const int len = GetWindowTextLengthW(edit);
    SendMessageW(edit, EM_SETSEL, len, len);
    std::wstring with_break = line + L"\r\n";
    SendMessageW(edit, EM_REPLACESEL, FALSE, reinterpret_cast<LPARAM>(with_break.c_str()));
}

std::wstring get_text(HWND dlg, int id)
{
    HWND ctrl = GetDlgItem(dlg, id);
    const int len = GetWindowTextLengthW(ctrl);
    std::wstring text(static_cast<size_t>(len) + 1, L'\0');
    GetWindowTextW(ctrl, text.data(), len + 1);
    text.resize(static_cast<size_t>(len));
    return text;
}

// The standard English model; voices from engines before 1.3.0 report none.
const wchar_t kStandardModel[] = L"english";

std::wstring model_title(const std::wstring& id)
{
    const std::wstring wanted = id.empty() ? kStandardModel : id;
    for (const auto& m : g_models) {
        if (m.id == wanted) {
            return m.title;
        }
    }
    return wanted == kStandardModel ? L"English, standard" : wanted;
}

// A short word for a model, used to name a copy of a voice made with it.
std::wstring model_short_name(const std::wstring& id)
{
    if (id == kStandardModel) return L"Standard";
    if (id.rfind(L"english_18m", 0) == 0) return L"Small";
    if (id.rfind(L"english_7m", 0) == 0) return L"Fast";
    if (id.rfind(L"english_2m", 0) == 0) return L"Tiny";
    if (id.rfind(L"english_drifting", 0) == 0) return L"Drifting";
    std::wstring title = model_title(id);
    const size_t comma = title.find(L',');
    return comma == std::wstring::npos ? title : title.substr(0, comma);
}

// Fills a drop-down list with the installed models and returns their ids in
// list order; `select` is chosen if it is among them, else the first.
std::vector<std::wstring> fill_model_combo(HWND combo, const std::wstring& select,
                                           const std::wstring& skip = std::wstring())
{
    std::vector<std::wstring> ids;
    SendMessageW(combo, CB_RESETCONTENT, 0, 0);
    for (const auto& m : g_models) {
        if (!m.installed || m.id == skip) {
            continue;
        }
        ids.push_back(m.id);
        SendMessageW(combo, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(m.title.c_str()));
    }
    if (ids.empty() && skip != kStandardModel) {
        // An engine from before 1.3.0 lists no models: it has the standard one.
        ids.push_back(kStandardModel);
        const std::wstring title = model_title(kStandardModel);
        SendMessageW(combo, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(title.c_str()));
    }
    int index = 0;
    for (size_t i = 0; i < ids.size(); ++i) {
        if (ids[i] == select) {
            index = static_cast<int>(i);
        }
    }
    SendMessageW(combo, CB_SETCURSEL, index, 0);
    return ids;
}

std::wstring combo_model(HWND dlg, int id, const std::vector<std::wstring>& ids)
{
    const LRESULT index = SendDlgItemMessageW(dlg, id, CB_GETCURSEL, 0, 0);
    if (index == CB_ERR || static_cast<size_t>(index) >= ids.size()) {
        return {};
    }
    return ids[static_cast<size_t>(index)];
}

void progress_to_dialog(const std::wstring& message, void* user)
{
    post_status(static_cast<HWND>(user), message);
}

// ---------------------------------------------------------------------------
// Streaming waveOut player for voice tests
// ---------------------------------------------------------------------------

class Player {
public:
    Player() { InitializeCriticalSection(&cs_); }
    ~Player() { close(); DeleteCriticalSection(&cs_); }

    bool open()
    {
        close();
        WAVEFORMATEX fmt = {};
        fmt.wFormatTag = WAVE_FORMAT_PCM;
        fmt.nChannels = POCKETTTS_CHANNELS;
        fmt.nSamplesPerSec = POCKETTTS_SAMPLE_RATE;
        fmt.wBitsPerSample = POCKETTTS_BITS;
        fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample / 8;
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign;
        return waveOutOpen(&handle_, WAVE_MAPPER, &fmt, 0, 0, CALLBACK_NULL) == MMSYSERR_NOERROR;
    }

    void feed(const char* data, uint32_t size)
    {
        if (!handle_ || size == 0) {
            return;
        }
        EnterCriticalSection(&cs_);
        reap_done();
        auto* hdr = new WAVEHDR{};
        hdr->lpData = new char[size];
        memcpy(hdr->lpData, data, size);
        hdr->dwBufferLength = size;
        if (waveOutPrepareHeader(handle_, hdr, sizeof(WAVEHDR)) == MMSYSERR_NOERROR &&
            waveOutWrite(handle_, hdr, sizeof(WAVEHDR)) == MMSYSERR_NOERROR) {
            headers_.push_back(hdr);
        } else {
            delete[] hdr->lpData;
            delete hdr;
        }
        LeaveCriticalSection(&cs_);
    }

    bool playing()
    {
        EnterCriticalSection(&cs_);
        reap_done();
        const bool active = !headers_.empty();
        LeaveCriticalSection(&cs_);
        return active;
    }

    void stop()
    {
        EnterCriticalSection(&cs_);
        if (handle_) {
            waveOutReset(handle_);
            reap_done();
        }
        LeaveCriticalSection(&cs_);
    }

    void close()
    {
        EnterCriticalSection(&cs_);
        if (handle_) {
            waveOutReset(handle_);
            reap_done();
            waveOutClose(handle_);
            handle_ = nullptr;
        }
        LeaveCriticalSection(&cs_);
    }

private:
    void reap_done()
    {
        for (size_t i = 0; i < headers_.size();) {
            WAVEHDR* hdr = headers_[i];
            if (hdr->dwFlags & WHDR_DONE) {
                waveOutUnprepareHeader(handle_, hdr, sizeof(WAVEHDR));
                delete[] hdr->lpData;
                delete hdr;
                headers_.erase(headers_.begin() + i);
            } else {
                ++i;
            }
        }
    }

    HWAVEOUT handle_ = nullptr;
    std::vector<WAVEHDR*> headers_;
    CRITICAL_SECTION cs_;
};

// ---------------------------------------------------------------------------
// Main dialog: voice list
// ---------------------------------------------------------------------------

struct VoiceRefresh {
    std::vector<HostVoice> voices;
    std::vector<HostModel> models;
};

void refresh_voices_async(HWND dlg)
{
    post_status(dlg, L"Connecting to the Pocket TTS engine (the first start can take a minute)...");
    CreateThread(nullptr, 0, [](LPVOID param) -> DWORD {
        HWND dlg = static_cast<HWND>(param);
        auto* result = new VoiceRefresh();
        std::wstring error;
        if (!g_client.listVoices(result->voices, error)) {
            DEBUG_LOG("manager: listVoices failed: %S", error.c_str());
            delete result;
            result = nullptr;
            post_status(dlg, L"Error: " + error);
        } else {
            DEBUG_LOG("manager: listVoices returned %u voices",
                      (unsigned)result->voices.size());
            std::wstring modelError;
            if (!g_client.listModels(result->models, modelError)) {
                DEBUG_LOG("manager: listModels failed: %S", modelError.c_str());
            }
        }
        PostMessageW(dlg, WM_APP_VOICES, 0, reinterpret_cast<LPARAM>(result));
        return 0;
    }, dlg, 0, nullptr);
}

void fill_voice_list(HWND dlg)
{
    HWND list = GetDlgItem(dlg, IDC_VOICE_LIST);
    SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const auto& v : g_voices) {
        std::wstring item = v.name;
        item += v.female ? L" (female, " : L" (male, ";
        item += v.published ? L"published, " : L"not published, ";
        item += model_title(v.model) + L")";
        SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(item.c_str()));
    }
    if (!g_voices.empty()) {
        int select = 0;
        for (size_t i = 0; i < g_voices.size(); ++i) {
            if (g_voices[i].name == g_selectedVoice) {
                select = static_cast<int>(i);
                break;
            }
        }
        SendMessageW(list, LB_SETCURSEL, select, 0);
    }
}

const HostVoice* selected_voice(HWND dlg)
{
    const LRESULT index = SendDlgItemMessageW(dlg, IDC_VOICE_LIST, LB_GETCURSEL, 0, 0);
    if (index == LB_ERR || static_cast<size_t>(index) >= g_voices.size()) {
        return nullptr;
    }
    return &g_voices[static_cast<size_t>(index)];
}

// ---------------------------------------------------------------------------
// Clone dialog
// ---------------------------------------------------------------------------

struct CloneParams {
    HWND dlg;
    std::wstring name;
    std::wstring file;
    bool female;
    std::wstring model;
};

std::vector<std::wstring> g_cloneModels;  // ids behind the Clone dialog's model list

INT_PTR CALLBACK CloneDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool cloning = false;

    switch (msg) {
    case WM_INITDIALOG: {
        cloning = false;
        HWND gender = GetDlgItem(dlg, IDC_GENDER);
        SendMessageW(gender, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Male"));
        SendMessageW(gender, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Female"));
        SendMessageW(gender, CB_SETCURSEL, 0, 0);
        g_cloneModels = fill_model_combo(GetDlgItem(dlg, IDC_CLONE_MODEL), kStandardModel);
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        append_log(dlg, IDC_LOG, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_DONE: {
        auto* error = reinterpret_cast<std::wstring*>(lparam);
        cloning = false;
        EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
        if (wparam) {
            MessageBoxW(dlg,
                        L"The voice was cloned. It is not published yet: select it in the "
                        L"list and choose Publish to SAPI to make it available to other "
                        L"applications, or Test Voice to hear it first.",
                        L"Voice cloned", MB_OK | MB_ICONINFORMATION);
            EndDialog(dlg, IDOK);
        } else {
            MessageBoxW(dlg, error ? error->c_str() : L"Cloning failed.",
                        L"Cloning failed", MB_OK | MB_ICONERROR);
        }
        delete error;
        return TRUE;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_BROWSE: {
            wchar_t file[MAX_PATH] = L"";
            OPENFILENAMEW ofn = {sizeof(ofn)};
            ofn.hwndOwner = dlg;
            ofn.lpstrFilter = L"Audio files (*.wav;*.mp3;*.flac;*.ogg)\0*.wav;*.mp3;*.flac;*.ogg\0All files\0*.*\0";
            ofn.lpstrFile = file;
            ofn.nMaxFile = MAX_PATH;
            ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
            ofn.lpstrTitle = L"Choose an audio sample of the voice";
            if (GetOpenFileNameW(&ofn)) {
                set_text(dlg, IDC_FILE, file);
            }
            return TRUE;
        }
        case IDOK: {
            if (cloning) {
                return TRUE;
            }
            auto* params = new CloneParams();
            params->dlg = dlg;
            params->name = get_text(dlg, IDC_NAME);
            params->file = get_text(dlg, IDC_FILE);
            params->female = SendDlgItemMessageW(dlg, IDC_GENDER, CB_GETCURSEL, 0, 0) == 1;
            params->model = combo_model(dlg, IDC_CLONE_MODEL, g_cloneModels);
            if (params->name.empty() || params->file.empty()) {
                MessageBoxW(dlg, L"Please enter a voice name and choose an audio file.",
                            L"Missing information", MB_OK | MB_ICONWARNING);
                delete params;
                return TRUE;
            }
            cloning = true;
            EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
            append_log(dlg, IDC_LOG, L"Starting the clone...");
            CreateThread(nullptr, 0, [](LPVOID p) -> DWORD {
                auto* params = static_cast<CloneParams*>(p);
                DEBUG_LOG("manager: cloning \"%S\" from \"%S\" (%s, model %S)",
                          params->name.c_str(), params->file.c_str(),
                          params->female ? "female" : "male", params->model.c_str());
                std::wstring error;
                const bool ok = g_client.cloneVoice(
                    params->name, params->file, params->female, L"409",
                    progress_to_dialog, params->dlg, error, params->model);
                DEBUG_LOG("manager: clone %s%S%s", ok ? "succeeded" : "failed: ",
                          ok ? L"" : error.c_str(), "");
                PostMessageW(params->dlg, WM_APP_DONE, ok ? 1 : 0,
                             reinterpret_cast<LPARAM>(ok ? nullptr : new std::wstring(error)));
                delete params;
                return 0;
            }, params, 0, nullptr);
            return TRUE;
        }
        case IDCANCEL:
            if (!cloning) {
                EndDialog(dlg, IDCANCEL);
            }
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// ---------------------------------------------------------------------------
// Test dialog
// ---------------------------------------------------------------------------

struct TestContext {
    HWND dlg = nullptr;
    Player player;
    std::atomic<bool> stop{false};
    std::atomic<bool> speaking{false};
    std::wstring voice;
};

TestContext* g_test = nullptr;

bool test_audio_callback(const char* pcm, uint32_t size, void* user)
{
    auto* ctx = static_cast<TestContext*>(user);
    if (ctx->stop.load()) {
        return false;
    }
    ctx->player.feed(pcm, size);
    return true;
}

INT_PTR CALLBACK TestDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool closePending = false;

    switch (msg) {
    case WM_INITDIALOG: {
        closePending = false;
        g_test->dlg = dlg;
        std::wstring caption = L"Test Voice: " + g_test->voice;
        SetWindowTextW(dlg, caption.c_str());
        std::wstring sample = L"Hello! This is " + g_test->voice +
            L" speaking through Kyutai Pocket TTS. I run completely on this computer.";
        set_text(dlg, IDC_TEXT, sample);
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        set_text(dlg, IDC_STATUS, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_DONE:
        g_test->speaking.store(false);
        if (closePending) {
            // The user closed the dialog mid-speech; the worker has now
            // finished with the context, so it is safe to tear down.
            g_test->player.close();
            EndDialog(dlg, IDCANCEL);
            return TRUE;
        }
        EnableWindow(GetDlgItem(dlg, IDC_SPEAK), TRUE);
        set_text(dlg, IDC_STATUS, wparam ? L"Finished." : L"Speech failed; is the engine running?");
        return TRUE;
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_SPEAK: {
            if (g_test->speaking.load()) {
                return TRUE;
            }
            const std::wstring text = get_text(dlg, IDC_TEXT);
            if (text.empty()) {
                return TRUE;
            }
            g_test->stop.store(false);
            g_test->speaking.store(true);
            EnableWindow(GetDlgItem(dlg, IDC_SPEAK), FALSE);
            set_text(dlg, IDC_STATUS, L"Generating speech...");
            auto* textCopy = new std::wstring(text);
            CreateThread(nullptr, 0, [](LPVOID p) -> DWORD {
                auto* text = static_cast<std::wstring*>(p);
                TestContext* ctx = g_test;
                bool ok = false;
                if (ctx->player.open()) {
                    ok = g_client.speak(
                        PocketTts::utils::wstring_to_string(ctx->voice),
                        PocketTts::utils::wstring_to_string(*text),
                        test_audio_callback, ctx);
                    // Let the queued audio finish before closing the device.
                    while (!ctx->stop.load() && ctx->player.playing()) {
                        Sleep(100);
                    }
                    ctx->player.close();
                }
                PostMessageW(ctx->dlg, WM_APP_DONE, ok ? 1 : 0, 0);
                delete text;
                return 0;
            }, textCopy, 0, nullptr);
            return TRUE;
        }
        case IDC_STOPBTN:
            g_test->stop.store(true);
            g_test->player.stop();
            set_text(dlg, IDC_STATUS, L"Stopped.");
            return TRUE;
        case IDCANCEL:
            g_test->stop.store(true);
            if (g_test->speaking.load()) {
                // Let the worker thread finish with the context first;
                // WM_APP_DONE completes the close.
                closePending = true;
                g_test->player.stop();
                set_text(dlg, IDC_STATUS, L"Stopping...");
                return TRUE;
            }
            g_test->player.close();
            EndDialog(dlg, IDCANCEL);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// CreateThread, with the handle closed straight away: nothing joins these
// workers, they report back by posting to the dialog.
bool start_worker(LPTHREAD_START_ROUTINE body, void* param)
{
    HANDLE thread = CreateThread(nullptr, 0, body, param, 0, nullptr);
    if (!thread) {
        DEBUG_LOG("manager: CreateThread failed, error %lu", GetLastError());
        return false;
    }
    CloseHandle(thread);
    return true;
}

void post_done(HWND dlg, bool ok, const std::wstring& message)
{
    PostMessageW(dlg, WM_APP_DONE, ok ? 1 : 0,
                 reinterpret_cast<LPARAM>(new std::wstring(message)));
}

// ---------------------------------------------------------------------------
// AI models dialog: download, update and remove models; engine options
// ---------------------------------------------------------------------------

struct ModelsSnapshot {
    std::vector<HostModel> models;
    bool quantize = false;
    bool continuation = false;
    bool settingsKnown = false;
    std::wstring error;
};

struct ModelOp {
    HWND dlg;
    int op;  // 0 download or update, 1 remove, 2 save engine options
    std::wstring model;
    std::wstring token;
    bool quantize;
    bool continuation;
};

void fetch_models_async(HWND dlg)
{
    start_worker([](LPVOID p) -> DWORD {
        HWND dlg = static_cast<HWND>(p);
        auto* snap = new ModelsSnapshot();
        if (g_client.listModels(snap->models, snap->error)) {
            std::wstring ignored;
            snap->settingsKnown =
                g_client.getSettings(snap->quantize, snap->continuation, ignored);
        }
        PostMessageW(dlg, WM_APP_MODELS, 0, reinterpret_cast<LPARAM>(snap));
        return 0;
    }, dlg);
}

std::wstring model_list_item(const HostModel& m)
{
    std::wstring item = m.title + L": ";
    if (m.installed) {
        item += L"installed";
        if (m.voices == 1) {
            item += L", used by 1 voice";
        } else if (m.voices > 1) {
            item += L", used by " + std::to_wstring(m.voices) + L" voices";
        }
        if (!m.loadError.empty()) {
            item += L", failed to load";
        }
    } else {
        item += L"not installed, about " + std::to_wstring(m.sizeMb) + L" MB to download";
    }
    return item;
}

void show_model_description(HWND dlg)
{
    const LRESULT index = SendDlgItemMessageW(dlg, IDC_MODEL_LIST, LB_GETCURSEL, 0, 0);
    if (index == LB_ERR || static_cast<size_t>(index) >= g_models.size()) {
        set_text(dlg, IDC_MODEL_DESC, L"");
        return;
    }
    const HostModel& m = g_models[static_cast<size_t>(index)];
    std::wstring text = m.description;
    text += m.installed
        ? L" Installed. Download or Update fetches Kyutai's current files for it again."
        : L" Not installed: Download or Update fetches it (about " +
              std::to_wstring(m.sizeMb) + L" MB).";
    if (!m.loadError.empty()) {
        text += L" It failed to load: " + m.loadError;
    }
    set_text(dlg, IDC_MODEL_DESC, text);
}

INT_PTR CALLBACK ModelsDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool busy = false;
    static std::wstring keepSelected;

    auto set_busy = [dlg](bool value) {
        busy = value;
        for (int id : {IDOK, IDC_MODEL_REMOVE, IDC_OPT_SAVE}) {
            EnableWindow(GetDlgItem(dlg, id), value ? FALSE : TRUE);
        }
    };

    switch (msg) {
    case WM_INITDIALOG:
        keepSelected = kStandardModel;
        set_busy(true);
        append_log(dlg, IDC_LOG, L"Asking the engine which AI models it has...");
        fetch_models_async(dlg);
        return TRUE;
    case WM_APP_MODELS: {
        auto* snap = reinterpret_cast<ModelsSnapshot*>(lparam);
        set_busy(false);
        if (!snap->error.empty()) {
            append_log(dlg, IDC_LOG, L"Error: " + snap->error);
        } else {
            g_models = snap->models;
            HWND list = GetDlgItem(dlg, IDC_MODEL_LIST);
            SendMessageW(list, LB_RESETCONTENT, 0, 0);
            int select = 0;
            for (size_t i = 0; i < g_models.size(); ++i) {
                const std::wstring item = model_list_item(g_models[i]);
                SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(item.c_str()));
                if (g_models[i].id == keepSelected) {
                    select = static_cast<int>(i);
                }
            }
            SendMessageW(list, LB_SETCURSEL, select, 0);
            show_model_description(dlg);
            if (snap->settingsKnown) {
                CheckDlgButton(dlg, IDC_OPT_QUANTIZE, snap->quantize ? BST_CHECKED : BST_UNCHECKED);
                CheckDlgButton(dlg, IDC_OPT_CONTINUATION,
                               snap->continuation ? BST_CHECKED : BST_UNCHECKED);
            }
            size_t installed = 0;
            for (const auto& m : g_models) {
                installed += m.installed ? 1 : 0;
            }
            wchar_t line[128];
            swprintf_s(line, L"%zu AI models are available, %zu of them installed.",
                       g_models.size(), installed);
            append_log(dlg, IDC_LOG, line);
        }
        delete snap;
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        append_log(dlg, IDC_LOG, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_DONE: {
        auto* message = reinterpret_cast<std::wstring*>(lparam);
        const std::wstring text = message ? *message : std::wstring();
        delete message;
        append_log(dlg, IDC_LOG, text);
        MessageBoxW(dlg, text.c_str(), wparam ? L"AI Models" : L"AI Models: it did not work",
                    MB_OK | (wparam ? MB_ICONINFORMATION : MB_ICONERROR));
        // The list says what is installed and used now.
        fetch_models_async(dlg);
        return TRUE;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_MODEL_LIST:
            if (HIWORD(wparam) == LBN_SELCHANGE) {
                show_model_description(dlg);
            }
            return TRUE;
        case IDOK:
        case IDC_MODEL_REMOVE:
        case IDC_OPT_SAVE: {
            if (busy) {
                return TRUE;
            }
            auto* op = new ModelOp();
            op->dlg = dlg;
            op->op = LOWORD(wparam) == IDOK ? 0 : (LOWORD(wparam) == IDC_MODEL_REMOVE ? 1 : 2);
            op->quantize = IsDlgButtonChecked(dlg, IDC_OPT_QUANTIZE) == BST_CHECKED;
            op->continuation = IsDlgButtonChecked(dlg, IDC_OPT_CONTINUATION) == BST_CHECKED;
            op->token = get_text(dlg, IDC_TOKEN);
            if (op->op != 2) {
                const LRESULT index =
                    SendDlgItemMessageW(dlg, IDC_MODEL_LIST, LB_GETCURSEL, 0, 0);
                if (index == LB_ERR || static_cast<size_t>(index) >= g_models.size()) {
                    MessageBoxW(dlg, L"Please select an AI model in the list first.",
                                L"No model selected", MB_OK | MB_ICONWARNING);
                    delete op;
                    return TRUE;
                }
                const HostModel& m = g_models[static_cast<size_t>(index)];
                op->model = m.id;
                keepSelected = m.id;
                if (op->op == 1) {
                    if (!m.installed) {
                        MessageBoxW(dlg, L"That model is not installed.", L"Remove Model",
                                    MB_OK | MB_ICONINFORMATION);
                        delete op;
                        return TRUE;
                    }
                    const std::wstring prompt = L"Remove " + m.title +
                        L" from this computer? You can download it again later.";
                    if (MessageBoxW(dlg, prompt.c_str(), L"Remove Model",
                                    MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) != IDYES) {
                        delete op;
                        return TRUE;
                    }
                }
            }
            set_busy(true);
            append_log(dlg, IDC_LOG, op->op == 0 ? L"Starting the download..."
                                     : op->op == 1 ? L"Removing the model..."
                                                   : L"Saving the engine options...");
            if (!start_worker([](LPVOID p) -> DWORD {
                auto* op = static_cast<ModelOp*>(p);
                std::wstring summary;
                std::wstring error;
                bool ok = false;
                DEBUG_LOG("manager: model op %d on %S", op->op, op->model.c_str());
                if (op->op == 0) {
                    ok = g_client.updateModels(op->token, progress_to_dialog, op->dlg, error,
                                               op->model);
                    summary = model_title(op->model) +
                              L" is installed and up to date, and its voices were rebuilt.";
                } else if (op->op == 1) {
                    ok = g_client.removeModel(op->model, summary, error);
                } else {
                    ok = g_client.setSettings(op->quantize, op->continuation,
                                              progress_to_dialog, op->dlg, summary, error);
                }
                if (!ok) {
                    DEBUG_LOG("manager: model op %d failed: %S", op->op, error.c_str());
                }
                post_done(op->dlg, ok, ok ? summary : error);
                delete op;
                return 0;
            }, op)) {
                delete op;
                set_busy(false);
                append_log(dlg, IDC_LOG, L"The work could not be started.");
            }
            return TRUE;
        }
        case IDCANCEL:
            if (!busy) {
                EndDialog(dlg, IDOK);
            }
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// ---------------------------------------------------------------------------
// Voice model dialog: rebuild a voice for another AI model, or copy it
// ---------------------------------------------------------------------------

struct VoiceModelParams {
    HWND dlg;
    std::wstring voice;
    std::wstring model;
    std::wstring copyName;
};

HostVoice g_vmVoice;                      // the voice the dialog is about
std::vector<std::wstring> g_vmModels;     // ids behind its model list

void suggest_copy_name(HWND dlg)
{
    const std::wstring model = combo_model(dlg, IDC_VM_MODEL, g_vmModels);
    set_text(dlg, IDC_VM_NAME, g_vmVoice.name + L" " + model_short_name(model));
}

INT_PTR CALLBACK VoiceModelDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool working = false;
    static bool nameTyped = false;

    switch (msg) {
    case WM_INITDIALOG: {
        working = false;
        nameTyped = false;
        const std::wstring intro = L"\"" + g_vmVoice.name + L"\" is made with " +
            model_title(g_vmVoice.model) +
            L". It is rebuilt from its audio sample for the model you choose.";
        set_text(dlg, IDC_VM_INTRO, intro);
        const std::wstring current = g_vmVoice.model.empty() ? kStandardModel : g_vmVoice.model;
        g_vmModels = fill_model_combo(GetDlgItem(dlg, IDC_VM_MODEL), L"", current);
        if (g_vmModels.empty()) {
            append_log(dlg, IDC_LOG, L"No other AI model is installed. Download one under "
                                     L"AI Models first.");
            EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
        }
        CheckDlgButton(dlg, IDC_VM_COPY, BST_CHECKED);
        suggest_copy_name(dlg);
        nameTyped = false;
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        append_log(dlg, IDC_LOG, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_DONE: {
        auto* message = reinterpret_cast<std::wstring*>(lparam);
        const std::wstring text = message ? *message : std::wstring();
        delete message;
        working = false;
        EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
        append_log(dlg, IDC_LOG, text);
        if (wparam) {
            MessageBoxW(dlg, text.c_str(), L"Voice made", MB_OK | MB_ICONINFORMATION);
            EndDialog(dlg, IDOK);
        } else {
            MessageBoxW(dlg, text.c_str(), L"The voice could not be made",
                        MB_OK | MB_ICONERROR);
        }
        return TRUE;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_VM_MODEL:
            if (HIWORD(wparam) == CBN_SELCHANGE && !nameTyped) {
                suggest_copy_name(dlg);
                nameTyped = false;
            }
            return TRUE;
        case IDC_VM_NAME:
            if (HIWORD(wparam) == EN_CHANGE && GetFocus() == GetDlgItem(dlg, IDC_VM_NAME)) {
                nameTyped = true;
            }
            return TRUE;
        case IDC_VM_COPY:
            EnableWindow(GetDlgItem(dlg, IDC_VM_NAME),
                         IsDlgButtonChecked(dlg, IDC_VM_COPY) == BST_CHECKED);
            return TRUE;
        case IDOK: {
            if (working) {
                return TRUE;
            }
            auto* params = new VoiceModelParams();
            params->dlg = dlg;
            params->voice = g_vmVoice.name;
            params->model = combo_model(dlg, IDC_VM_MODEL, g_vmModels);
            if (IsDlgButtonChecked(dlg, IDC_VM_COPY) == BST_CHECKED) {
                params->copyName = get_text(dlg, IDC_VM_NAME);
                if (params->copyName.empty()) {
                    MessageBoxW(dlg, L"Please enter a name for the copy.",
                                L"Missing information", MB_OK | MB_ICONWARNING);
                    delete params;
                    return TRUE;
                }
            }
            if (params->model.empty()) {
                delete params;
                return TRUE;
            }
            working = true;
            EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
            append_log(dlg, IDC_LOG, L"Starting...");
            if (!start_worker([](LPVOID p) -> DWORD {
                auto* params = static_cast<VoiceModelParams*>(p);
                std::wstring summary;
                std::wstring error;
                const bool ok = g_client.setVoiceModel(params->voice, params->model,
                                                       params->copyName, progress_to_dialog,
                                                       params->dlg, summary, error);
                post_done(params->dlg, ok, ok ? summary : error);
                delete params;
                return 0;
            }, params)) {
                delete params;
                working = false;
                EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
            }
            return TRUE;
        }
        case IDCANCEL:
            if (!working) {
                EndDialog(dlg, IDCANCEL);
            }
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// ---------------------------------------------------------------------------
// Voice packages: export voices for sharing, import voices from other users
// ---------------------------------------------------------------------------

std::vector<int> selected_indices(HWND dlg, int listId)
{
    HWND list = GetDlgItem(dlg, listId);
    const LRESULT count = SendMessageW(list, LB_GETSELCOUNT, 0, 0);
    std::vector<int> indices;
    if (count > 0) {
        indices.resize(static_cast<size_t>(count));
        SendMessageW(list, LB_GETSELITEMS, static_cast<WPARAM>(count),
                     reinterpret_cast<LPARAM>(indices.data()));
    }
    return indices;
}

// Moves focus the way the dialog manager does. A bare SetFocus would leave
// the button the user just pressed as the default push button, so Enter would
// press it again instead of Export or Import.
void focus_control(HWND dlg, HWND control)
{
    SendMessageW(dlg, WM_NEXTDLGCTL, reinterpret_cast<WPARAM>(control), TRUE);
}

// Selects or clears every item, then moves focus to the list so a screen
// reader announces the new state instead of leaving it silent.
void set_whole_selection(HWND dlg, int listId, bool selected)
{
    HWND list = GetDlgItem(dlg, listId);
    SendMessageW(list, LB_SETSEL, selected ? TRUE : FALSE, static_cast<LPARAM>(-1));
    focus_control(dlg, list);
}

void select_all_items(HWND dlg, int listId)
{
    HWND list = GetDlgItem(dlg, listId);
    SendMessageW(list, LB_SETSEL, TRUE, static_cast<LPARAM>(-1));
    SendMessageW(list, LB_SETCARETINDEX, 0, 0);
}

std::wstring default_package_path()
{
    PWSTR folder = nullptr;
    std::wstring dir;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &folder))) {
        dir = folder;
    }
    if (folder) {
        CoTaskMemFree(folder);
    }
    SYSTEMTIME st;
    GetLocalTime(&st);
    wchar_t name[64];
    swprintf_s(name, L"Pocket TTS Voices %04u-%02u-%02u.pttsvoices",
               st.wYear, st.wMonth, st.wDay);
    return dir.empty() ? std::wstring(name) : dir + L"\\" + name;
}

void copy_to_buffer(const std::wstring& text, wchar_t* buffer, size_t capacity)
{
    buffer[0] = L'\0';
    if (!text.empty() && text.size() < capacity) {
        memcpy(buffer, text.c_str(), (text.size() + 1) * sizeof(wchar_t));
    }
}

struct ExportParams {
    HWND dlg;
    std::vector<std::wstring> names;
    std::wstring dest;
    bool includeSources;
};

struct ImportParams {
    HWND dlg;
    std::wstring path;
    std::vector<std::wstring> names;
    bool publish;
    uint8_t collision;
};

INT_PTR CALLBACK ExportDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool exporting = false;

    switch (msg) {
    case WM_INITDIALOG: {
        exporting = false;
        HWND list = GetDlgItem(dlg, IDC_EXPORT_LIST);
        SendMessageW(list, LB_RESETCONTENT, 0, 0);
        // Snapshot the names: a refresh on the main window can replace
        // g_voices while this dialog is open, and the list indices have to
        // keep meaning what they meant when the list was filled.
        g_exportNames.clear();
        for (const auto& v : g_voices) {
            g_exportNames.push_back(v.name);
            std::wstring item = v.name;
            item += v.female ? L" (female, " : L" (male, ";
            item += v.published ? L"published, " : L"not published, ";
            item += v.hasSource ? L"audio sample included)" : L"no audio sample)";
            SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(item.c_str()));
        }
        select_all_items(dlg, IDC_EXPORT_LIST);
        CheckDlgButton(dlg, IDC_EXPORT_SOURCES, BST_CHECKED);
        set_text(dlg, IDC_EXPORT_FILE, default_package_path());
        wchar_t line[160];
        swprintf_s(line,
                   L"%zu voice(s) selected. Press Export to write them all to one file "
                   L"you can send to somebody else.",
                   g_exportNames.size());
        append_log(dlg, IDC_LOG, line);
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        append_log(dlg, IDC_LOG, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_DONE: {
        auto* message = reinterpret_cast<std::wstring*>(lparam);
        exporting = false;
        EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
        const std::wstring text = message ? *message : std::wstring();
        delete message;
        append_log(dlg, IDC_LOG, text);
        if (wparam) {
            MessageBoxW(dlg, text.empty() ? L"The voices were exported." : text.c_str(),
                        L"Voices exported", MB_OK | MB_ICONINFORMATION);
            EndDialog(dlg, IDOK);
        } else {
            MessageBoxW(dlg, text.empty() ? L"The export failed." : text.c_str(),
                        L"Export failed", MB_OK | MB_ICONERROR);
        }
        return TRUE;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_EXPORT_ALL:
            if (!exporting) {
                set_whole_selection(dlg, IDC_EXPORT_LIST, true);
            }
            return TRUE;
        case IDC_EXPORT_NONE:
            if (!exporting) {
                set_whole_selection(dlg, IDC_EXPORT_LIST, false);
            }
            return TRUE;
        case IDC_EXPORT_BROWSE: {
            // Never while exporting: the dialog can finish and close itself
            // with a modal Save box still on the stack.
            if (exporting) {
                return TRUE;
            }
            wchar_t file[MAX_PATH];
            copy_to_buffer(get_text(dlg, IDC_EXPORT_FILE), file, MAX_PATH);
            OPENFILENAMEW ofn = {sizeof(ofn)};
            ofn.hwndOwner = dlg;
            ofn.lpstrFilter = L"Pocket TTS voice packages (*.pttsvoices)\0*.pttsvoices\0All files\0*.*\0";
            ofn.lpstrFile = file;
            ofn.nMaxFile = MAX_PATH;
            ofn.lpstrDefExt = L"pttsvoices";
            ofn.Flags = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST | OFN_NOREADONLYRETURN;
            ofn.lpstrTitle = L"Save the voice package";
            if (GetSaveFileNameW(&ofn)) {
                set_text(dlg, IDC_EXPORT_FILE, file);
            }
            return TRUE;
        }
        case IDOK: {
            if (exporting) {
                return TRUE;
            }
            const std::vector<int> chosen = selected_indices(dlg, IDC_EXPORT_LIST);
            if (chosen.empty()) {
                MessageBoxW(dlg, L"Please select at least one voice to export.",
                            L"No voices selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            const std::wstring dest = get_text(dlg, IDC_EXPORT_FILE);
            if (dest.empty()) {
                MessageBoxW(dlg, L"Please choose where the voice package should be saved.",
                            L"No file chosen", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            auto* params = new ExportParams();
            params->dlg = dlg;
            params->dest = dest;
            params->includeSources =
                IsDlgButtonChecked(dlg, IDC_EXPORT_SOURCES) == BST_CHECKED;
            for (int index : chosen) {
                if (index >= 0 && static_cast<size_t>(index) < g_exportNames.size()) {
                    params->names.push_back(g_exportNames[static_cast<size_t>(index)]);
                }
            }
            exporting = true;
            EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
            append_log(dlg, IDC_LOG, L"Writing the voice package...");
            if (!start_worker([](LPVOID p) -> DWORD {
                auto* params = static_cast<ExportParams*>(p);
                DEBUG_LOG("manager: exporting %u voice(s) to \"%S\"",
                          (unsigned)params->names.size(), params->dest.c_str());
                std::wstring summary;
                std::wstring error;
                const bool ok = g_client.exportVoices(
                    params->names, params->dest, params->includeSources,
                    [](const std::wstring& message, void* user) {
                        post_status(static_cast<HWND>(user), message);
                    },
                    params->dlg, summary, error);
                if (!ok) {
                    DEBUG_LOG("manager: export failed: %S", error.c_str());
                }
                post_done(params->dlg, ok, ok ? summary : error);
                delete params;
                return 0;
            }, params)) {
                delete params;
                exporting = false;
                EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
                append_log(dlg, IDC_LOG, L"The export could not be started.");
            }
            return TRUE;
        }
        case IDCANCEL:
            if (!exporting) {
                EndDialog(dlg, IDCANCEL);
            }
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// Reading a package asks the engine host to open it and compare its AI model
// with this computer's, which can take a moment, so it runs on a worker.
void inspect_package_async(HWND dlg, const std::wstring& path)
{
    g_inspecting = true;
    g_inspectedPath = path;
    // Where the user was when the read started. Reading can take a while on a
    // cold engine, and focus is only moved to the results if they are still
    // waiting on the same control.
    g_inspectFocus = GetFocus();
    EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
    EnableWindow(GetDlgItem(dlg, IDC_IMPORT_BROWSE), FALSE);
    append_log(dlg, IDC_LOG, L"Reading the voice package...");
    auto* param = new std::pair<HWND, std::wstring>(dlg, path);
    if (!start_worker([](LPVOID p) -> DWORD {
        auto* param = static_cast<std::pair<HWND, std::wstring>*>(p);
        auto* info = new PackageInfo();
        std::wstring error;
        if (!g_client.inspectPackage(param->second, *info, error)) {
            DEBUG_LOG("manager: inspect failed: %S", error.c_str());
            info->voices.clear();
            info->summary = error;
        }
        PostMessageW(param->first, WM_APP_PACKAGE, 0,
                     reinterpret_cast<LPARAM>(info));
        delete param;
        return 0;
    }, param)) {
        delete param;
        g_inspecting = false;
        g_inspectedPath.clear();
        EnableWindow(GetDlgItem(dlg, IDC_IMPORT_BROWSE), TRUE);
        append_log(dlg, IDC_LOG, L"The voice package could not be read.");
    }
}

INT_PTR CALLBACK ImportDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    static bool importing = false;
    static bool closePending = false;

    switch (msg) {
    case WM_INITDIALOG: {
        importing = false;
        closePending = false;
        g_inspecting = false;
        g_package.clear();
        g_inspectedPath.clear();
        g_inspectFocus = nullptr;
        HWND combo = GetDlgItem(dlg, IDC_IMPORT_COLLISION);
        SendMessageW(combo, CB_ADDSTRING, 0,
                     reinterpret_cast<LPARAM>(L"Import it under a new name"));
        SendMessageW(combo, CB_ADDSTRING, 0,
                     reinterpret_cast<LPARAM>(L"Skip it and keep the voice I have"));
        SendMessageW(combo, CB_ADDSTRING, 0,
                     reinterpret_cast<LPARAM>(L"Replace the voice I have"));
        SendMessageW(combo, CB_SETCURSEL, 0, 0);
        CheckDlgButton(dlg, IDC_IMPORT_PUBLISH, BST_CHECKED);
        EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
        if (!g_importPath.empty()) {
            set_text(dlg, IDC_IMPORT_FILE, g_importPath);
            inspect_package_async(dlg, g_importPath);
        } else {
            append_log(dlg, IDC_LOG,
                       L"Choose a voice package file, then pick the voices to import.");
        }
        return TRUE;
    }
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        append_log(dlg, IDC_LOG, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_PACKAGE: {
        auto* info = reinterpret_cast<PackageInfo*>(lparam);
        g_inspecting = false;
        if (closePending) {
            delete info;
            EndDialog(dlg, IDCANCEL);
            return TRUE;
        }
        EnableWindow(GetDlgItem(dlg, IDC_IMPORT_BROWSE), TRUE);
        HWND list = GetDlgItem(dlg, IDC_IMPORT_LIST);
        SendMessageW(list, LB_RESETCONTENT, 0, 0);
        g_package.clear();
        append_log(dlg, IDC_LOG, info->summary);
        if (info->voices.empty()) {
            MessageBoxW(dlg,
                        info->summary.empty() ? L"The voice package could not be read."
                                              : info->summary.c_str(),
                        L"The package could not be read", MB_OK | MB_ICONERROR);
        } else {
            g_package = info->voices;
            for (const auto& v : g_package) {
                std::wstring item = v.name;
                item += v.female ? L" (female" : L" (male";
                item += v.hasSource ? L", audio sample included" : L", no audio sample";
                if (v.status == PACKAGE_WILL_REBUILD) {
                    item += L", will be rebuilt for your AI model";
                } else if (v.status == PACKAGE_NO_SAMPLE) {
                    item += L", made with a different AI model, may not sound correct";
                } else if (v.status == PACKAGE_NEEDS_MODEL) {
                    item += L", needs an AI model that is not installed";
                }
                item += L")";
                SendMessageW(list, LB_ADDSTRING, 0,
                             reinterpret_cast<LPARAM>(item.c_str()));
            }
            select_all_items(dlg, IDC_IMPORT_LIST);
            EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
            // Only take focus if the user is still waiting where they were;
            // pulling it away from somewhere else would be disorienting.
            if (GetFocus() == g_inspectFocus) {
                focus_control(dlg, list);
            }
        }
        delete info;
        return TRUE;
    }
    case WM_APP_DONE: {
        auto* message = reinterpret_cast<std::wstring*>(lparam);
        importing = false;
        EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
        const std::wstring text = message ? *message : std::wstring();
        delete message;
        append_log(dlg, IDC_LOG, text);
        if (wparam) {
            MessageBoxW(dlg, text.empty() ? L"The voices were imported." : text.c_str(),
                        L"Voices imported", MB_OK | MB_ICONINFORMATION);
            EndDialog(dlg, IDOK);
        } else {
            MessageBoxW(dlg, text.empty() ? L"The import failed." : text.c_str(),
                        L"Import failed", MB_OK | MB_ICONERROR);
        }
        return TRUE;
    }
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_IMPORT_FILE:
            // A path can be typed or pasted as well as browsed to; read it as
            // soon as the user moves on, so Import stops being unavailable.
            if (HIWORD(wparam) == EN_KILLFOCUS && !importing && !g_inspecting) {
                const std::wstring typed = get_text(dlg, IDC_IMPORT_FILE);
                if (!typed.empty() && typed != g_inspectedPath) {
                    inspect_package_async(dlg, typed);
                }
            }
            return TRUE;
        case IDC_IMPORT_ALL:
            if (!importing) {
                set_whole_selection(dlg, IDC_IMPORT_LIST, true);
            }
            return TRUE;
        case IDC_IMPORT_NONE:
            if (!importing) {
                set_whole_selection(dlg, IDC_IMPORT_LIST, false);
            }
            return TRUE;
        case IDC_IMPORT_BROWSE: {
            if (importing || g_inspecting) {
                return TRUE;
            }
            wchar_t file[MAX_PATH];
            copy_to_buffer(get_text(dlg, IDC_IMPORT_FILE), file, MAX_PATH);
            OPENFILENAMEW ofn = {sizeof(ofn)};
            ofn.hwndOwner = dlg;
            ofn.lpstrFilter = L"Pocket TTS voice packages (*.pttsvoices)\0*.pttsvoices\0All files\0*.*\0";
            ofn.lpstrFile = file;
            ofn.nMaxFile = MAX_PATH;
            ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
            ofn.lpstrTitle = L"Choose a voice package to import";
            if (GetOpenFileNameW(&ofn)) {
                set_text(dlg, IDC_IMPORT_FILE, file);
                inspect_package_async(dlg, file);
            }
            return TRUE;
        }
        case IDOK: {
            if (importing || g_inspecting) {
                return TRUE;
            }
            const std::wstring path = get_text(dlg, IDC_IMPORT_FILE);
            if (path.empty() || g_package.empty()) {
                MessageBoxW(dlg, L"Please choose a voice package file first.",
                            L"No package chosen", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            const std::vector<int> chosen = selected_indices(dlg, IDC_IMPORT_LIST);
            if (chosen.empty()) {
                MessageBoxW(dlg, L"Please select at least one voice to import.",
                            L"No voices selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            auto* params = new ImportParams();
            params->dlg = dlg;
            params->path = path;
            params->publish =
                IsDlgButtonChecked(dlg, IDC_IMPORT_PUBLISH) == BST_CHECKED;
            static const uint8_t kCollisionModes[] = {
                COLLISION_RENAME, COLLISION_SKIP, COLLISION_REPLACE};
            const LRESULT mode =
                SendDlgItemMessageW(dlg, IDC_IMPORT_COLLISION, CB_GETCURSEL, 0, 0);
            params->collision = (mode >= 0 && mode < 3)
                ? kCollisionModes[mode] : COLLISION_RENAME;
            for (int index : chosen) {
                if (index >= 0 && static_cast<size_t>(index) < g_package.size()) {
                    params->names.push_back(g_package[static_cast<size_t>(index)].name);
                }
            }
            importing = true;
            EnableWindow(GetDlgItem(dlg, IDOK), FALSE);
            append_log(dlg, IDC_LOG, L"Importing the selected voices...");
            if (!start_worker([](LPVOID p) -> DWORD {
                auto* params = static_cast<ImportParams*>(p);
                DEBUG_LOG("manager: importing %u voice(s) from \"%S\"",
                          (unsigned)params->names.size(), params->path.c_str());
                std::wstring summary;
                std::wstring error;
                const bool ok = g_client.importVoices(
                    params->path, params->names, params->publish, params->collision,
                    [](const std::wstring& message, void* user) {
                        post_status(static_cast<HWND>(user), message);
                    },
                    params->dlg, summary, error);
                if (!ok) {
                    DEBUG_LOG("manager: import failed: %S", error.c_str());
                }
                post_done(params->dlg, ok, ok ? summary : error);
                delete params;
                return 0;
            }, params)) {
                delete params;
                importing = false;
                EnableWindow(GetDlgItem(dlg, IDOK), TRUE);
                append_log(dlg, IDC_LOG, L"The import could not be started.");
            }
            return TRUE;
        }
        case IDCANCEL:
            if (importing) {
                return TRUE;
            }
            if (g_inspecting) {
                // Let the worker finish with the dialog before it goes away.
                closePending = true;
                append_log(dlg, IDC_LOG, L"Closing once the package has been read...");
                return TRUE;
            }
            EndDialog(dlg, IDCANCEL);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

// ---------------------------------------------------------------------------
// Simple worker for publish / unpublish / delete
// ---------------------------------------------------------------------------

struct SimpleOp {
    HWND dlg;
    std::wstring voice;
    int op;  // 0 publish, 1 unpublish, 2 delete
};

void run_simple_op(HWND dlg, const std::wstring& voice, int op)
{
    g_busy = true;
    post_status(dlg, L"Working...");
    auto* params = new SimpleOp{dlg, voice, op};
    CreateThread(nullptr, 0, [](LPVOID p) -> DWORD {
        auto* params = static_cast<SimpleOp*>(p);
        DEBUG_LOG("manager: op %d on voice \"%S\"", params->op, params->voice.c_str());
        std::wstring error;
        bool ok = false;
        switch (params->op) {
        case 0: ok = g_client.setPublished(params->voice, true, error); break;
        case 1: ok = g_client.setPublished(params->voice, false, error); break;
        case 2: ok = g_client.deleteVoice(params->voice, error); break;
        }
        if (!ok) {
            DEBUG_LOG("manager: op %d failed: %S", params->op, error.c_str());
        }
        if (ok) {
            const wchar_t* done = params->op == 0
                ? L"Published. The voice is now available to all SAPI applications."
                : (params->op == 1 ? L"Unpublished. The voice was removed from the SAPI voice list."
                                   : L"The voice was deleted.");
            post_status(params->dlg, done);
        } else {
            post_status(params->dlg, L"Error: " + error);
        }
        PostMessageW(params->dlg, WM_APP_DONE, ok ? 1 : 0, 0);
        delete params;
        return 0;
    }, params, 0, nullptr);
}

// ---------------------------------------------------------------------------
// Main dialog proc
// ---------------------------------------------------------------------------

INT_PTR CALLBACK MainDlgProc(HWND dlg, UINT msg, WPARAM wparam, LPARAM lparam)
{
    switch (msg) {
    case WM_INITDIALOG:
        refresh_voices_async(dlg);
        if (!g_importPath.empty()) {
            // Opened by double-clicking a .pttsvoices file.
            PostMessageW(dlg, WM_COMMAND, IDC_IMPORT, 0);
        }
        return TRUE;
    case WM_APP_STATUS: {
        auto* text = reinterpret_cast<std::wstring*>(lparam);
        set_text(dlg, IDC_STATUS, *text);
        delete text;
        return TRUE;
    }
    case WM_APP_VOICES: {
        auto* result = reinterpret_cast<VoiceRefresh*>(lparam);
        if (result) {
            g_voices = std::move(result->voices);
            if (!result->models.empty()) {
                g_models = std::move(result->models);
            }
            delete result;
            fill_voice_list(dlg);
            wchar_t status[64];
            swprintf_s(status, L"%zu voice(s).", g_voices.size());
            set_text(dlg, IDC_STATUS, status);
        }
        return TRUE;
    }
    case WM_APP_DONE:
        g_busy = false;
        refresh_voices_async(dlg);
        return TRUE;
    case WM_COMMAND:
        switch (LOWORD(wparam)) {
        case IDC_CLONE:
            if (!g_busy) {
                if (DialogBoxW(g_instance, MAKEINTRESOURCEW(IDD_CLONE), dlg, CloneDlgProc) == IDOK) {
                    refresh_voices_async(dlg);
                }
            }
            return TRUE;
        case IDC_TEST: {
            const HostVoice* voice = selected_voice(dlg);
            if (!voice) {
                MessageBoxW(dlg, L"Please select a voice in the list first.",
                            L"No voice selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            if (!g_busy) {
                TestContext ctx;
                ctx.voice = voice->name;
                g_test = &ctx;
                DialogBoxW(g_instance, MAKEINTRESOURCEW(IDD_TEST), dlg, TestDlgProc);
                g_test = nullptr;
            }
            return TRUE;
        }
        case IDC_PUBLISH:
        case IDC_UNPUBLISH: {
            const HostVoice* voice = selected_voice(dlg);
            if (!voice) {
                MessageBoxW(dlg, L"Please select a voice in the list first.",
                            L"No voice selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            if (!g_busy) {
                g_selectedVoice = voice->name;
                run_simple_op(dlg, voice->name, LOWORD(wparam) == IDC_PUBLISH ? 0 : 1);
            }
            return TRUE;
        }
        case IDC_DELETE: {
            const HostVoice* voice = selected_voice(dlg);
            if (!voice) {
                MessageBoxW(dlg, L"Please select a voice in the list first.",
                            L"No voice selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            if (!g_busy) {
                std::wstring prompt = L"Delete the voice \"" + voice->name +
                    L"\" permanently? This also removes it from the SAPI voice list.";
                if (MessageBoxW(dlg, prompt.c_str(), L"Delete voice",
                                MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES) {
                    g_selectedVoice.clear();
                    run_simple_op(dlg, voice->name, 2);
                }
            }
            return TRUE;
        }
        case IDC_EXPORT:
            if (!g_busy) {
                if (g_voices.empty()) {
                    MessageBoxW(dlg, L"There are no voices to export yet.",
                                L"No voices", MB_OK | MB_ICONWARNING);
                    return TRUE;
                }
                DialogBoxW(g_instance, MAKEINTRESOURCEW(IDD_EXPORT), dlg,
                           ExportDlgProc);
            }
            return TRUE;
        case IDC_IMPORT:
            if (!g_busy) {
                const INT_PTR result = DialogBoxW(
                    g_instance, MAKEINTRESOURCEW(IDD_IMPORT), dlg, ImportDlgProc);
                g_importPath.clear();
                if (result == IDOK) {
                    refresh_voices_async(dlg);
                }
            }
            return TRUE;
        case IDC_UPDATE:
            if (!g_busy) {
                DialogBoxW(g_instance, MAKEINTRESOURCEW(IDD_UPDATE), dlg, ModelsDlgProc);
                // A download rebuilds voices and a removal changes what is
                // installed, so the list is read again either way.
                refresh_voices_async(dlg);
            }
            return TRUE;
        case IDC_VOICE_MODEL: {
            const HostVoice* voice = selected_voice(dlg);
            if (!voice) {
                MessageBoxW(dlg, L"Please select a voice in the list first.",
                            L"No voice selected", MB_OK | MB_ICONWARNING);
                return TRUE;
            }
            if (!voice->hasSource) {
                MessageBoxW(dlg,
                            L"This voice has no audio sample on this computer, so it cannot "
                            L"be made with another AI model. Clone it again from the "
                            L"original recording.",
                            L"Change AI Model", MB_OK | MB_ICONINFORMATION);
                return TRUE;
            }
            if (!g_busy) {
                g_vmVoice = *voice;
                g_selectedVoice = voice->name;
                if (DialogBoxW(g_instance, MAKEINTRESOURCEW(IDD_VOICE_MODEL), dlg,
                               VoiceModelDlgProc) == IDOK) {
                    refresh_voices_async(dlg);
                }
            }
            return TRUE;
        }
        case IDC_REFRESH:
            if (!g_busy) {
                refresh_voices_async(dlg);
            }
            return TRUE;
        case IDCANCEL:
            EndDialog(dlg, 0);
            return TRUE;
        }
        break;
    }
    return FALSE;
}

}  // namespace

// True when `path` ends with `suffix`, compared case-insensitively.
static bool has_suffix(const std::wstring& path, const wchar_t* suffix)
{
    const size_t length = wcslen(suffix);
    return path.size() > length &&
           _wcsicmp(path.c_str() + path.size() - length, suffix) == 0;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR commandLine, int)
{
    g_instance = instance;
    DEBUG_LOG("manager: starting");

    // Windows passes a .pttsvoices file here when one is opened from
    // Explorer; the main window then opens the import dialog for it.
    if (commandLine && *commandLine) {
        int argc = 0;
        if (LPWSTR* argv = CommandLineToArgvW(commandLine, &argc)) {
            for (int i = 0; i < argc; ++i) {
                const DWORD attributes = GetFileAttributesW(argv[i]);
                if (attributes != INVALID_FILE_ATTRIBUTES &&
                    !(attributes & FILE_ATTRIBUTE_DIRECTORY) &&
                    has_suffix(argv[i], L".pttsvoices")) {
                    g_importPath = argv[i];
                    DEBUG_LOG("manager: opening package \"%S\"",
                              g_importPath.c_str());
                    break;
                }
            }
            LocalFree(argv);
        }
    }
    INITCOMMONCONTROLSEX icc = {sizeof(icc), ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&icc);
    const INT_PTR result = DialogBoxW(instance, MAKEINTRESOURCEW(IDD_MAIN), nullptr, MainDlgProc);
    if (result == -1) {
        wchar_t message[128];
        swprintf_s(message, L"The main window could not be created (error %lu).", GetLastError());
        MessageBoxW(nullptr, message, L"Pocket TTS Voice Manager", MB_OK | MB_ICONERROR);
        return 1;
    }
    return 0;
}
