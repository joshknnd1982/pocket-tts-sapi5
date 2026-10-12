#pragma once

// Wire protocol between the SAPI engine DLLs / Voice Manager and the
// Pocket TTS host process (host/pockettts_host.py). Framed TCP on
// 127.0.0.1: every frame is <uint32 type><uint32 payload_size><payload>,
// little-endian; strings inside payloads are UTF-8 and length-prefixed.

#include <stdint.h>

enum HostCommand : uint32_t {
    CMD_PING = 0,
    CMD_LIST_VOICES = 1,
    CMD_SPEAK = 2,          // <u16 voice_len><voice utf8><u32 text_len><text utf8>[<u8 flags>]
    CMD_STOP = 3,
    CMD_CLONE = 4,          // <u16+name><u16+audio_path><u16+language_hex><u8 gender>
    CMD_SET_PUBLISHED = 5,  // <u16+name><u8 published>
    CMD_DELETE_VOICE = 6,   // <u16+name>
    CMD_UPDATE_MODELS = 7,  // <u16+hf_token (may be empty)>
    CMD_SHUTDOWN = 8,
    CMD_INFO = 9,
    // Voice packages (.pttsvoices), for sharing voices between computers.
    // An empty name list means "every voice".
    CMD_EXPORT_VOICES = 10,   // <u16+dest_path><u8 include_sources><u32 count>{<u16+name>}
    CMD_IMPORT_VOICES = 11,   // <u16+package_path><u8 publish><u8 collision><u32 count>{<u16+name>}
    CMD_INSPECT_PACKAGE = 12, // <u16+package_path>
};

// CMD_SPEAK flags. The byte is optional: hosts before 1.2.2 ignore it, and
// a request without it has no flags set.
enum HostSpeakFlags : uint8_t {
    SPEAK_SPELL = 1,        // name each character (SAPI's SpellOut action)
};

// What an import does with a voice whose name is already in use.
enum HostCollisionMode : uint8_t {
    COLLISION_SKIP = 0,
    COLLISION_RENAME = 1,
    COLLISION_REPLACE = 2,
};

enum HostResponse : uint32_t {
    RESP_OK = 0,          // payload: utf8 summary for long operations, else empty
    RESP_ERROR = 1,       // payload: utf8 message
    RESP_AUDIO = 2,       // payload: raw PCM, 16-bit mono 24000 Hz
    RESP_AUDIO_END = 3,   // <f32 speed_factor> (see below; absent on old hosts)
    RESP_VOICES = 4,      // <u32 count>{<u16+name><u8 female><u32 lcid><u8 published><u8 has_src>}
    RESP_PONG = 5,        // <f32 speed_factor> (absent on old hosts)
    RESP_PROGRESS = 6,    // payload: utf8 message
    RESP_INFO = 7,        // payload: utf8 message
    RESP_PACKAGE = 8,     // <u16+summary><u32 count>{<u16+name><u8 female><u8 has_src><u8 status>}
};

// Per-voice status in a RESP_PACKAGE reply.
enum HostPackageStatus : uint8_t {
    PACKAGE_READY = 0,        // made with this computer's model
    PACKAGE_WILL_REBUILD = 1, // other model, but the audio sample is included
    PACKAGE_NO_SAMPLE = 2,    // other model and no audio sample
};

// Host candidate ports, tried in order; the active one is written to
// %LOCALAPPDATA%\PocketTTS\host.port.
constexpr unsigned short POCKETTTS_PORTS[] = {17853, 17854, 17855, 17856, 17857};
constexpr int POCKETTTS_PORT_COUNT = 5;

// How much audio the host machine generates per second of work: above 1.0
// means faster than realtime. Reported by the host in PONG and AUDIO_END,
// 0.0 while it has nothing to report. The SAPI engine banks audio before
// starting playback when this is low, so that speech does not break up on
// machines that cannot generate faster than they play.
constexpr float POCKETTTS_SPEED_UNKNOWN = 0.0f;

constexpr unsigned long POCKETTTS_SAMPLE_RATE = 24000;
constexpr unsigned short POCKETTTS_CHANNELS = 1;
constexpr unsigned short POCKETTTS_BITS = 16;
