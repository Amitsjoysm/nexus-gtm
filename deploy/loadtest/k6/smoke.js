// k6 SMOKE — one pass of every catalogue journey, no think time. The cheapest proof that the
// catalogue still matches the API. Run through scripts/load/run.py (caps, window, data, results);
// see docs/quality/load.md for a direct `docker run grafana/k6` invocation.
import { configure } from "./lib/runtime.js";

const runtime = configure("smoke");

export const options = runtime.options;
export const setup = runtime.setup;
export const journey = runtime.journey;
export const paidJourney = runtime.paidJourney;
