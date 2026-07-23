# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 James Braid
"""UOS-native (443, unifi-os dialect) key mint — empirical probe + bootstrap.

Task 14 / spec decision tree #2. Manual probe run 2026-07-22 against
ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim, booted by hand with the
documented UOS_RUN_KWARGS contract translated to `docker run` flags
(support.UOS_RUN_KWARGS), port-mapped 127.0.0.1:11443->443 and
127.0.0.1:17443->7443:

    docker run -d --name uos-probe --cgroupns=host \\
      -v /sys/fs/cgroup:/sys/fs/cgroup:rw \\
      --cap-drop ALL --cap-add SYS_ADMIN --cap-add NET_ADMIN \\
      --cap-add NET_RAW --cap-add NET_BIND_SERVICE --cap-add DAC_OVERRIDE \\
      --cap-add DAC_READ_SEARCH --cap-add FOWNER --cap-add CHOWN \\
      --cap-add SETUID --cap-add SETGID --cap-add KILL \\
      --cap-add SYS_CHROOT --cap-add SYS_PTRACE --cap-add SYS_RESOURCE \\
      --cap-add AUDIT_WRITE --cap-add MKNOD \\
      --tmpfs /run:exec --tmpfs /run/lock --tmpfs /tmp:exec \\
      --tmpfs /var/lib/journal --tmpfs /var/opt/unifi/tmp:size=64m \\
      -p 127.0.0.1:11443:443 -p 127.0.0.1:17443:7443 \\
      ghcr.io/jamesbraid/unifi-os-server:5.1.21-sim

Image was already local (no pull needed); `docker inspect -f
'{{.State.Health.Status}}'` went starting -> healthy in ~25s (the 7443
bundled-network-app healthcheck; brief's "1-5 min" budget is for a cold
pull, not this warm-cache run).

--- Request 1: UOS SSO/portal login -----------------------------------

    curl -ksi -X POST https://127.0.0.1:11443/api/auth/login \\
      -H 'Content-Type: application/json' \\
      -d '{"username":"admin","password":"admin"}'

    HTTP/2 403
    content-type: application/json; charset=utf-8
    x-response-time: 4922ms          # first attempt (dbus not yet warm)
    {"message":"Authentication failed, NTP out of sync",
     "code":"AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC","level":"debug"}

Repeated with a wrong password and a nonexistent username: byte-identical
403 body both times, and every attempt takes ~25.1s (25112ms / 25151ms /
25101ms measured across 3 requests) — the response time never varies with
credential correctness, so the NTP gate fires unconditionally, before (or
regardless of) any credential check.

--- Root cause, traced in the compiled app ------------------------------

`/usr/share/unifi-core/app/service.js` (minified): the login path calls
`n8()`, which runs three preflight checks in parallel (default-DNS-server
sanity, a DNS resolution check, a `ping -c5 -W1 ui.com` reachability
check, and an NTP-sync check `bv()`) and throws
AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC when `bv()` is false — regardless of
which login-response code triggered the diagnostic (the `default:` arm of
the switch that dispatches on the upstream login-call's response code
calls `n8()` unconditionally, same as every named failure code). `bv()`
is:

    timedatectl show --value --property=NTPSynchronized --property=NTP

Confirmed by hand:

    $ time docker exec uos-probe timedatectl show --value \\
        --property=NTPSynchronized --property=NTP
    Failed to parse bus message: Connection timed out
    real  25.060s

25.06s — matches the login endpoint's response time exactly. `timedatectl`
talks to `systemd-timedated` over D-Bus; that service is down:

    $ docker exec uos-probe systemctl status systemd-timedated
    ● systemd-timedated.service - Time & Date Service
         Active: failed (Result: exit-code)
        Process: 2658 ExecStart=/lib/systemd/systemd-timedated
                 (code=exited, status=226/NAMESPACE)

226/NAMESPACE is systemd's own exit code for "failed to set up the
service's mount/namespace sandbox" — this unit ships with
`ProtectSystem=strict`, `ProtectHome=yes`, `RestrictNamespaces=yes`,
`ProtectControlGroups=yes`, etc. (`/lib/systemd/system/
systemd-timedated.service`, read in full during the probe), and those
directives cannot be satisfied inside this container even with the full
documented UOS_RUN_KWARGS cap_add list and `cgroupns=host`. A manual
`systemctl restart systemd-timedated` reproduces the identical 226 exit —
deterministic, not a boot race. `systemd-journald` fails the same way
(218/CAPABILITIES), which is why `journalctl` reports "No journal files
were found" throughout — a second, independent symptom of the same
"real systemd PID 1 inside a capability-limited container" ceiling.

No bypass exists: grepping the compiled bundle for every
`process.env.*` reference turns up exactly one (`TEST_ENV`, unrelated);
there is no skip-NTP-check flag, header, or alternate local-only login
route. The image's own sim-mode hook
(`/usr/local/uos/init.d/demo-mode`) only flips `is_simulation=true` for
the bundled Network Application (backing port 7443, exposed via the
`UOS_NETWORK_DIRECT=true` env var already baked into this image — see
`uos-network-direct.socket`, "Direct (SSO-free) UniFi Network API port")
— it never touches the UOS SSO/portal layer that gates port 443. The
-sim tag was never built to make 443 headless-testable.

--- Requests 2 and 3: recorded for completeness, no session available --

Since step 1 never yields a session, these were run with placeholder
cookies/keys purely to confirm the routes exist and reject cleanly (no
crash, no silent 200):

    curl -ksi https://127.0.0.1:11443/api/users/self -b 'TOKEN=dummy'
    HTTP/2 401  ->  "Unauthorized" (text/plain)

    curl -ksi -X POST https://127.0.0.1:11443/api/users/self/api-keys \\
      -b 'TOKEN=dummy' -H 'x-csrf-token: dummy' \\
      -H 'Content-Type: application/json' -d '{"name":"controllertest"}'
    HTTP/2 404  ->  "Not Found" (text/plain; pre-auth, so this alone
                     doesn't prove the route is absent post-auth)

    curl -ksi https://127.0.0.1:11443/proxy/network/api/s/default/rest/networkconf \\
      -H 'X-API-KEY: bogus-key-000'   # gitleaks:allow — deliberate bogus probe value
    HTTP/2 401
    {"meta":{"rc":"error","msg":"api.err.LoginRequired"},"data":[]}

That last one is the useful negative control: `/proxy/network/...` (the
production unifi-os dialect ubitofu.Controller._resolve() builds) is
live and dialect-correct — it recognizes X-API-KEY and rejects a bad one
cleanly. The blocker is strictly upstream of it: minting a real key
requires the SSO login that cannot complete headlessly.

--- Conclusion -----------------------------------------------------------

No mint endpoint is reachable without a completed SSO login, and that
login cannot complete under the documented container capability contract
(spec decision tree #2, negative branch). `native_api_key()` below
therefore returns None against this image (the login 401/403 case only);
S11 in test_scenarios_uos.py xfails with the spec's stated fallback: bake
a pre-minted key into the -sim image (a unifi-containers change, out of
scope for ubitofu). Any other failure mode (transport error, unexpected
status, unparseable body) raises instead — the None result is reserved
for exactly this documented condition, so a real regression can't be
misread as the known gap. `native_api_key()` is still implemented
against the real endpoints (not stubbed to `None` outright) so it starts
working the moment that image-side fix lands, with no ubitofu-side
change needed.
"""
import httpx

