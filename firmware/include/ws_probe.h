#pragma once

#include <stdint.h>

// Filled by the WebSocket library's synchronous payload read (readCb) and by
// Link::onEvent. Present in every build so the library can call it; the
// counters only move when LOOP_TIMING is on.
struct WsProbe {
  uint32_t waitUs = 0;    // spinning inside readCb until bytes are available
  uint32_t tcpUs = 0;     // time spent inside the TCP read itself
  uint32_t tcpBytes = 0;  // bytes returned by those reads
  uint32_t cbUs = 0;      // time inside the WebSocket event callback
  uint32_t waits = 0;

  void reset() {
    waitUs = 0;
    tcpUs = 0;
    tcpBytes = 0;
    cbUs = 0;
    waits = 0;
  }
};

extern WsProbe wsProbe;

extern "C" void ws_probe_wait(uint32_t us);
extern "C" void ws_probe_tcp(uint32_t us, uint32_t n);
