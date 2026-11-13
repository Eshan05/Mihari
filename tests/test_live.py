from collections import Counter
from dataclasses import replace

import pytest

from mihari import live
from mihari.rollout import GRACE, ONSET, RULES, SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("rule", RULES)
def test_real_http_demo(scenario, rule, monkeypatch, capsys):
    requests = []
    original = live.request

    def request(port, path, version):
        status, latency = original(port, path, version)
        requests.append((port, path, version, status))
        return status, latency

    monkeypatch.setattr(live, "request", request)
    stop = live.run(scenario, rule)
    output = capsys.readouterr().out
    ports = {version: port for port, _, version, _ in requests}
    assert set(ports) == {"control", "canary"} and len(set(ports.values())) == 2
    sent = Counter(version for _, path, version, _ in requests if path != "/probe")
    failures = Counter(version for _, _, version, status in requests if status == 503)
    assert sent["control"] == 9 * sent["canary"]
    assert "90% control" in output and "10% canary" in output
    if scenario == "delayed-regression":
        assert failures["canary"] and not failures["control"]
        assert stop.at == ONSET + GRACE + 1
        assert stop.exposed == 16
        assert "first canary outcomes arrive at tick 6" in output
        assert "ROLLBACK:" in output and "post-onset 16" in output
        assert requests[-1][1:] == ("/probe", "control", 200)
    else:
        assert stop is None and "No rollback." in output
        assert failures["control"] == 9 * failures["canary"]
        assert bool(failures) is (scenario == "shared-outage")
    for version, port in ports.items():
        with pytest.raises(OSError):
            original(port, "/probe", version)


def test_incomplete_telemetry_cannot_pass(monkeypatch, capsys):
    original = live.with_response

    def with_response(row, status, latency):
        return replace(original(row, status, latency), available_at=None if row.canary else row.available_at)

    monkeypatch.setattr(live, "with_response", with_response)
    with pytest.raises(RuntimeError, match="Incomplete telemetry"):
        live.run("healthy", "threshold")
    assert "No rollback." not in capsys.readouterr().out


def test_second_listener_failure_closes_first(monkeypatch):
    original = live.HTTPServer
    ports = []

    def server(*args):
        if ports:
            raise OSError("port unavailable")
        started = original(*args)
        ports.append(started.server_port)
        return started

    monkeypatch.setattr(live, "HTTPServer", server)
    with pytest.raises(OSError, match="port unavailable"):
        live.run("healthy", "threshold")
    with pytest.raises(OSError):
        live.request(ports[0], "/probe", "control")


@pytest.mark.parametrize("rule", RULES)
def test_live_stress_rolls_back_earlier_with_prompt_telemetry(rule, monkeypatch, capsys):
    original = live.request
    requests = []

    def request(port, path, version):
        status, latency = original(port, path, version)
        requests.append((port, path, version, status))
        return status, latency

    monkeypatch.setattr(live, "request", request)
    seen_rollbacks = []
    sequences = []
    for profile in ("prompt", "delayed", "timeout"):
        stop = live.run("delayed-regression", rule, telemetry=profile)
        seen_rollbacks.append((stop.at, stop.exposed))
        output = capsys.readouterr().out
        assert f"({profile} telemetry)" in output
        assert requests[-1][1:] == ("/probe", "control", 200)
        ports = {version: port for port, _, version, _ in requests}
        assert len(set(ports.values())) == 2
        for version, port in ports.items():
            with pytest.raises(OSError):
                original(port, "/probe", version)
        sequences.append([entry[1:] for entry in requests[:-1]])
        requests.clear()
        if profile == "timeout":
            line, = (line for line in output.splitlines() if "HOLD: telemetry deadline" in line)
            assert "canary 3/3" in line and "lag 2, missing 1" in line
    assert seen_rollbacks == [(ONSET + 1, 8), (ONSET + GRACE + 1, 16), (ONSET + GRACE, 12)]
    assert sequences[0] == sequences[1][:len(sequences[0])] == sequences[2][:len(sequences[0])]
