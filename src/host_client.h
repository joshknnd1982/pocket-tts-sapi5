#pragma once

// TCP client for the Pocket TTS host process. Used by both SAPI engine
// DLLs (x86 and x64) and by the Voice Manager. Launches the host on
// demand and keeps one connection per HostClient instance.

#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include <string>
#include <vector>

#include "host_protocol.h"

struct HostVoice {
    std::wstring name;
    bool female = false;
    uint32_t lcid = 0x409;
    bool published = false;
    bool hasSource = false;
    std::wstring model;     // id of the AI model the voice belongs to
};

// One AI model the host knows, installed or not.
struct HostModel {
    std::wstring id;
    std::wstring title;
    std::wstring description;
    std::wstring loadError;  // why it failed to load, if it did
    bool installed = false;
    bool loaded = false;
    uint32_t sizeMb = 0;
    uint32_t lcid = 0x409;
    uint32_t voices = 0;     // voices that use it
};

// One voice inside a .pttsvoices package, as reported by inspectPackage.
struct PackageVoice {
    std::wstring name;
    bool female = false;
    bool hasSource = false;
    uint8_t status = PACKAGE_READY;
};

struct PackageInfo {
    std::wstring summary;   // human-readable description of the package
    std::vector<PackageVoice> voices;
};

class HostClient {
public:
    // Returns false to stop the utterance (abort).
    using AudioCallback = bool(*)(const char* pcm, uint32_t size, void* user);
    // Progress text for long operations (clone, model update, export, import).
    using ProgressCallback = void(*)(const std::wstring& message, void* user);

    HostClient();
    ~HostClient();

    HostClient(const HostClient&) = delete;
    HostClient& operator=(const HostClient&) = delete;

    // Connects, starting the host process if necessary. Blocks up to
    // `timeoutMs` while a cold host loads the model.
    bool connect(DWORD timeoutMs = 90000);
    bool isConnected() const { return sock_ != INVALID_SOCKET; }

    // How much audio the host machine generates per second of work, as
    // last reported by the host: above 1.0 is faster than realtime,
    // POCKETTTS_SPEED_UNKNOWN while the host has not measured anything.
    float speedFactor() const { return speedFactor_; }

    // `spell` asks the host to name each character instead of reading the
    // text as words.
    bool speak(const std::string& voiceUtf8, const std::string& textUtf8,
               AudioCallback callback, void* user, bool spell = false);

    bool listVoices(std::vector<HostVoice>& out, std::wstring& error);
    // An empty `modelId` means the standard English model.
    bool cloneVoice(const std::wstring& name, const std::wstring& audioPath,
                    bool female, const std::wstring& languageHex,
                    ProgressCallback progress, void* user, std::wstring& error,
                    const std::wstring& modelId = std::wstring());
    bool setPublished(const std::wstring& name, bool published, std::wstring& error);
    bool deleteVoice(const std::wstring& name, std::wstring& error);
    // Downloads or updates one model (empty: the standard English model).
    bool updateModels(const std::wstring& hfToken,
                      ProgressCallback progress, void* user, std::wstring& error,
                      const std::wstring& modelId = std::wstring());

    // AI models and engine settings.
    bool listModels(std::vector<HostModel>& out, std::wstring& error);
    bool removeModel(const std::wstring& modelId, std::wstring& summary,
                     std::wstring& error);
    // Rebuilds the voice for another model; with a non-empty `copyName` the
    // voice is kept as it is and a new voice of that name is made instead.
    bool setVoiceModel(const std::wstring& name, const std::wstring& modelId,
                       const std::wstring& copyName, ProgressCallback progress,
                       void* user, std::wstring& summary, std::wstring& error);
    bool getSettings(bool& quantize, bool& continuation, std::wstring& error);
    bool setSettings(bool quantize, bool continuation, ProgressCallback progress,
                     void* user, std::wstring& summary, std::wstring& error);

    // Voice packages. An empty `names` means every voice in the store, or
    // every voice in the package.
    bool exportVoices(const std::vector<std::wstring>& names,
                      const std::wstring& destPath, bool includeSources,
                      ProgressCallback progress, void* user,
                      std::wstring& summary, std::wstring& error);
    bool inspectPackage(const std::wstring& packagePath, PackageInfo& out,
                        std::wstring& error);
    bool importVoices(const std::wstring& packagePath,
                      const std::vector<std::wstring>& names, bool publish,
                      uint8_t collision, ProgressCallback progress, void* user,
                      std::wstring& summary, std::wstring& error);

    bool info(std::wstring& out);
    void shutdownServer();

    void disconnect();

private:
    bool ensureConnected(DWORD timeoutMs);
    bool tryConnectOnce();
    bool tryPort(unsigned short port);
    bool launchHost();
    bool sendFrame(uint32_t type, const void* data, uint32_t size);
    bool readFrame(uint32_t& type, std::vector<char>& payload);
    // Drains RESP_PROGRESS frames until the operation ends; on success the
    // RESP_OK payload is returned in `summary`.
    bool pumpLongOperation(ProgressCallback progress, void* user,
                           std::wstring& summary, std::wstring& error);
    void setRecvTimeout(DWORD ms);

    void noteSpeedFactor(const std::vector<char>& payload);

    SOCKET sock_;
    CRITICAL_SECTION cs_;
    bool wsaReady_;
    volatile float speedFactor_;
};
