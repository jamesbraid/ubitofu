# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""A stand-in for unifi-emu-herder, driven by one mode argument.

Speaks the real protocol-1 NDJSON contract over real pipes so the driver's
state machine, pipe draining and signal handling are exercised against a
genuine child process. Every mode below is a shape the real herder can
produce, including the ones that are its known failure modes.
"""
import json
import signal
import sys
import time


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def identities(request: dict) -> list[dict]:
    """Allocate the fields the request left out, as the herder does."""
    out = []
    for index, device in enumerate(request.get("devices", [])):
        model = device["model"]
        mac = device.get("mac") or f"02:00:00:00:00:{index:02x}"
        out.append({
            "index": index,
            "model": model,
            "mac": mac,
            "serial": device.get("serial") or f"EMU{mac.replace(':', '').upper()}",
            "name": device.get("name") or f"emu-{model.lower()}-{index}",
            "ip": f"172.28.0.{index + 4}",
            # An unknown field: consumers must ignore what they don't know.
            "runtime_hint": "unstable-do-not-parse",
        })
    return out


def hold_until_signal(run_id: str, *, deaf: bool = False) -> None:
    """Idle until SIGTERM. `deaf` models a herder that never stops itself."""
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    while not stopping:
        time.sleep(0.02)
    if deaf:
        while True:
            time.sleep(0.05)
    emit({"protocol": 1, "event": "stopped", "run_id": run_id, "reason": "signal"})
    sys.exit(0)


def main() -> None:
    mode = sys.argv[1]
    run_id = "fa4e0001"

    if mode == "mute":
        # Not the herder at all: the likeliest shape of a mistyped binary
        # path. Nothing is ever written to stdout.
        hold_until_signal(run_id)

    emit({"protocol": 1, "event": "started", "run_id": run_id})

    if mode == "crash":
        # The real bug found on 2026-08-01: NewBackend fails, cleanup derefs a
        # nil Backend, so the run dies after `started` with no terminal event
        # and exit 2 -- the status the contract reserves for CLI usage.
        sys.stderr.write("panic: runtime error: invalid memory address\n")
        sys.stderr.flush()
        sys.exit(2)

    if mode == "failed-early":
        emit({
            "protocol": 1, "event": "failed", "run_id": run_id, "phase": "validate",
            "code": "network_not_found", "message": "the requested Docker network does not exist",
            "cleanup_complete": True, "devices": [],
        })
        sys.exit(1)

    if mode == "chatty":
        # Not an event at all — stdout is the protocol and nothing else.
        sys.stdout.write("starting up, please wait\n")
        sys.stdout.flush()
        hold_until_signal(run_id)

    if mode == "protocol2":
        emit({"protocol": 2, "event": "ready", "run_id": run_id, "fleet": []})
        hold_until_signal(run_id)

    if mode == "silent":
        # Started, then nothing: `ready` never arrives and the process never
        # ends on its own.
        hold_until_signal(run_id)

    if mode == "noisy":
        # More than any pipe buffer holds: a driver that drains stderr only
        # after `ready` deadlocks here instead of reaching it.
        for line in range(8000):
            sys.stderr.write(f"[0] device log line {line} " + "x" * 60 + "\n")
        sys.stderr.flush()

    request = json.loads(sys.stdin.read())
    emit({
        "protocol": 1, "event": "ready", "run_id": run_id,
        "devices": identities(request),
        "extra_field": "consumers ignore this",
    })

    if mode == "die-after-ready":
        sys.exit(1)

    hold_until_signal(run_id, deaf=(mode == "deaf"))


if __name__ == "__main__":
    main()
