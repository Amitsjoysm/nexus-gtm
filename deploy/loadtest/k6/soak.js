// k6 SOAK — modest constant load held long enough to surface leaks, drift and slow degradation a
// short run never sees. Access tokens live 1 h, so a soak past auth.refresh_after_s re-logs in per
// VU at staggered offsets (lib/auth.js) instead of 401ing at minute 60. Watch Grafana alongside:
// RSS and DB connections should stay flat and p95 must not creep.
import { configure } from "./lib/runtime.js";

const runtime = configure("soak");

export const options = runtime.options;
export const setup = runtime.setup;
export const journey = runtime.journey;
export const paidJourney = runtime.paidJourney;
