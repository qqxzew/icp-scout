"""CZ.NIC WHOIS: who owns a .cz domain.

A second, independent way to tie a domain to a company - and the only
one that does not depend on what the website chooses to print.

The registry answers on port 43 with the registrant's organisation,
contact person and postal address:

    domain:     csadrychnov.cz
    org:        ČSAD, s.r.o. Rychnov n. Kn.
    name:       Josef Kotyza
    address:    Dobruška čp. 202
    address:    Dobruška
    address:    51801

That is a statement made by whoever registered the domain, held in a
registry - not marketing copy on a page. It reaches the cases the HTTP
check cannot:

* sites rendered by JavaScript, where the text layer is empty
* sites that simply never print their ICO
* domains whose contact page we failed to find

There is no ICO field, so the match is made on three weaker things at
once - organisation name, postal code and the names of the company's
directors from ARES. A postal code plus a name is a good deal stronger
than either alone.

Rate limit, measured on 2026-08-22: a burst of unthrottled queries is
cut off after twelve ("Your connection limit exceeded"). One query per
second ran 45 in a row with no refusal, so DELAY is one second and the
module is deliberately serial.

Run:
    python -m pipeline.sources.whois_cz csadrychnov.cz
"""

import re
import socket
import sys
import threading
import time

SERVER = "whois.nic.cz"
PORT = 43
TIMEOUT = 12

# One second between queries. Measured, not guessed - see the module
# docstring. Going faster earns a refusal, not an answer.
DELAY = 1.0

_last_query = [0.0]
_pace = threading.Lock()

LIMIT_MARKER = re.compile(r"(?i)connection limit exceeded|rate limit")


def query(domain):
    """Raw WHOIS text for one domain, or None when the registry refused.

    Refusal and "no such domain" are different answers and must not be
    confused: the first means ask again later, the second is a fact.

    The pacing lock is held across the sleep, so callers from several
    threads queue up rather than all waking at once and tripping the
    limit together. That makes this module serial by construction -
    which is why the pipeline runs it as its own pass, after the
    parallel HTTP one, instead of inside it.
    """
    with _pace:
        wait = DELAY - (time.monotonic() - _last_query[0])
        if wait > 0:
            time.sleep(wait)
        _last_query[0] = time.monotonic()

    try:
        connection = socket.create_connection((SERVER, PORT), timeout=TIMEOUT)
        connection.sendall((domain + "\r\n").encode())
        chunks = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        connection.close()
    except OSError:
        return None

    text = b"".join(chunks).decode("utf-8", "replace")
    if LIMIT_MARKER.search(text):
        return None
    return text


def parse(text):
    """Pull the registrant block out of a WHOIS answer.

    The reply is several blocks in a row - the domain, then the
    registrant contact, then the nsset, then the registrar. They are not
    labelled, they are only ordered, so the registrant is taken as the
    first `contact:` block and reading stops at the next block header.
    Anything later belongs to the hosting company, not to the owner.
    """
    if not text:
        return None

    lines = [line for line in text.splitlines() if not line.startswith("%")]

    start = None
    for index, line in enumerate(lines):
        if line.startswith("contact:"):
            start = index + 1
            break
    if start is None:
        return None

    record = {"org": None, "person": None, "address": [], "postcode": None}
    for line in lines[start:]:
        if re.match(r"^(registrar|nsset|keyset|contact|domain):", line):
            break
        key, _, value = line.partition(":")
        value = value.strip()
        if not value:
            continue
        if key == "org" and not record["org"]:
            record["org"] = value
        elif key == "name" and not record["person"]:
            record["person"] = value
        elif key == "address":
            record["address"].append(value)
            if re.fullmatch(r"\d{3} ?\d{2}", value):
                record["postcode"] = value.replace(" ", "")

    if not (record["org"] or record["person"]):
        return None
    return record


def owner(domain):
    """Registrant of one domain, or None."""
    return parse(query(domain))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.sources.whois_cz <domain>")
        sys.exit(1)
    import json
    print(json.dumps(owner(sys.argv[1]), ensure_ascii=False, indent=2))
