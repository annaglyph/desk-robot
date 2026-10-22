#include "ws_probe.h"

#include "config.h"

WsProbe wsProbe;

extern "C" void ws_probe_wait(uint32_t us) {
#if LOOP_TIMING
  wsProbe.waitUs += us;
  wsProbe.waits++;
#else
  (void)us;
#endif
}

extern "C" void ws_probe_tcp(uint32_t us, uint32_t n) {
#if LOOP_TIMING
  wsProbe.tcpUs += us;
  wsProbe.tcpBytes += n;
#else
  (void)us;
  (void)n;
#endif
}
