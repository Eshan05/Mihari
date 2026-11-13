"""Drive two local HTTP service versions through a fixed-share rollout."""

import argparse
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from math import log
from threading import Thread
from time import perf_counter_ns, sleep

from . import guardrail
from .rollout import GRACE, MIN_CANARY, ONSET, RULES, SCENARIOS, SLOW_MS, Request, make_trace, replay, with_response


DEMO_CUTOFFS = {"threshold": 0.20, "sequential": log(20)}
GUARDRAIL_DEMO_CUTOFF = 0.50


@contextmanager
def service(version: str, responses, *, inject_latency: bool = False):
    responses = iter(responses)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/probe":
                status, latency = 200, 25
            else:
                row = next(responses, None)
                if row is None or self.path != f"/request/{row.block}":
                    self.send_error(400)
                    return
                status, latency = (503 if row.failed else 200), row.latency_ms
                if inject_latency and latency:
                    sleep(latency / 1000)
            self.send_response(status)
            self.send_header("X-Version", version)
            if not inject_latency:
                self.send_header("X-Scripted-Latency-Ms", str(latency))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _message, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join()


def request(port: int, path: str, version: str, *, measure_latency: bool = False) -> tuple[int, int]:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        started = perf_counter_ns() if measure_latency else None
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        if response.getheader("X-Version") != version or response.status not in (200, 503):
            raise RuntimeError(f"Unexpected response from the {version} demo service: {response.status}")
        if started is not None:
            # Round up so crossing 100 ms is still visible at integer resolution.
            elapsed_ms = (perf_counter_ns() - started + 999_999) // 1_000_000
        else:
            elapsed_ms = int(response.getheader("X-Scripted-Latency-Ms"))
        return response.status, elapsed_ms
    finally:
        connection.close()


def scripted_telemetry(trace, profile: str):
    def retime(row):
        if profile == "delayed" and row.canary:
            delay = GRACE
        elif profile == "timeout" and row.canary and row.block == ONSET and row.pair == 0:
            delay = GRACE + 1
        else:
            delay = 0
        return replace(row, available_at=row.block + delay)

    return tuple(tuple(retime(row) for row in window) for window in trace)


def run(scenario: str, rule: str, *, telemetry: str | None = None):
    trace = make_trace(scenario, seed=0, scripted=True)
    if telemetry is not None:
        trace = scripted_telemetry(trace, telemetry)
    control = (row for window in trace for row in window if not row.canary)
    canary = (row for window in trace for row in window if row.canary)
    onset = ONSET if scenario == "delayed-regression" else None
    cutoff = DEMO_CUTOFFS[rule]
    with service("control", control) as control_port, service("canary", canary) as canary_port:
        label = scenario if telemetry is None else f"{scenario} ({telemetry} telemetry)"
        print(f"{label} / {rule}: 90% control at 127.0.0.1:{control_port}, "
              f"10% canary at 127.0.0.1:{canary_port}")
        print("Failures come from HTTP status; latency is scripted and not used for rollback.")
        if telemetry is not None:
            print(f"Telemetry timing is scripted. Windows close when complete or after {GRACE} ticks.")
        elif onset is not None:
            print(f"Fault starts in window {onset}; its first canary outcomes arrive at tick {onset + GRACE}.")

        def dispatch(row):
            port, version = (canary_port, "canary") if row.canary else (control_port, "control")
            return with_response(row, *request(port, f"/request/{row.block}", version))

        for decision in replay(trace, onset, dispatch, complete_early=telemetry is not None):
            look = decision.look
            missing = len(trace[look.block]) - look.control - look.canary
            if missing and telemetry is None:
                raise RuntimeError(f"Incomplete telemetry for demo window {look.block}")
            rollback = decision.rollback(rule, cutoff)
            reason = decision.reason(rule, cutoff)
            if telemetry is not None:
                if missing and min(look.control, look.canary) < MIN_CANARY:
                    reason = f"telemetry deadline, {reason} ({missing} missing)"
                availability = f"lag {look.at - look.block}, missing {missing} | "
            else:
                availability = ""
            print(f"tick {look.at:2d} | window {look.block:2d} | "
                  f"failures control {look.control_failures}/{look.control}, canary {look.canary_failures}/{look.canary} | "
                  f"slow {look.control_slow}/{look.canary_slow} (control/canary) | "
                  f"{availability}{'ROLLBACK' if rollback else 'HOLD'}: {reason} | "
                  f"canary sent {look.sent_canary}, post-onset {look.exposed}")
            if rollback:
                status, _ = request(control_port, "/probe", "control")
                print(f"Canary traffic stopped. Follow-up uses control only: HTTP {status}.")
                return look
        print("No rollback.")
    return None


