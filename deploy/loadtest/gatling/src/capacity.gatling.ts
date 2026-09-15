// Gatling CAPACITY — closed-model concurrency stairs, to read sustainable throughput per level.
// The shape is profiles.capacity in quality/load/scenarios.yaml; this file only names the profile.
import { buildSimulation } from "./lib/simulation";

export default buildSimulation("capacity");
