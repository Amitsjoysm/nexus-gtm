// k6 LOAD — the per-release regression gate: ramp every catalogue journey to busy-hour concurrency
// by weight, hold, and fail the run when the SLO export's thresholds break. The shape lives in
// quality/load/scenarios.yaml (profiles.load); this file only names the profile.
import { configure } from "./lib/runtime.js";

const runtime = configure("load");

export const options = runtime.options;
export const setup = runtime.setup;
export const journey = runtime.journey;
export const paidJourney = runtime.paidJourney;
