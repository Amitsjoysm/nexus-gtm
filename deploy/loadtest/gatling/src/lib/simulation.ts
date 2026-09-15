// Builds a Gatling simulation for one profile from catalogue + plan + SLO export.
//
// Assertions come from the SLO export, never from this file:
//   global p95 / p99 response time, global failed-request percentage  <- slo.api
//   per journey (group) p95                                            <- slo.api.p95_ms
//   per route overrides                                                <- slo.routes
// For stress, spike, breakpoint and capacity a failed assertion is a FINDING, not a broken run —
// scripts/load/run.py records the verdict and only gates k6 profiles on it.
import {
  Assertion,
  ClosedInjectionStep,
  OpenInjectionStep,
  PopulationBuilder,
  atOnceUsers,
  constantConcurrentUsers,
  constantUsersPerSec,
  details,
  global,
  group,
  incrementConcurrentUsers,
  incrementUsersPerSec,
  nothingFor,
  rampUsersPerSec,
  repeat,
  scenario,
  simulation,
} from "@gatling.io/core";
import { http } from "@gatling.io/http";

import { ClosedStep, OpenStep, Runtime, loadRuntime, log, routeTag } from "./config";
import { bootstrapScenario, journeyChain } from "./journey";

function openStep(step: OpenStep): OpenInjectionStep {
  switch (step.type) {
    case "nothing":
      return nothingFor(step.duration_s!);
    case "constant":
      return constantUsersPerSec(step.rate!).during(step.duration_s!);
    case "ramp":
      return rampUsersPerSec(step.from!).to(step.to!).during(step.duration_s!);
    case "stairs":
      return incrementUsersPerSec(step.increment!)
        .times(step.levels!)
        .eachLevelLasting(step.level_s!)
        .separatedByRampsLasting(step.ramp_s!)
        .startingFrom(step.start!);
  }
}

function closedStep(step: ClosedStep): ClosedInjectionStep {
  if (step.increment === 0) {
    const total = step.levels * step.level_s + (step.levels - 1) * step.ramp_s;
    return constantConcurrentUsers(step.start).during(total);
  }
  return incrementConcurrentUsers(step.increment)
    .times(step.levels)
    .eachLevelLasting(step.level_s)
    .separatedByRampsLasting(step.ramp_s)
    .startingFrom(step.start);
}

function assertions(rt: Runtime): Assertion[] {
  const api = rt.slo.api;
  const out: Assertion[] = [
    global().responseTime().percentile(95).lt(api.p95_ms),
    global().responseTime().percentile(99).lt(api.p99_ms),
    global().failedRequests().percent().lt(api.error_rate_pct),
  ];
  // Per journey, per request. NOT details(<group>): a Gatling group's response time is the
  // cumulated time of the whole group, so a 10-request journey would "breach" a request-level SLO
  // by construction. k6 grades the journey's requests together; here each request type in the
  // journey must meet it, which is the stricter reading.
  for (const id of Object.keys(rt.plan.journeys)) {
    const names = new Set(rt.catalogue.journeys[id].steps.flatMap((s) => (s.requests || []).map((r) => r.name)));
    for (const name of names) out.push(details(id, name).responseTime().percentile(95).lt(api.p95_ms));
  }
  for (const [route, override] of Object.entries(rt.slo.routes || {})) {
    const name = routeTag(route);
    for (const id of Object.keys(rt.plan.journeys)) {
      const uses = rt.catalogue.journeys[id].steps.some((s) => (s.requests || []).some((r) => r.name === name));
      if (!uses) continue;
      if (override.p95_ms !== undefined) out.push(details(id, name).responseTime().percentile(95).lt(override.p95_ms));
      if (override.p99_ms !== undefined) out.push(details(id, name).responseTime().percentile(99).lt(override.p99_ms));
      if (override.error_rate_pct !== undefined) out.push(details(id, name).failedRequests().percent().lt(override.error_rate_pct));
    }
  }
  return out;
}

export function buildSimulation(profile: string) {
  return simulation((setUp) => {
    const rt = loadRuntime(profile);
    log(`LT_RUN ${rt.runId} profile=${profile} env=${rt.envName} slo=${rt.slo.profile}${rt.slo.is_fixture ? " (fixture)" : ""}`);

    const protocol = http
      .baseUrl(rt.baseUrl)
      .acceptHeader("application/json")
      .header(rt.catalogue.run_header, rt.runId)
      .userAgentHeader(`nexus-loadtest/gatling ${rt.runId}`)
      // The default warm-up request goes to gatling.io; a load test sends traffic to its target only.
      .disableWarmUp();

    const populations: PopulationBuilder[] = [];
    for (const [id, spec] of Object.entries(rt.plan.journeys)) {
      const journey = rt.catalogue.journeys[id];
      const scn = scenario(id).exec(group(id).on(journeyChain(rt, journey)));
      if (spec.open) populations.push(scn.injectOpen(...spec.open.map(openStep)));
      else if (spec.closed) populations.push(scn.injectClosed(...spec.closed.map(closedStep)));
    }
    for (const [id, paid] of Object.entries(rt.plan.paid || {})) {
      const journey = rt.catalogue.paid_journeys[id];
      const budget = { key: `lt:paid:${id}`, max: paid.max_paid_calls };
      const scn = scenario(`paid_${id}`).exec(repeat(paid.iterations).on(group(id).on(journeyChain(rt, journey, budget))));
      populations.push(scn.injectOpen(atOnceUsers(1)));
    }

    setUp(bootstrapScenario(rt).injectOpen(atOnceUsers(1)).andThen(...populations))
      .protocols(protocol)
      .assertions(...assertions(rt))
      .maxDuration(rt.plan.max_duration_s);
  });
}
