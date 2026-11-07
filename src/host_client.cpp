#include "host_client.h"

#include <ws2tcpip.h>
#include <shlwapi.h>
#include <shlobj.h>

#include "utils.hpp"
#include "debug_log.h"

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "shlwapi.lib")

namespace {

constexpr wchar_t LAUNCH_MUTEX[] = L"Local\\PocketTTSLaunchMutex";

class CsLock {
public:
    explicit CsLock(CRITICAL_SECTION* cs) noexcept : cs_(cs) { EnterCriticalSection(cs_); }
    ~CsLock() { LeaveCriticalSection(cs_); }
    CsLock(const CsLock&) = delete;
    CsLock& operator=(const CsLock&) = delete;
private:
    CRITICAL_SECTION* cs_;
};

std::wstring local_appdata_dir()
{
    PWSTR folder = nullptr;
    std::wstring result;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_LocalAppData, 0, nullptr, &folder))) {
        result = folder;
    }
    if (folder) {
        CoTaskMemFree(folder);
    }
    return result;
}

int read_port_file()
{
    const std::wstring path = local_appdata_dir() + L"\\PocketTTS\\host.port";
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        return 0;
    }
    char buf[16] = {};
    DWORD read = 0;
    ReadFile(h, buf, sizeof(buf) - 1, &read, nullptr);
    CloseHandle(h);
    return atoi(buf);
}

HMODULE current_module()
{
    HMODULE hm = nullptr;
    GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&read_port_file), &hm);
    return hm;
}

bool file_exists(const std::wstring& path)
{
    const DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

// Builds the host command line: an explicit POCKETTTS_HOST_CMD override,
// or <base>\runtime\{PocketTTSHost.exe|pythonw.exe} <base>\host\pockettts_host.py
// where <base> is the DLL's directory or one of its parents (the x64 DLL
// and the manager live in subdirectories of the install root).
std::wstring host_command()
{
    wchar_t env[2048];
    DWORD n = GetEnvironmentVariableW(L"POCKETTTS_HOST_CMD", env, 2048);
    if (n > 0 && n < 2048) {
        return std::wstring(env, n);
    }

    wchar_t module_path[MAX_PATH] = {};
    if (HMODULE hm = current_module()) {
        GetModuleFileNameW(hm, module_path, MAX_PATH);
        PathRemoveFileSpecW(module_path);
    }

    std::wstring base = module_path;
    for (int depth = 0; depth < 3 && !base.empty(); ++depth) {
        const std::wstring script = base + L"\\host\\pockettts_host.py";
        if (file_exists(script)) {
            const wchar_t* exes[] = {L"\\runtime\\PocketTTSHost.exe", L"\\runtime\\pythonw.exe"};
            for (const wchar_t* exe : exes) {
                const std::wstring exe_path = base + exe;
                if (file_exists(exe_path)) {
                    return L"\"" + exe_path + L"\" \"" + script + L"\"";
                }
            }
        }
        const size_t slash = base.find_last_of(L'\\');
        if (slash == std::wstring::npos) {
            break;
        }
        base.resize(slash);
    }
    return {};
}

}  // namespace

HostClient::HostClient()
    : sock_(INVALID_SOCKET)
    , wsaReady_(false)
    , speedFactor_(POCKETTTS_SPEED_UNKNOWN)
{
    InitializeCriticalSection(&cs_);
    WSADATA wsa;
    wsaReady_ = (WSAStartup(MAKEWORD(2, 2), &wsa) == 0);
}

HostClient::~HostClient()
{
    disconnect();
    if (wsaReady_) {
        WSACleanup();
    }
    DeleteCriticalSection(&cs_);
}

void HostClient::disconnect()
{
    if (sock_ != INVALID_SOCKET) {
        closesocket(sock_);
        sock_ = INVALID_SOCKET;
    }
}