def guardrail_demo_trace(scenario: str):
    trace = []
    for block in range(ONSET + 3):
        window = []
        for slot in range(MIN_CANARY * 10):
            canary = slot % 10 == 0
            affected = canary and block >= ONSET
            shared = scenario == "shared-degradation" and ONSET <= block < ONSET + 2 and slot < 10
            failed = (shared or (scenario == "error-only" and affected)
                      or (scenario == "timeout" and canary and block == ONSET))
            slow = shared or (scenario == "latency-only" and affected)
            lost = scenario == "timeout" and block == ONSET and slot == 0
            window.append(Request(block, canary, None, failed, 175 if slow else 0,
                                  block + (GRACE + 1 if lost else 0)))
        trace.append(tuple(window))
    return tuple(trace)


def run_guardrail(scenario: str, rule: str):
    trace = guardrail_demo_trace(scenario)
    control = (row for window in trace for row in window if not row.canary)
    canary = (row for window in trace for row in window if row.canary)
    onset = None if scenario == "shared-degradation" else ONSET
    with (service("control", control, inject_latency=True) as control_port,
          service("canary", canary, inject_latency=True) as canary_port):
        print(f"{scenario} / {rule}: 90% control at 127.0.0.1:{control_port}, "
              f"10% canary at 127.0.0.1:{canary_port}")
        print(f"Latency is measured by the HTTP client. Slow means greater than {SLOW_MS} ms.")

        def dispatch(row):
            port, version = (canary_port, "canary") if row.canary else (control_port, "control")
            return with_response(row, *request(port, f"/request/{row.block}", version, measure_latency=True))

        incomplete = 0
        for scores in guardrail.decisions(trace, onset, dispatch):
            look = scores.look
            trigger = scores.trigger(rule, GUARDRAIL_DEMO_CUTOFF)
            missing = len(trace[look.block]) - look.control - look.canary
            if min(look.control, look.canary) < MIN_CANARY:
                incomplete += 1
                decision = f"HOLD: telemetry deadline, too few observed requests ({missing} missing)"
            elif trigger is not None:
                decision = f"ROLLBACK: {trigger}"
            elif scores.errors is None:
                decision = "HOLD: waiting for a second eligible window"
            else:
                decision = "HOLD: no enabled trigger crossed the cutoff"
            print(f"tick {look.at:2d} | window {look.block:2d} | "
                  f"HTTP 5xx control {look.control_failures}/{look.control}, canary {look.canary_failures}/{look.canary} | "
                  f"slow control {look.control_slow}/{look.control}, canary {look.canary_slow}/{look.canary} | "
                  f"{decision} | canary sent {look.sent_canary}, post-onset {look.exposed}")
            if trigger is not None:
                status, _ = request(control_port, "/probe", "control", measure_latency=True)
                print(f"Canary traffic stopped. Follow-up uses control only: HTTP {status}.")
                return scores
        if incomplete:
            print(f"No rollback; evidence was insufficient in {incomplete} window(s).")
        else:
            print("No rollback.")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--rule", choices=RULES, default="threshold")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--telemetry-stress", action="store_true",
                      help="compare prompt, delayed and timed-out outcomes for the same scripted fault")
    mode.add_argument("--joint-guardrail", action="store_true",
                      help="run the error and measured-latency guardrail comparison")
    args = parser.parse_args()
    if args.joint_guardrail:
        if args.scenario != "all" or args.rule != "threshold":
            parser.error("--joint-guardrail uses its own fixed scenarios and rules")
        for scenario, rule in (("latency-only", "errors"), ("latency-only", "joint"),
                               ("error-only", "joint"), ("shared-degradation", "joint"),
                               ("timeout", "joint")):
            run_guardrail(scenario, rule)
        return
    if args.telemetry_stress:
        if args.scenario != "all":
            parser.error("--telemetry-stress uses its own fixed scenario")
        for profile in ("prompt", "delayed", "timeout"):
            run("delayed-regression", args.rule, telemetry=profile)
        return
    for scenario in SCENARIOS if args.scenario == "all" else (args.scenario,):
        run(scenario, args.rule)


if __name__ == "__main__":
    main()
