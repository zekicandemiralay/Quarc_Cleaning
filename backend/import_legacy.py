"""One-off migration helper — NOT part of the app itself, not wired into
main.py/router.py, never run automatically.

Replays a Clean House export (see the export snippet in the project's
chat history / commit message) into a LIVE Quarc Cleaning instance
through its real HTTP API, rather than writing to the database directly.
Doing it this way means every normal rule still applies on import —
duplicate names get rejected, a section's parent must already exist,
Absent logs can't carry sections — instead of silently writing rows that
bypass those checks.

Run from a machine that can reach the target instance (e.g. the server
itself, or anywhere on the Tailscale network):

    python import_legacy.py clean_house_export.json https://quarcnet0.tail84500c.ts.net:4004

Safe to re-run: persons/sections already present (matched by name) are
skipped rather than duplicated. Logs are NOT de-duplicated against
existing ones beyond the API's own same-second-collision suffixing, so
only run this against an otherwise-empty instance (which is exactly
what a fresh Quarc Cleaning install is).
"""
import json
import sys
import urllib.error
import urllib.request


def post(base, path, payload):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"__error__": e.code, "__body__": body}


def main():
    if len(sys.argv) != 3:
        print("Usage: python import_legacy.py <export.json> <base_url>")
        print("  e.g. python import_legacy.py clean_house_export.json https://quarcnet0.tail84500c.ts.net:4004")
        sys.exit(1)

    export_path, base = sys.argv[1], sys.argv[2].rstrip("/")
    with open(export_path, encoding="utf-8") as f:
        data = json.load(f)

    print(f"Importing {len(data['persons'])} person(s), {len(data['sections'])} section(s), "
          f"{len(data['logs'])} log(s) into {base} ...")

    # ── Persons ──────────────────────────────────────────────────────────
    added_p = skipped_p = 0
    for p in data["persons"]:
        r = post(base, "/api/persons", {"name": p["name"], "active": bool(p["active"])})
        if r.get("__error__") == 409:
            skipped_p += 1
        elif "__error__" in r:
            print(f"  FAILED: person '{p['name']}': {r['__body__']}")
        else:
            added_p += 1
    print(f"Persons: added={added_p} skipped_existing={skipped_p}")

    # ── Sections — parents first, since a child's parent must already exist ──
    parents = [s for s in data["sections"] if not s.get("parent_name")]
    children = [s for s in data["sections"] if s.get("parent_name")]
    added_s = skipped_s = 0
    for s in parents + children:
        r = post(base, "/api/sections", {
            "name": s["name"],
            "parent_name": s.get("parent_name"),
            "type": s["type"],
            "active": bool(s["active"]),
            "clean_cooldown": s["clean_cooldown"],
        })
        if r.get("__error__") == 409:
            skipped_s += 1
        elif "__error__" in r:
            print(f"  FAILED: section '{s['name']}': {r['__body__']}")
        else:
            added_s += 1
    print(f"Sections: added={added_s} skipped_existing={skipped_s}")

    # ── Logs — oldest first, explicit `date` so history lands on the real day ──
    logs_sorted = sorted(data["logs"], key=lambda l: l["date"])
    added_l = failed_l = 0
    for l in logs_sorted:
        r = post(base, "/api/logs", {
            "person_name": l["person_name"],
            "type": l["type"],
            "sections": json.loads(l["sections"]) if isinstance(l["sections"], str) else l["sections"],
            "date": l["date"],
        })
        if "__error__" in r:
            failed_l += 1
            print(f"  FAILED: log '{l['log_id']}': {r['__body__']}")
        else:
            added_l += 1
    print(f"Logs: added={added_l} failed={failed_l}")


if __name__ == "__main__":
    main()
