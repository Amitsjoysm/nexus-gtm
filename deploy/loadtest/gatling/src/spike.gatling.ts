// Gatling SPIKE — a sudden burst of arrivals and the recovery after it.
// The shape is profiles.spike in quality/load/scenarios.yaml; this file only names the profile.
import { buildSimulation } from "./lib/simulation";

export default buildSimulation("spike");