// PONG and AUDIO_END carry the host's measured generation speed. Hosts
// older than 1.1.1 send neither, and the factor simply stays unknown.
void HostClient::noteSpeedFactor(const std::vector<char>& payload)
{
    if (payload.size() < sizeof(float)) {
        return;
    }
    float factor = POCKETTTS_SPEED_UNKNOWN;
    memcpy(&factor, payload.data(), sizeof(factor));
    if (factor > 0.0f && factor < 100.0f) {
        speedFactor_ = factor;
    }
}

void HostClient::setRecvTimeout(DWORD ms)
{
    if (sock_ != INVALID_SOCKET) {
        setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO,
                   reinterpret_cast<const char*>(&ms), sizeof(ms));
    }
}

bool HostClient::tryPort(unsigned short port)
{
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        return false;
    }

    // Non-blocking connect with a short deadline so scanning is fast.
    u_long nonblocking = 1;
    ioctlsocket(s, FIONBIO, &nonblocking);

    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    ::connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));

    fd_set write_set;
    FD_ZERO(&write_set);
    FD_SET(s, &write_set);
    timeval tv = {0, 300000};  // 300 ms
    if (select(0, nullptr, &write_set, nullptr, &tv) != 1) {
        closesocket(s);
        return false;
    }

    u_long blocking = 0;
    ioctlsocket(s, FIONBIO, &blocking);
    DEBUG_LOG("host_client: connected to 127.0.0.1:%u", port);
    BOOL nodelay = TRUE;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY,
               reinterpret_cast<const char*>(&nodelay), sizeof(nodelay));

    sock_ = s;
    setRecvTimeout(5000);

    // Verify it is actually our host.
    if (!sendFrame(CMD_PING, nullptr, 0)) {
        disconnect();
        return false;
    }
    uint32_t type = 0;
    std::vector<char> payload;
    if (!readFrame(type, payload) || type != RESP_PONG) {
        DEBUG_LOG("host_client: port %u did not answer PING; not our host", port);
        disconnect();
        return false;
    }
    noteSpeedFactor(payload);
    return true;
}

bool HostClient::tryConnectOnce()
{
    const int filePort = read_port_file();
    if (filePort > 0 && tryPort(static_cast<unsigned short>(filePort))) {
        return true;
    }
    for (int i = 0; i < POCKETTTS_PORT_COUNT; ++i) {
        if (POCKETTTS_PORTS[i] != filePort && tryPort(POCKETTTS_PORTS[i])) {
            return true;
        }
    }
    return false;
}

bool HostClient::launchHost()
{
    const std::wstring cmd = host_command();
    if (cmd.empty()) {
        DEBUG_LOG("launchHost: no host command found");
        return false;
    }

    HANDLE mutex = CreateMutexW(nullptr, FALSE, LAUNCH_MUTEX);
    if (mutex) {
        const DWORD wait = WaitForSingleObject(mutex, 5000);
        if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED) {
            CloseHandle(mutex);
            return false;
        }
    }

    bool launched = false;
    if (tryConnectOnce()) {
        launched = true;  // someone else won the race
    } else {
        DEBUG_LOG("host_client: launching host: %S", cmd.c_str());
        std::vector<wchar_t> cmdline(cmd.begin(), cmd.end());
        cmdline.push_back(L'\0');

        STARTUPINFOW si = {sizeof(si)};
        si.dwFlags = STARTF_USESHOWWINDOW;
        si.wShowWindow = SW_HIDE;
        PROCESS_INFORMATION pi = {};
        launched = CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, FALSE,
                                  CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
        if (launched) {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        } else {
            DEBUG_LOG("host_client: CreateProcess failed, error %lu", GetLastError());
        }
    }

    if (mutex) {
        ReleaseMutex(mutex);
        CloseHandle(mutex);
    }
    return launched;
}

bool HostClient::ensureConnected(DWORD timeoutMs)
{
    if (sock_ != INVALID_SOCKET) {
        return true;
    }
    if (tryConnectOnce()) {
        return true;
    }
    if (!launchHost()) {
        return false;
    }
    // A cold host imports torch and loads the model; poll until it answers.
    const ULONGLONG deadline = GetTickCount64() + timeoutMs;
    while (GetTickCount64() < deadline) {
        Sleep(300);
        if (tryConnectOnce()) {
            return true;
        }
    }
    DEBUG_LOG("host_client: host did not come up within %lu ms", timeoutMs);
    return false;
}

