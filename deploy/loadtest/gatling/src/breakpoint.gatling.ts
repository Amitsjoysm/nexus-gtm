// Gatling BREAKPOINT — arrival-rate stairs until a guard trips or the environment cap is reached.
// A guard stopping this run is the result (the level it stopped at is the knee), not a failure.
// The shape is profiles.breakpoint in quality/load/scenarios.yaml; this file only names the profile.
import { buildSimulation } from "./lib/simulation";

export default buildSimulation("breakpoint");
