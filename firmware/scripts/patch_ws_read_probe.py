# Times the WebSocket library's synchronous payload read.
# The probe lives in the installed library, which PlatformIO can replace, so
# each build reapplies this patch. Removing the script leaves the library
# untouched on the next clean install.

Import("env")

from pathlib import Path

MARKER = "ws_probe_wait"
TARGET = (
    Path(env["PROJECT_LIBDEPS_DIR"])
    / env["PIOENV"]
    / "WebSockets"
    / "src"
    / "WebSockets.cpp"
)

OLD_DECL = '#include "WebSockets.h"\n'
NEW_DECL = '''#include "WebSockets.h"

// desk-robot: time spent waiting for a payload versus reading it.
// Defined in src/ws_probe.cpp. No-op unless LOOP_TIMING is on.
extern "C" void ws_probe_wait(uint32_t us);
extern "C" void ws_probe_tcp(uint32_t us, uint32_t n);

'''

OLD_LOOP = """        if(!client->tcp->available()) {
            WEBSOCKETS_YIELD_MORE();
            continue;
        }

        len = client->tcp->read((uint8_t *)out, n);
        if(len > 0) {
            t = millis();
            out += len;
            n -= len;
            // DEBUG_WEBSOCKETS("Receive %d left %d!\\n", len, n);
        } else {
            // DEBUG_WEBSOCKETS("Receive %d left %d!\\n", len, n);
        }
        if(n > 0) {
            WEBSOCKETS_YIELD();
        }
"""

NEW_LOOP = """        if(!client->tcp->available()) {
            uint32_t _wsT0 = micros();
            WEBSOCKETS_YIELD_MORE();
            ws_probe_wait(static_cast<uint32_t>(micros() - _wsT0));
            continue;
        }

        uint32_t _wsT0 = micros();
        len = client->tcp->read((uint8_t *)out, n);
        ws_probe_tcp(static_cast<uint32_t>(micros() - _wsT0),
                     len > 0 ? static_cast<uint32_t>(len) : 0);
        if(len > 0) {
            t = millis();
            out += len;
            n -= len;
            // DEBUG_WEBSOCKETS("Receive %d left %d!\\n", len, n);
        } else {
            // DEBUG_WEBSOCKETS("Receive %d left %d!\\n", len, n);
        }
        if(n > 0) {
            uint32_t _wsT1 = micros();
            WEBSOCKETS_YIELD();
            ws_probe_wait(static_cast<uint32_t>(micros() - _wsT1));
        }
"""


def patch() -> None:
    if not TARGET.is_file():
        print(f"ws probe: {TARGET} not installed yet; skipping")
        return
    text = TARGET.read_text()
    if MARKER in text:
        return
    if OLD_DECL not in text or OLD_LOOP not in text:
        raise SystemExit(f"ws probe: {TARGET} does not match the expected readCb")
    TARGET.write_text(text.replace(OLD_DECL, NEW_DECL, 1).replace(OLD_LOOP, NEW_LOOP, 1))
    print(f"ws probe: timed readCb in {TARGET}")


patch()
