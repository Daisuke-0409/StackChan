"""Answers "where is so-and-so's grave?" without letting the ledger leave the office.

The CRM holds real customer records -- names, addresses, the deceased, photos
of the work -- and its own documentation says it must not be exposed as it
stands. The gateway needs three fields out of it: who, which cemetery, which
district. This process is the seam between those two facts.

    home gateway --(Tailscale)--> this, on the office PC --(localhost)--> CRM

It is deliberately thin, in the same way forwarder.py is thin. It decides
nothing about what the robot says: no phrasing, no "is this a CRM question",
no memory rules. Those live in the gateway, because *this process is the part
that goes away*. The CRM is moving to the NAS, and on that day the gateway
will reach it directly and this file gets deleted. Anything clever written
here would have to be written again over there.

What it does do is narrow. It asks the CRM, then copies out only the fields
the robot can actually say aloud and drops the rest on the floor. The project
keeps secrets from the model by not handing them over rather than by asking
it not to repeat them; the same reasoning applies one layer out. A reply that
never crossed the tailnet cannot be leaked by anything downstream, however
badly the gateway is written.

This needs the CRM to ship /api/tachikoma/lookup first, and there is no way
around that. The CRM used to admit callers on localhost without a login, and
an earlier plan leaned on it; that bypass went when logins became per-person
in August, and require_auth() now answers 401 to every /api/ path whoever is
asking. The remaining ways in would be to hold somebody's PIN, which puts a
person's credential in a robot and signs their name to every lookup, or to
reopen the bypass, which removes the "who asked" the CRM treats as its
foundation. Neither is worth a grave location. So: the endpoint, with a
token of its own -- shipped by the CRM on 2026-08-19 (commit 5c681d2) and
in production since.

    $env:CRM_RELAY_TOKEN = "..."          # shared with the gateway
    $env:CRM_BASE_URL    = "http://127.0.0.1:8765"
    python -u gateway/crm_relay.py

The office PC has to be running for any of this to answer, so grave lookups
work during office hours and not outside them. That is the same property the
office body already has, and it disappears when the CRM reaches the NAS.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Where the CRM lives. Today that is this same PC; when it moves to the NAS
# this one value changes and nothing else here does. That is the whole point
# of routing through a setting rather than assuming co-location -- being on
# the same machine buys us nothing any more, now that the localhost bypass
# is gone, so there is no reason to depend on it.
CRM_BASE_URL = os.environ.get("CRM_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

# Sent to the CRM as X-Tachikoma-Token. Required in practice: every /api/
# path answers 401 without it. Empty only while the CRM has yet to ship the
# endpoint, when nothing works anyway.
CRM_TOKEN = os.environ.get("CRM_TOKEN", "")

# Required. This process listens on the tailnet, so an unauthenticated one
# would put the customer ledger a single request away from anything that can
# reach the office PC. Refuse to start rather than come up open.
RELAY_TOKEN = os.environ.get("CRM_RELAY_TOKEN", "")

HOST = os.environ.get("CRM_RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("CRM_RELAY_PORT", "8767"))

# A SQLite LIKE across four joined tables, on a machine that is also running
# the CRM's own users. Generous, but not open-ended.
TIMEOUT_SECONDS = float(os.environ.get("CRM_RELAY_TIMEOUT_SECONDS", "10"))

# Spoken aloud, so the limit is what a person can hold in their head, not what
# the database can return. Past this the robot asks for a narrower name.
MAX_RESULTS = int(os.environ.get("CRM_RELAY_MAX_RESULTS", "3"))

# The whitelist is the security boundary, and it is deliberately redundant
# with the CRM narrowing its own reply. Everything not named here is dropped,
# including fields the CRM grows later: a new column must not start crossing
# the tailnet because somebody added it at the other end.
ALLOWED_FIELDS = ("customer_name", "cemetery_name", "area")


def _project(row):
    """Copy out the sayable fields and nothing else."""
    return {key: row.get(key) for key in ALLOWED_FIELDS if row.get(key) not in (None, "")}


def _get_json(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def lookup(name, asked_by):
    """Ask the CRM about a name. Returns (total_found, [projected rows]).

    The one endpoint is the one asked for in docs/CRM_LOOKUP_API_REQUEST.md,
    which narrows on the CRM's side and records who asked in its audit log.
    There is no fallback to the general field search: it answers 401 to
    everyone now, and the ways of getting past that all cost more than they
    are worth (see the module docstring).

    `count` is the true number of distinct graves and may exceed the rows
    returned -- the robot needs it to say "seven people, narrow it down"
    rather than reading the first three as if they were all of them.
    """
    headers = {"Accept": "application/json"}
    if CRM_TOKEN:
        headers["X-Tachikoma-Token"] = CRM_TOKEN

    query = urllib.parse.urlencode({"name": name, "asked_by": asked_by or "unknown"})
    _, payload = _get_json(f"{CRM_BASE_URL}/api/tachikoma/lookup?{query}",
                           headers, TIMEOUT_SECONDS)

    rows = [_project(row) for row in payload.get("results", [])]
    total = int(payload.get("count", len(rows)))
    return total, rows[:MAX_RESULTS]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/healthz":
            # Deliberately unauthenticated and deliberately silent about the
            # CRM: this answers "is the relay up", which is a question the
            # person standing at the office PC needs to ask.
            return self._send(200, {"ok": True})

        if parsed.path != "/crm/lookup":
            return self._send(404, {"error": "not found"})

        if self.headers.get("X-Tachikoma-Token", "") != RELAY_TOKEN:
            return self._send(403, {"error": "forbidden"})

        params = urllib.parse.parse_qs(parsed.query)
        name = (params.get("name", [""])[0] or "").strip()
        asked_by = (params.get("asked_by", [""])[0] or "unknown").strip()
        if not name:
            return self._send(400, {"error": "name is required"})

        try:
            total, results = lookup(name, asked_by)
        except urllib.error.HTTPError as exc:
            self.log_message("crm lookup upstream HTTP %s", exc.code)
            if exc.code == 403:
                return self._send(502, {"error": "crm rejected the token"})
            if exc.code == 404:
                return self._send(503, {"error": "crm lookup endpoint not available"})
            return self._send(502, {"error": "crm rejected the lookup"})
        except Exception as exc:                     # noqa: BLE001 - reported, not raised
            # The message may quote the URL, which carries the name searched
            # for. Report the kind of failure and not the failure itself.
            self.log_message("crm lookup failed (%s)", type(exc).__name__)
            return self._send(502, {"error": "crm unreachable"})

        self._send(200, {"count": total, "results": results})

    def log_request(self, code="-", size="-"):
        # The default logs the request line, and the request line carries the
        # name searched for -- a real customer, written to a file on the office
        # PC that nobody is accountable for. The CRM's audit log is where that
        # record belongs. Log the shape of the traffic instead.
        self.log_message("%s -> %s", urllib.parse.urlparse(self.path).path, code)

    def log_message(self, fmt, *args):
        sys.stderr.write("[crm_relay] " + (fmt % args) + "\n")


def main():
    if not RELAY_TOKEN:
        sys.exit("CRM_RELAY_TOKEN is not set. This process listens on the tailnet "
                 "and would otherwise serve customer lookups to anyone who can "
                 "reach this PC. Put it in gateway/.env.crm_relay.")

    print(f"Tachikoma CRM relay listening on {HOST}:{PORT} -> {CRM_BASE_URL}")
    print("the gateway reaches this over Tailscale; the CRM is reached from this PC")
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
