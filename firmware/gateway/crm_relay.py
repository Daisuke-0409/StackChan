"""Answers "where is so-and-so's grave?" without letting the ledger leave the office.

The CRM holds real customer records -- names, addresses, the deceased, photos
of the work -- and its own documentation says it must not be exposed as it
stands. The gateway needs three fields out of it: who, which cemetery, which
district. This process is the seam between those two facts.

    home gateway --(Tailscale)--> this, on the office PC --(localhost)--> CRM

Three questions cross it: where is so-and-so's grave (lookup), who did I
see in that district recently (find, which carries a phone number), and
what link opens that customer's record (show, which carries no record at
all -- an integer goes in and a URL comes out).

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
import webbrowser
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

# The whitelists are the security boundary, and they are deliberately
# redundant with the CRM narrowing its own replies. Everything not named
# here is dropped, including fields the CRM grows later: a new column must
# not start crossing the tailnet because somebody added it at the other end.
ALLOWED_FIELDS = ("customer_name", "cemetery_name", "area")

# A conditional search carries a phone number, which is a heavier
# disclosure than where a grave is -- the robot says it out loud, so
# everyone in the room hears it. It has its own list rather than being
# folded into the one above, so that widening one can never widen the
# other by accident.
ALLOWED_FIND_FIELDS = ("customer_id", "customer_name", "phone", "cemetery_name")

# The CRM builds the URL it returns from the Host it was called on, so
# asking it over the tailnet yields a URL reachable from the other site.
# Nothing else in the reply is carried.
ALLOWED_SHOW_FIELDS = ("url",)


def _project(row, allowed=ALLOWED_FIELDS):
    """Copy out the sayable fields and nothing else."""
    return {key: row.get(key) for key in allowed if row.get(key) not in (None, "")}


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


def find(params, asked_by):
    """Search by circumstance -- district, staff, since -- not by name alone.

    The CRM requires at least one condition and answers 400 otherwise; that
    refusal is passed through rather than second-guessed here, because a
    search with no conditions is a request for the whole ledger and the
    place to say no to that is the place that holds it.
    """
    headers = {"Accept": "application/json"}
    if CRM_TOKEN:
        headers["X-Tachikoma-Token"] = CRM_TOKEN

    query = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value})
    _, payload = _get_json(f"{CRM_BASE_URL}/api/tachikoma/find?{query}",
                           headers, TIMEOUT_SECONDS)
    rows = [_project(row, ALLOWED_FIND_FIELDS)
            for row in payload.get("results", [])]
    total = int(payload.get("count", len(rows)))
    return total, rows[:MAX_RESULTS]


def show(customer_id, asked_by, host=None):
    """The URL that opens one customer's record, for a screen to display.

    The record itself never comes through here. That is the whole appeal of
    showing over speaking: what crosses this process is an integer on the
    way in and a link on the way out, and the customer's details go from
    the CRM to a monitor without passing through anything of ours.

    `host` overrides the Host header, so the CRM builds a URL reachable
    from wherever the screen is rather than from its own loopback.
    """
    headers = {"Accept": "application/json"}
    if CRM_TOKEN:
        headers["X-Tachikoma-Token"] = CRM_TOKEN
    if host:
        headers["Host"] = host

    query = urllib.parse.urlencode({"customer_id": customer_id,
                                    "asked_by": asked_by or "unknown"})
    _, payload = _get_json(f"{CRM_BASE_URL}/api/tachikoma/show?{query}",
                           headers, TIMEOUT_SECONDS)
    return _project(payload, ALLOWED_SHOW_FIELDS)


def open_on_this_screen(customer_id, asked_by):
    """Put one customer's record on the monitor attached to this machine.

    The caller passes an id and nothing else. It cannot pass a URL, and
    that is the whole design: an endpoint that opened whatever it was
    handed would be a way to make the office PC visit anything, dressed up
    as a feature. The link is asked for from the CRM here, and checked
    against the CRM's own address before anything opens it.

    Nothing about the customer passes through this process -- the browser
    fetches the record itself, from a page that still requires a login.
    """
    result = show(customer_id, asked_by)
    url = result.get("url") or ""
    expected_host = urllib.parse.urlsplit(CRM_BASE_URL).netloc
    if urllib.parse.urlsplit(url).netloc != expected_host:
        raise ValueError("CRM 以外のアドレスは開きません")
    webbrowser.open(url)
    return {"opened": True, "url": url}


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

        if parsed.path not in ("/crm/lookup", "/crm/find", "/crm/show",
                               "/crm/open"):
            return self._send(404, {"error": "not found"})

        if self.headers.get("X-Tachikoma-Token", "") != RELAY_TOKEN:
            return self._send(403, {"error": "forbidden"})

        params = urllib.parse.parse_qs(parsed.query)
        first = lambda key: (params.get(key, [""])[0] or "").strip()  # noqa: E731
        asked_by = first("asked_by") or "unknown"

        try:
            if parsed.path == "/crm/find":
                conditions = {key: first(key)
                              for key in ("area", "staff", "since", "name")}
                conditions["asked_by"] = asked_by
                if not any(conditions[key] for key in ("area", "staff", "since", "name")):
                    return self._send(400, {"error": "at least one condition is required"})
                total, results = find(conditions, asked_by)
                return self._send(200, {"count": total, "results": results})

            if parsed.path in ("/crm/show", "/crm/open"):
                customer_id = first("customer_id")
                if not customer_id.isdigit():
                    return self._send(400, {"error": "customer_id must be a number"})
                if parsed.path == "/crm/show":
                    return self._send(200, show(customer_id, asked_by,
                                                host=first("host") or None))
                try:
                    return self._send(200, open_on_this_screen(customer_id, asked_by))
                except ValueError as exc:
                    self.log_message("refused to open (%s)", exc)
                    return self._send(502, {"error": str(exc)})

            name = first("name")
            if not name:
                return self._send(400, {"error": "name is required"})
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


class _SingleInstanceServer(http.server.ThreadingHTTPServer):
    """Refuses to start when the port is already taken.

    Python turns SO_REUSEADDR on by default, and on Windows that does not
    mean what it means elsewhere: a second process can bind a port another
    one is already listening on, and requests are split between them at
    random. The visible symptom is that a change does not take effect --
    the old process is still answering half the time.

    The CRM hit this and fixed it on 2026-08-19; this file hit it on
    2026-08-20, an hour after reading their note about it. Failing to start
    is the correct behaviour: a relay that is already running does not need
    a second one, and a person who meant to restart it would rather be told.
    """

    allow_reuse_address = False


def main():
    if not RELAY_TOKEN:
        sys.exit("CRM_RELAY_TOKEN is not set. This process listens on the tailnet "
                 "and would otherwise serve customer lookups to anyone who can "
                 "reach this PC. Put it in gateway/.env.crm_relay.")

    print(f"Tachikoma CRM relay listening on {HOST}:{PORT} -> {CRM_BASE_URL}")
    print("the gateway reaches this over Tailscale; the CRM is reached from this PC")
    server = _SingleInstanceServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