bool HostClient::connect(DWORD timeoutMs)
{
    CsLock lock(&cs_);
    return ensureConnected(timeoutMs);
}

bool HostClient::sendFrame(uint32_t type, const void* data, uint32_t size)
{
    if (sock_ == INVALID_SOCKET) {
        return false;
    }
    char header[8];
    memcpy(header, &type, 4);
    memcpy(header + 4, &size, 4);
    if (send(sock_, header, 8, 0) != 8) {
        return false;
    }
    const char* p = static_cast<const char*>(data);
    uint32_t remaining = size;
    while (remaining > 0) {
        const int sent = send(sock_, p, static_cast<int>(remaining), 0);
        if (sent <= 0) {
            return false;
        }
        p += sent;
        remaining -= sent;
    }
    return true;
}

bool HostClient::readFrame(uint32_t& type, std::vector<char>& payload)
{
    payload.clear();
    if (sock_ == INVALID_SOCKET) {
        return false;
    }
    char header[8];
    int got = 0;
    while (got < 8) {
        const int r = recv(sock_, header + got, 8 - got, 0);
        if (r <= 0) {
            return false;
        }
        got += r;
    }
    uint32_t size = 0;
    memcpy(&type, header, 4);
    memcpy(&size, header + 4, 4);
    if (size > 64 * 1024 * 1024) {
        return false;
    }
    payload.resize(size);
    uint32_t off = 0;
    while (off < size) {
        const int r = recv(sock_, payload.data() + off, static_cast<int>(size - off), 0);
        if (r <= 0) {
            return false;
        }
        off += static_cast<uint32_t>(r);
    }
    return true;
}

namespace {

void append_u16_string(std::vector<char>& out, const std::string& s)
{
    const uint16_t len = static_cast<uint16_t>(s.size());
    out.insert(out.end(), reinterpret_cast<const char*>(&len),
               reinterpret_cast<const char*>(&len) + 2);
    out.insert(out.end(), s.begin(), s.end());
}

void append_u32_string(std::vector<char>& out, const std::string& s)
{
    const uint32_t len = static_cast<uint32_t>(s.size());
    out.insert(out.end(), reinterpret_cast<const char*>(&len),
               reinterpret_cast<const char*>(&len) + 4);
    out.insert(out.end(), s.begin(), s.end());
}

}  // namespace

bool HostClient::speak(const std::string& voiceUtf8, const std::string& textUtf8,
                       AudioCallback callback, void* user, bool spell)
{
    CsLock lock(&cs_);

    if (!ensureConnected(90000)) {
        return false;
    }

    std::vector<char> payload;
    payload.reserve(voiceUtf8.size() + textUtf8.size() + 9);
    append_u16_string(payload, voiceUtf8);
    append_u32_string(payload, textUtf8);
    payload.push_back(static_cast<char>(spell ? SPEAK_SPELL : 0));

    DEBUG_LOG("speak: voice \"%s\", %u chars%s", voiceUtf8.c_str(),
              (unsigned)textUtf8.size(), spell ? ", spelled" : "");
    setRecvTimeout(120000);
    if (!sendFrame(CMD_SPEAK, payload.data(), static_cast<uint32_t>(payload.size()))) {
        DEBUG_LOG("speak: send failed; dropping connection");
        disconnect();
        return false;
    }

    bool stopped = false;
    bool hadError = false;
    while (true) {
        uint32_t type = 0;
        std::vector<char> data;
        if (!readFrame(type, data)) {
            DEBUG_LOG("speak: connection lost mid-stream");
            disconnect();
            return false;
        }
        if (type == RESP_AUDIO_END) {
            noteSpeedFactor(data);
            break;
        }
        if (type == RESP_ERROR) {
            DEBUG_LOG("speak: host error: %.*s", (int)data.size(), data.data());
            hadError = true;
            continue;  // host still sends AUDIO_END
        }
        if (type == RESP_AUDIO && !stopped && !data.empty()) {
            if (callback && !callback(data.data(), static_cast<uint32_t>(data.size()), user)) {
                DEBUG_LOG("speak: client aborted; sending STOP");
                sendFrame(CMD_STOP, nullptr, 0);
                stopped = true;
            }
        }
        // RESP_OK (from STOP) and anything else: keep draining.
    }
    return !hadError;
}

