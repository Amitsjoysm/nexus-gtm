// Gatling STRESS — open-model arrivals stepped past busy hour, to watch how latency degrades.
// The shape is profiles.stress in quality/load/scenarios.yaml; this file only names the profile.
import { buildSimulation } from "./lib/simulation";

export default buildSimulation("stress");