# The exact "code" field the probe observed (see "Request 1" above) — the
# NTP-sync preflight failure, byte-identical across a right password, a
# wrong password, and a nonexistent username. This is the ONLY body shape
# native_api_key treats as the documented gap; any other code (or no code
# at all) on a 401/403 means something else rejected the login — most
# plausibly a real credential failure, which must never be misread as the
# known NTP limitation.
_NTP_GATE_CODE = "AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC"


def native_api_key(native_url: str, username: str, password: str) -> str | None:
    """SSO login + API-key mint against the UOS native (443) endpoint.

    None means EXACTLY one thing: the documented limitation from the probe
    above — UOS SSO/portal login rejects the attempt with 401/403 and a
    body whose "code" is AUTHENTICATION_FAILED_NTP_OUT_OF_SYNC, because the
    NTP-sync preflight can never pass under the container capability
    contract (systemd-timedated exits 226/NAMESPACE; see "Root cause,
    traced in the compiled app" above). A 401/403 with any other code (or
    an unparseable body) raises instead of collapsing into None — a future
    image fix landing the NTP gate closed while credentials are still
    wrong (or any other rejection reason) must surface as a real failure,
    not be silently read as "the known gap".
    """
    if not native_url:
        return None
    # Login is gated on a ~25s D-Bus timeout in the current -sim image
    # (see the probe above) even when it fails; give it room either way.
    with httpx.Client(base_url=native_url, verify=False, timeout=40.0) as client:
        try:
            resp = client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
        except httpx.TransportError as exc:
            # Connection/TLS/DNS failure is infra breakage, not the
            # documented limitation — surface it loudly.
            raise RuntimeError(f"UOS native login transport failure: {exc}") from exc

        if resp.status_code in (401, 403):
            try:
                code = resp.json().get("code")
            except ValueError as exc:
                # Unparseable body: unknown territory, not the documented
                # gap's well-formed JSON — raise rather than guess.
                raise RuntimeError(
                    f"UOS native login: {resp.status_code} with unparseable "
                    f"body: {resp.text[:500]!r}"
                ) from exc
            if code == _NTP_GATE_CODE:
                # The documented condition (probe: "Request 1" above).
                return None
            raise RuntimeError(
                f"UOS native login: {resp.status_code} with code {code!r}, "
                f"expected {_NTP_GATE_CODE!r} (the documented NTP gate) — "
                f"body: {resp.text[:500]!r}"
            )

        if resp.status_code != 200:
            # Unknown territory: neither the documented NTP-gate rejection
            # nor a clean success. A future image fix or route change must
            # fail loudly here, not be silently read as the known gap.
            raise RuntimeError(
                f"UOS native login: unexpected status {resp.status_code}, "
                f"body: {resp.text[:500]!r}"
            )

        # UNVERIFIED against a real 2xx: no image has ever reached this
        # point (see "Conclusion" above — login cannot complete under the
        # documented container capability contract). This success path is
        # shaped from the -sim image's 4xx bodies (Requests 2/3) and
        # general UniFi API convention (bare `key`, or `data.key`), not
        # from an observed success. Revisit once an image-side fix (a
        # pre-minted key baked into the -sim image, or a real NTP fix)
        # lets this actually run.
        csrf = resp.headers.get("x-csrf-token", "")
        headers = {"x-csrf-token": csrf} if csrf else {}
        mint = client.post(
            "/api/users/self/api-keys", json={"name": "controllertest"}, headers=headers
        )
        if mint.status_code >= 400:
            raise RuntimeError(
                f"UOS native key mint: status {mint.status_code}, "
                f"body: {mint.text[:500]!r}"
            )
        try:
            body = mint.json()
        except ValueError as exc:
            raise RuntimeError(
                f"UOS native key mint: unparseable body: {mint.text[:500]!r}"
            ) from exc
        key = body.get("key") or body.get("data", {}).get("key")
        if not key:
            raise RuntimeError(
                f"UOS native key mint: 2xx response with no recognizable "
                f"key field: {body!r}"
            )
        return str(key)