bool HostClient::listVoices(std::vector<HostVoice>& out, std::wstring& error)
{
    CsLock lock(&cs_);
    out.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    setRecvTimeout(15000);
    if (!sendFrame(CMD_LIST_VOICES, nullptr, 0)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data) || type != RESP_VOICES || data.size() < 4) {
        disconnect();
        error = L"Unexpected reply from the engine host.";
        return false;
    }
    uint32_t count = 0;
    memcpy(&count, data.data(), 4);
    size_t off = 4;
    for (uint32_t i = 0; i < count; ++i) {
        if (off + 2 > data.size()) {
            break;
        }
        uint16_t nameLen = 0;
        memcpy(&nameLen, data.data() + off, 2);
        off += 2;
        if (off + nameLen + 7 > data.size()) {
            break;
        }
        HostVoice v;
        v.name = PocketTts::utils::string_to_wstring(
            std::string(data.data() + off, nameLen));
        off += nameLen;
        v.female = data[off] != 0;
        memcpy(&v.lcid, data.data() + off + 1, 4);
        v.published = data[off + 5] != 0;
        v.hasSource = data[off + 6] != 0;
        off += 7;
        // Hosts from 1.3.0 end each voice with its model's id.
        if (off + 2 <= data.size()) {
            uint16_t modelLen = 0;
            memcpy(&modelLen, data.data() + off, 2);
            if (off + 2 + modelLen <= data.size()) {
                v.model = PocketTts::utils::string_to_wstring(
                    std::string(data.data() + off + 2, modelLen));
                off += 2 + modelLen;
            }
        }
        out.push_back(std::move(v));
    }
    return true;
}

bool HostClient::cloneVoice(const std::wstring& name, const std::wstring& audioPath,
                            bool female, const std::wstring& languageHex,
                            ProgressCallback progress, void* user, std::wstring& error,
                            const std::wstring& modelId)
{
    CsLock lock(&cs_);
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }

    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(name));
    append_u16_string(payload, PocketTts::utils::wstring_to_string(audioPath));
    append_u16_string(payload, PocketTts::utils::wstring_to_string(languageHex));
    const char gender = female ? 1 : 0;
    payload.push_back(gender);
    if (!modelId.empty()) {
        append_u16_string(payload, PocketTts::utils::wstring_to_string(modelId));
    }

    setRecvTimeout(10 * 60 * 1000);
    if (!sendFrame(CMD_CLONE, payload.data(), static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    while (true) {
        uint32_t type = 0;
        std::vector<char> data;
        if (!readFrame(type, data)) {
            disconnect();
            error = L"Connection to the engine host was lost.";
            return false;
        }
        if (type == RESP_PROGRESS) {
            if (progress) {
                progress(PocketTts::utils::string_to_wstring(
                             std::string(data.data(), data.size())), user);
            }
        } else if (type == RESP_OK) {
            return true;
        } else if (type == RESP_ERROR) {
            error = PocketTts::utils::string_to_wstring(
                std::string(data.data(), data.size()));
            return false;
        }
    }
}

bool HostClient::setPublished(const std::wstring& name, bool published, std::wstring& error)
{
    CsLock lock(&cs_);
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(name));
    payload.push_back(published ? 1 : 0);
    setRecvTimeout(15000);
    if (!sendFrame(CMD_SET_PUBLISHED, payload.data(), static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    if (type == RESP_OK) {
        return true;
    }
    error = PocketTts::utils::string_to_wstring(std::string(data.data(), data.size()));
    return false;
}

bool HostClient::deleteVoice(const std::wstring& name, std::wstring& error)
{
    CsLock lock(&cs_);
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(name));
    setRecvTimeout(15000);
    if (!sendFrame(CMD_DELETE_VOICE, payload.data(), static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    if (type == RESP_OK) {
        return true;
    }
    error = PocketTts::utils::string_to_wstring(std::string(data.data(), data.size()));
    return false;
}

bool HostClient::updateModels(const std::wstring& hfToken,
                              ProgressCallback progress, void* user, std::wstring& error,
                              const std::wstring& modelId)
{
    CsLock lock(&cs_);
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(hfToken));
    if (!modelId.empty()) {
        append_u16_string(payload, PocketTts::utils::wstring_to_string(modelId));
    }
    setRecvTimeout(60 * 60 * 1000);
    if (!sendFrame(CMD_UPDATE_MODELS, payload.data(), static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    while (true) {
        uint32_t type = 0;
        std::vector<char> data;
        if (!readFrame(type, data)) {
            disconnect();
            error = L"Connection to the engine host was lost.";
            return false;
        }
        if (type == RESP_PROGRESS) {
            if (progress) {
                progress(PocketTts::utils::string_to_wstring(
                             std::string(data.data(), data.size())), user);
            }
        } else if (type == RESP_OK) {
            return true;
        } else if (type == RESP_ERROR) {
            error = PocketTts::utils::string_to_wstring(
                std::string(data.data(), data.size()));
            return false;
        }
    }
}

namespace {

void append_u32(std::vector<char>& out, uint32_t value)
{
    out.insert(out.end(), reinterpret_cast<const char*>(&value),
               reinterpret_cast<const char*>(&value) + 4);
}

void append_name_list(std::vector<char>& out, const std::vector<std::wstring>& names)
{
    append_u32(out, static_cast<uint32_t>(names.size()));
    for (const std::wstring& name : names) {
        append_u16_string(out, PocketTts::utils::wstring_to_string(name));
    }
}

std::wstring payload_text(const std::vector<char>& data)
{
    if (data.empty()) {
        return {};
    }
    return PocketTts::utils::string_to_wstring(std::string(data.data(), data.size()));
}

}  // namespace

bool HostClient::pumpLongOperation(ProgressCallback progress, void* user,
                                   std::wstring& summary, std::wstring& error)
{
    while (true) {
        uint32_t type = 0;
        std::vector<char> data;
        if (!readFrame(type, data)) {
            disconnect();
            error = L"Connection to the engine host was lost.";
            return false;
        }
        if (type == RESP_PROGRESS) {
            if (progress) {
                progress(payload_text(data), user);
            }
        } else if (type == RESP_OK) {
            summary = payload_text(data);
            return true;
        } else if (type == RESP_ERROR) {
            error = payload_text(data);
            return false;
        }
    }
}

bool HostClient::exportVoices(const std::vector<std::wstring>& names,
                              const std::wstring& destPath, bool includeSources,
                              ProgressCallback progress, void* user,
                              std::wstring& summary, std::wstring& error)
{
    CsLock lock(&cs_);
    summary.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }

    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(destPath));
    payload.push_back(includeSources ? 1 : 0);
    append_name_list(payload, names);

    DEBUG_LOG("export: %u voice(s) to \"%S\" (sources %s)",
              (unsigned)names.size(), destPath.c_str(),
              includeSources ? "included" : "omitted");
    setRecvTimeout(60 * 60 * 1000);
    if (!sendFrame(CMD_EXPORT_VOICES, payload.data(),
                   static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    return pumpLongOperation(progress, user, summary, error);
}

bool HostClient::inspectPackage(const std::wstring& packagePath, PackageInfo& out,
                                std::wstring& error)
{
    CsLock lock(&cs_);
    out.summary.clear();
    out.voices.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }

    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(packagePath));
    setRecvTimeout(120000);
    if (!sendFrame(CMD_INSPECT_PACKAGE, payload.data(),
                   static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }

    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    if (type == RESP_ERROR) {
        error = payload_text(data);
        return false;
    }
    if (type != RESP_PACKAGE || data.size() < 6) {
        error = L"Unexpected reply from the engine host.";
        return false;
    }

    uint16_t summaryLen = 0;
    memcpy(&summaryLen, data.data(), 2);
    size_t off = 2;
    if (off + summaryLen + 4 > data.size()) {
        error = L"The engine host sent a malformed package description.";
        return false;
    }
    out.summary = PocketTts::utils::string_to_wstring(
        std::string(data.data() + off, summaryLen));
    off += summaryLen;

    uint32_t count = 0;
    memcpy(&count, data.data() + off, 4);
    off += 4;
    for (uint32_t i = 0; i < count; ++i) {
        if (off + 2 > data.size()) {
            break;
        }
        uint16_t nameLen = 0;
        memcpy(&nameLen, data.data() + off, 2);
        off += 2;
        if (off + nameLen + 3 > data.size()) {
            break;
        }
        PackageVoice voice;
        voice.name = PocketTts::utils::string_to_wstring(
            std::string(data.data() + off, nameLen));
        off += nameLen;
        voice.female = data[off] != 0;
        voice.hasSource = data[off + 1] != 0;
        voice.status = static_cast<uint8_t>(data[off + 2]);
        off += 3;
        out.voices.push_back(std::move(voice));
    }
    DEBUG_LOG("inspect: \"%S\" holds %u voice(s)", packagePath.c_str(),
              (unsigned)out.voices.size());
    return true;
}

bool HostClient::importVoices(const std::wstring& packagePath,
                              const std::vector<std::wstring>& names, bool publish,
                              uint8_t collision, ProgressCallback progress, void* user,
                              std::wstring& summary, std::wstring& error)
{
    CsLock lock(&cs_);
    summary.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }

    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(packagePath));
    payload.push_back(publish ? 1 : 0);
    payload.push_back(static_cast<char>(collision));
    append_name_list(payload, names);

    DEBUG_LOG("import: %u voice(s) from \"%S\" (publish %d, collision %u)",
              (unsigned)names.size(), packagePath.c_str(), publish ? 1 : 0,
              (unsigned)collision);
    setRecvTimeout(60 * 60 * 1000);
    if (!sendFrame(CMD_IMPORT_VOICES, payload.data(),
                   static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    return pumpLongOperation(progress, user, summary, error);
}

namespace {

// Reads a <u16 length><utf8> string at `off`; false when the data runs out.
bool read_u16_string(const std::vector<char>& data, size_t& off, std::wstring& out)
{
    if (off + 2 > data.size()) {
        return false;
    }
    uint16_t length = 0;
    memcpy(&length, data.data() + off, 2);
    off += 2;
    if (off + length > data.size()) {
        return false;
    }
    out = PocketTts::utils::string_to_wstring(std::string(data.data() + off, length));
    off += length;
    return true;
}

}  // namespace

bool HostClient::listModels(std::vector<HostModel>& out, std::wstring& error)
{
    CsLock lock(&cs_);
    out.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    setRecvTimeout(30000);
    if (!sendFrame(CMD_LIST_MODELS, nullptr, 0)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    if (type != RESP_MODELS || data.size() < 4) {
        error = type == RESP_ERROR
            ? L"The running engine is older than this Voice Manager. Sign out and back in, "
              L"or end PocketTTSHost.exe in Task Manager, and try again."
            : L"Unexpected reply from the engine host.";
        return false;
    }
    uint32_t count = 0;
    memcpy(&count, data.data(), 4);
    size_t off = 4;
    for (uint32_t i = 0; i < count; ++i) {
        HostModel m;
        if (!read_u16_string(data, off, m.id) || !read_u16_string(data, off, m.title) ||
            !read_u16_string(data, off, m.description) ||
            !read_u16_string(data, off, m.loadError) || off + 14 > data.size()) {
            break;
        }
        m.installed = data[off] != 0;
        m.loaded = data[off + 1] != 0;
        memcpy(&m.sizeMb, data.data() + off + 2, 4);
        memcpy(&m.lcid, data.data() + off + 6, 4);
        memcpy(&m.voices, data.data() + off + 10, 4);
        off += 14;
        out.push_back(std::move(m));
    }
    return true;
}

bool HostClient::removeModel(const std::wstring& modelId, std::wstring& summary,
                             std::wstring& error)
{
    CsLock lock(&cs_);
    summary.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(modelId));
    setRecvTimeout(120000);
    if (!sendFrame(CMD_REMOVE_MODEL, payload.data(), static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    return pumpLongOperation(nullptr, nullptr, summary, error);
}

bool HostClient::setVoiceModel(const std::wstring& name, const std::wstring& modelId,
                               const std::wstring& copyName, ProgressCallback progress,
                               void* user, std::wstring& summary, std::wstring& error)
{
    CsLock lock(&cs_);
    summary.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    std::vector<char> payload;
    append_u16_string(payload, PocketTts::utils::wstring_to_string(name));
    append_u16_string(payload, PocketTts::utils::wstring_to_string(modelId));
    payload.push_back(copyName.empty() ? 0 : 1);
    append_u16_string(payload, PocketTts::utils::wstring_to_string(copyName));
    DEBUG_LOG("setVoiceModel: \"%S\" -> %S (copy \"%S\")", name.c_str(),
              modelId.c_str(), copyName.c_str());
    setRecvTimeout(10 * 60 * 1000);
    if (!sendFrame(CMD_SET_VOICE_MODEL, payload.data(),
                   static_cast<uint32_t>(payload.size()))) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    return pumpLongOperation(progress, user, summary, error);
}

bool HostClient::getSettings(bool& quantize, bool& continuation, std::wstring& error)
{
    CsLock lock(&cs_);
    quantize = continuation = false;
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    setRecvTimeout(15000);
    if (!sendFrame(CMD_GET_SETTINGS, nullptr, 0)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    if (type != RESP_SETTINGS || data.size() < 2) {
        error = L"Unexpected reply from the engine host.";
        return false;
    }
    quantize = data[0] != 0;
    continuation = data[1] != 0;
    return true;
}

bool HostClient::setSettings(bool quantize, bool continuation, ProgressCallback progress,
                             void* user, std::wstring& summary, std::wstring& error)
{
    CsLock lock(&cs_);
    summary.clear();
    if (!ensureConnected(90000)) {
        error = L"Could not reach the Pocket TTS engine host.";
        return false;
    }
    const char payload[2] = {static_cast<char>(quantize ? 1 : 0),
                             static_cast<char>(continuation ? 1 : 0)};
    setRecvTimeout(10 * 60 * 1000);
    if (!sendFrame(CMD_SET_SETTINGS, payload, 2)) {
        disconnect();
        error = L"Connection to the engine host was lost.";
        return false;
    }
    return pumpLongOperation(progress, user, summary, error);
}

bool HostClient::info(std::wstring& out)
{
    CsLock lock(&cs_);
    if (!ensureConnected(90000)) {
        return false;
    }
    setRecvTimeout(15000);
    if (!sendFrame(CMD_INFO, nullptr, 0)) {
        disconnect();
        return false;
    }
    uint32_t type = 0;
    std::vector<char> data;
    if (!readFrame(type, data) || type != RESP_INFO) {
        return false;
    }
    out = PocketTts::utils::string_to_wstring(std::string(data.data(), data.size()));
    return true;
}

void HostClient::shutdownServer()
{
    CsLock lock(&cs_);
    if (sock_ == INVALID_SOCKET && !tryConnectOnce()) {
        return;
    }
    sendFrame(CMD_SHUTDOWN, nullptr, 0);
    uint32_t type = 0;
    std::vector<char> data;
    setRecvTimeout(5000);
    readFrame(type, data);
    disconnect();
}
