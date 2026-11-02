"""Quarc Cleaning — household chore tracker.

Intentionally open: no auth dependencies, single shared dataset reachable by
anyone on the Tailscale network. All persistence lives in the three
`house_*` tables defined in database.py.

Extracted from Kaan's "skynet" monorepo (originally "Clean House", one of
several apps sharing a single backend there) into its own standalone app.
Business logic is untouched from the original — only the route prefix
changed (`/clean_house` -> `/api`), and two pieces of cruft specific to that
monorepo were removed: a cross-app sync into a `split_expenses` table that
doesn't exist here, and a hardcoded 2026-08-04 week cutoff that was Kaan's
own one-off history wipe, not something a fresh dataset needs.
"""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["clean_house"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class PersonIn(BaseModel):
    name: str
    active: bool = True


class SectionIn(BaseModel):
    name: str
    parent_name: Optional[str] = None
    type: str = "Primary"             # 'Primary' | 'Secondary'
    active: bool = True
    clean_cooldown: int = Field(ge=1)


class LogIn(BaseModel):
    person_name: str
    type: str                          # 'Cleaning' | 'Absent'
    sections: list[str] = []
    date: Optional[str] = None         # 'YYYY-MM-DDTHH:mm[:ss]' — overrides
                                        # the server clock; used to backfill
                                        # an Absent log into a past week.


class LogUpdateIn(BaseModel):
    person_name: str
    type: str                          # 'Cleaning' | 'Absent'
    sections: list[str] = []
    date: str                          # 'YYYY-MM-DDTHH:mm:ss' or 'YYYY-MM-DDTHH:mm'


# ── Helpers ───────────────────────────────────────────────────────────────────

def _person_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "active": bool(r["active"]),
        "created_at": r["created_at"],
    }


def _section_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "parent_name": r["parent_name"],
        "type": r["type"],
        "active": bool(r["active"]),
        "clean_cooldown": r["clean_cooldown"],
        "created_at": r["created_at"],
    }


def _log_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "log_id": r["log_id"],
        "person_name": r["person_name"],
        "type": r["type"],
        "sections": json.loads(r["sections"]),
        "date": r["date"],
        "created_at": r["created_at"],
    }


def _validate_section(payload: SectionIn, conn: sqlite3.Connection,
                      *, ignore_id: Optional[int] = None) -> None:
    """Cross-field validation shared by add + edit."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Section name is required")
    parent = (payload.parent_name or "").strip() or None
    type_ = payload.type
    if type_ not in ("Primary", "Secondary"):
        raise HTTPException(400, "Type must be 'Primary' or 'Secondary'")
    if payload.clean_cooldown is None or payload.clean_cooldown < 1:
        raise HTTPException(400, "Clean Cooldown must be a positive integer")

    # NOTE: An earlier version of this code forbade Primary children, which
    # was overly strict. Child sections can be either Primary or Secondary —
    # the Primary/Secondary distinction drives the "green parent" rule in
    # the time table (only primary children participate in the auto-green
    # calculation), but Primary children with a parent are perfectly valid.

    # Parent must exist and itself be a parent (parent_name IS NULL) — otherwise
    # we'd allow grandchild chains which the spec doesn't define.
    if parent is not None:
        if parent == name:
            raise HTTPException(400, "A section cannot be its own parent")
        prow = conn.execute(
            "SELECT id, parent_name FROM house_sections WHERE name = ?",
            (parent,),
        ).fetchone()
        if not prow:
            raise HTTPException(400, f"Parent section '{parent}' does not exist")
        if prow["parent_name"]:
            raise HTTPException(
                400,
                f"'{parent}' is itself a child section — only one level of "
                "nesting is supported"
            )

    # Uniqueness (case-sensitive, matching the UNIQUE constraint).
    clash = conn.execute(
        "SELECT id FROM house_sections WHERE name = ? AND id IS NOT ?",
        (name, ignore_id),
    ).fetchone()
    if clash:
        raise HTTPException(409, f"Section '{name}' already exists")


# ── Persons CRUD ──────────────────────────────────────────────────────────────

@router.get("/persons")
def list_persons():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM house_persons ORDER BY name"
        ).fetchall()
    return [_person_to_dict(r) for r in rows]


@router.post("/persons", status_code=201)
def add_person(payload: PersonIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Person name is required")
    with get_conn() as conn:
        clash = conn.execute(
            "SELECT id FROM house_persons WHERE name = ?", (name,)
        ).fetchone()
        if clash:
            raise HTTPException(409, f"Person '{name}' already exists")
        cur = conn.execute(
            "INSERT INTO house_persons (name, active) VALUES (?, ?)",
            (name, 1 if payload.active else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_persons WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _person_to_dict(row)


@router.put("/persons/{person_id}")
def edit_person(person_id: int, payload: PersonIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Person name is required")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM house_persons WHERE id = ?", (person_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Person not found")
        clash = conn.execute(
            "SELECT id FROM house_persons WHERE name = ? AND id != ?",
            (name, person_id),
        ).fetchone()
        if clash:
            raise HTTPException(409, f"Another person named '{name}' exists")

        # If the name changed, propagate it to any log rows so historical data
        # stays meaningful.
        if name != row["name"]:
            conn.execute(
                "UPDATE house_logs SET person_name = ? WHERE person_name = ?",
                (name, row["name"]),
            )
            # Logs are keyed by log_id = "PersonName YYYY-MM-DDTHH:mm:ss".
            # Rewrite the prefix so log_ids stay consistent with the new name.
            old_prefix = f"{row['name']} "
            new_prefix = f"{name} "
            for lr in conn.execute(
                "SELECT id, log_id FROM house_logs WHERE log_id LIKE ?",
                (old_prefix + "%",),
            ).fetchall():
                new_id = new_prefix + lr["log_id"][len(old_prefix):]
                conn.execute(
                    "UPDATE house_logs SET log_id = ? WHERE id = ?",
                    (new_id, lr["id"]),
                )

        conn.execute(
            "UPDATE house_persons SET name = ?, active = ? WHERE id = ?",
            (name, 1 if payload.active else 0, person_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_persons WHERE id = ?", (person_id,)
        ).fetchone()
    return _person_to_dict(row)


@router.delete("/persons/{person_id}")
def delete_person(person_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM house_persons WHERE id = ?", (person_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Person not found")
        conn.execute("DELETE FROM house_persons WHERE id = ?", (person_id,))
        conn.commit()
    return {"status": "deleted", "id": person_id, "name": row["name"]}


# ── Sections CRUD ─────────────────────────────────────────────────────────────

@router.get("/sections")
def list_sections():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM house_sections ORDER BY "
            # Parents first (parent_name IS NULL), then children grouped
            # under their parent, primary before secondary.
            "CASE WHEN parent_name IS NULL THEN 0 ELSE 1 END, "
            "COALESCE(parent_name, name), "
            "CASE type WHEN 'Primary' THEN 0 ELSE 1 END, "
            "name"
        ).fetchall()
    return [_section_to_dict(r) for r in rows]


@router.post("/sections", status_code=201)
def add_section(payload: SectionIn):
    with get_conn() as conn:
        _validate_section(payload, conn)
        name = payload.name.strip()
        parent = (payload.parent_name or "").strip() or None
        cur = conn.execute(
            "INSERT INTO house_sections "
            "(name, parent_name, type, active, clean_cooldown) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, parent, payload.type, 1 if payload.active else 0,
             payload.clean_cooldown),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_sections WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _section_to_dict(row)


@router.put("/sections/{section_id}")
def edit_section(section_id: int, payload: SectionIn):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM house_sections WHERE id = ?", (section_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Section not found")
        _validate_section(payload, conn, ignore_id=section_id)
        new_name = payload.name.strip()
        parent = (payload.parent_name or "").strip() or None

        # Renaming a parent section?  Cascade the new name into its children's
        # parent_name and into any logs that reference the old name.
        if new_name != row["name"]:
            conn.execute(
                "UPDATE house_sections SET parent_name = ? WHERE parent_name = ?",
                (new_name, row["name"]),
            )
            for lr in conn.execute(
                "SELECT id, sections FROM house_logs"
            ).fetchall():
                sects = json.loads(lr["sections"])
                if row["name"] in sects:
                    new_sects = [new_name if s == row["name"] else s for s in sects]
                    conn.execute(
                        "UPDATE house_logs SET sections = ? WHERE id = ?",
                        (json.dumps(new_sects), lr["id"]),
                    )

        conn.execute(
            "UPDATE house_sections "
            "SET name = ?, parent_name = ?, type = ?, active = ?, "
            "    clean_cooldown = ? "
            "WHERE id = ?",
            (new_name, parent, payload.type, 1 if payload.active else 0,
             payload.clean_cooldown, section_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_sections WHERE id = ?", (section_id,)
        ).fetchone()
    return _section_to_dict(row)


@router.delete("/sections/{section_id}")
def delete_section(section_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM house_sections WHERE id = ?", (section_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Section not found")
        # Refuse if this is a parent with children — force the user to clean up
        # first rather than silently orphaning the children.
        if row["parent_name"] is None:
            kids = conn.execute(
                "SELECT COUNT(*) AS c FROM house_sections WHERE parent_name = ?",
                (row["name"],),
            ).fetchone()["c"]
            if kids:
                raise HTTPException(
                    409,
                    f"Cannot delete '{row['name']}' — it still has {kids} "
                    "child section(s). Remove them first."
                )
        conn.execute("DELETE FROM house_sections WHERE id = ?", (section_id,))
        conn.commit()
    return {"status": "deleted", "id": section_id, "name": row["name"]}


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
def list_logs():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM house_logs ORDER BY date DESC, id DESC"
        ).fetchall()
    return [_log_to_dict(r) for r in rows]


@router.post("/logs", status_code=201)
def create_log(payload: LogIn):
    if payload.type not in ("Cleaning", "Absent"):
        raise HTTPException(400, "Type must be 'Cleaning' or 'Absent'")
    person = (payload.person_name or "").strip()
    if not person:
        raise HTTPException(400, "Person name is required")
    sects = payload.sections or []
    if payload.type == "Cleaning" and not sects:
        raise HTTPException(
            400, "At least one section must be selected for a Cleaning log"
        )
    if payload.type == "Absent":
        sects = []  # spec: absent logs always have an empty sections list

    now = datetime.now().replace(microsecond=0)

    # Normally the log gets the server clock. A caller may instead pass an
    # explicit `date` to backfill a log into a past week (e.g. recording an
    # Absent entry for a week someone was already gone, after the fact) —
    # same normalisation/validation as update_log's date field, plus a
    # future-date guard since backfilling is only ever for the past.
    if payload.date:
        date_str = payload.date.strip()
        if len(date_str) == 16:
            date_str += ":00"
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise HTTPException(
                400, "Invalid date — expected YYYY-MM-DDTHH:mm or YYYY-MM-DDTHH:mm:ss"
            )
        if parsed.date() > now.date():
            raise HTTPException(400, "date cannot be in the future")
    else:
        date_str = now.strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        # Verify the person exists & is active
        prow = conn.execute(
            "SELECT id, active FROM house_persons WHERE name = ?", (person,)
        ).fetchone()
        if not prow:
            raise HTTPException(400, f"Person '{person}' does not exist")
        if not prow["active"]:
            raise HTTPException(400, f"Person '{person}' is inactive")

        # Spec: log_id = "Person Name" + "YYYY-MM-DDTHH:mm:ss"
        log_id = f"{person} {date_str}"

        try:
            cur = conn.execute(
                "INSERT INTO house_logs "
                "(log_id, person_name, type, sections, date) "
                "VALUES (?, ?, ?, ?, ?)",
                (log_id, person, payload.type, json.dumps(sects), date_str),
            )
        except sqlite3.IntegrityError:
            # Same person logged twice in the same second — append a suffix.
            log_id = f"{log_id}#{int(now.timestamp()) % 1000}"
            cur = conn.execute(
                "INSERT INTO house_logs "
                "(log_id, person_name, type, sections, date) "
                "VALUES (?, ?, ?, ?, ?)",
                (log_id, person, payload.type, json.dumps(sects), date_str),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_logs WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return _log_to_dict(row)


@router.put("/logs/{log_id}")
def update_log(log_id: int, payload: LogUpdateIn):
    if payload.type not in ("Cleaning", "Absent"):
        raise HTTPException(400, "Type must be 'Cleaning' or 'Absent'")
    person = (payload.person_name or "").strip()
    if not person:
        raise HTTPException(400, "Person name is required")
    sects = payload.sections or []
    if payload.type == "Cleaning" and not sects:
        raise HTTPException(
            400, "At least one section must be selected for a Cleaning log"
        )
    if payload.type == "Absent":
        sects = []

    # Normalise date: datetime-local inputs give 'YYYY-MM-DDTHH:mm'; add seconds.
    date_str = (payload.date or "").strip()
    if len(date_str) == 16:
        date_str += ":00"
    try:
        datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise HTTPException(400, "Invalid date — expected YYYY-MM-DDTHH:mm or YYYY-MM-DDTHH:mm:ss")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM house_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Log not found")

        prow = conn.execute(
            "SELECT id, active FROM house_persons WHERE name = ?", (person,)
        ).fetchone()
        if not prow:
            raise HTTPException(400, f"Person '{person}' does not exist")
        if not prow["active"]:
            raise HTTPException(400, f"Person '{person}' is inactive")

        # Recompute log_id when person or date changes to keep it consistent.
        old_log_id = row["log_id"]
        new_log_id = f"{person} {date_str}"
        if new_log_id != old_log_id:
            clash = conn.execute(
                "SELECT id FROM house_logs WHERE log_id = ? AND id != ?",
                (new_log_id, log_id),
            ).fetchone()
            if clash:
                suffix = int(datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S").timestamp()) % 1000
                new_log_id = f"{new_log_id}#{suffix}"

        conn.execute(
            "UPDATE house_logs "
            "SET person_name = ?, type = ?, sections = ?, date = ?, log_id = ? "
            "WHERE id = ?",
            (person, payload.type, json.dumps(sects), date_str, new_log_id, log_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM house_logs WHERE id = ?", (log_id,)
        ).fetchone()
    return _log_to_dict(row)


@router.delete("/logs/{log_id}")
def delete_log(log_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, log_id FROM house_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Log not found")
        conn.execute("DELETE FROM house_logs WHERE id = ?", (log_id,))
        conn.commit()
    return {"status": "deleted", "id": log_id, "log_id": row["log_id"]}


# ── Tuesday-week numbering ────────────────────────────────────────────────────
# Spec:
#   • Tuesday is the first day of the week.
#   • If Jan 1 is not a Tuesday, the partial first stretch (Jan 1 → Mon before
#     the first Tuesday) is Week 0.  The first Tuesday-starting block is Week 1.
#   • If Jan 1 IS a Tuesday, there is no Week 0 — Jan 1 itself starts Week 1.
#   • Subsequent weeks are full Tue-Mon blocks.  The final week is capped at
#     Dec 31 (so its visible end date may be a few days earlier than Monday).

def _build_year_weeks(year: int) -> list[dict]:
    """Return an ordered list of {week, start, end} dicts for the given year.
    Dates are ISO 'YYYY-MM-DD' strings; both endpoints are inclusive."""
    jan1 = date(year, 1, 1)
    dec31 = date(year, 12, 31)

    # weekday(): Monday=0 ... Tuesday=1 ... Sunday=6
    # Days from jan1 forward to the first Tuesday:
    days_to_tue = (1 - jan1.weekday()) % 7
    first_tue = jan1 + timedelta(days=days_to_tue)

    weeks: list[dict] = []

    if first_tue != jan1:
        # Week 0: Jan 1 to the Monday before the first Tuesday.
        weeks.append({
            "week": 0,
            "start": jan1.isoformat(),
            "end":   (first_tue - timedelta(days=1)).isoformat(),
        })

    cur = first_tue
    n = 1
    while cur <= dec31:
        end = min(cur + timedelta(days=6), dec31)
        weeks.append({
            "week": n,
            "start": cur.isoformat(),
            "end":   end.isoformat(),
        })
        cur = cur + timedelta(days=7)
        n += 1
    return weeks


def _week_index_for_date(d: date, weeks: list[dict]) -> Optional[int]:
    """Return the position (0-based) of the bucket that `d` falls into, or
    None if `d` is outside the year's range."""
    for i, w in enumerate(weeks):
        if w["start"] <= d.isoformat() <= w["end"]:
            return i
    return None


# ── Persons Time Table analyser ───────────────────────────────────────────────

@router.get("/timetable")
def get_timetable(year: Optional[int] = None):
    """Return the colour-coded grid for the Persons Time Table.

    Output shape:
      {
        "year": 2026,
        "weeks": [{week, start, end}, ...],
        "persons": [
          {"name": "...", "cells": [{"color": "green|orange|red|blank",
                                      "logs": [log_id, ...]}, ...]}
        ],
        "totals": {"green": N, "orange": N, "red": N,
                   "green_pct": float}
      }
    """
    today = date.today()
    year = year or today.year
    weeks = _build_year_weeks(year)

    with get_conn() as conn:
        persons = conn.execute(
            "SELECT name FROM house_persons WHERE active = 1 ORDER BY name"
        ).fetchall()
        sections = conn.execute(
            "SELECT name, parent_name, type FROM house_sections"
        ).fetchall()
        logs = conn.execute(
            "SELECT person_name, type, sections, date, log_id "
            "FROM house_logs WHERE date LIKE ?",
            (f"{year}-%",),
        ).fetchall()

    # Pre-compute parent -> {primary_children_set} mapping (across ALL sections,
    # not just active ones — historical logs reference whatever existed at
    # the time, and the spec's parent-completion check needs the full picture).
    primaries_of: dict[str, set[str]] = {}
    for s in sections:
        if s["parent_name"]:
            if s["type"] == "Primary":
                primaries_of.setdefault(s["parent_name"], set()).add(s["name"])
        else:
            primaries_of.setdefault(s["name"], set())  # ensure key exists

    # Bucket logs by (person, week_index)
    buckets: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for lr in logs:
        try:
            d = datetime.strptime(lr["date"][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        idx = _week_index_for_date(d, weeks)
        if idx is None:
            continue
        buckets.setdefault((lr["person_name"], idx), []).append(lr)

    totals = {"green": 0, "orange": 0, "red": 0}
    persons_out = []

    for p in persons:
        cells = []
        for i, w in enumerate(weeks):
            week_start = datetime.strptime(w["start"], "%Y-%m-%d").date()
            # Only weeks that haven't started yet are blank.  The current
            # (in-progress) week is evaluated immediately, so a fresh Record
            # Entry shows up right away in its column.
            if today < week_start:
                cells.append({"color": "blank", "logs": []})
                continue

            week_logs = buckets.get((p["name"], i), [])
            cleaning_logs = [lr for lr in week_logs if lr["type"] == "Cleaning"]
            absent_logs   = [lr for lr in week_logs if lr["type"] == "Absent"]

            green = False
            green_log_ids: list[str] = []

            if cleaning_logs:
                parent_set: set[str] = set()
                child_set:  set[str] = set()
                for lr in cleaning_logs:
                    for nm in json.loads(lr["sections"]):
                        if nm in primaries_of:           # it's a parent
                            parent_set.add(nm)
                        else:
                            child_set.add(nm)

                # Rule 1: any parent listed → green.
                if parent_set:
                    green = True
                # Rule 2: even without a parent name, if every primary child
                # of *some* parent is in the child_set → green.
                if not green:
                    for parent, prims in primaries_of.items():
                        if prims and prims.issubset(child_set):
                            green = True
                            break

                if green:
                    green_log_ids = [lr["log_id"] for lr in cleaning_logs]

            if green:
                cells.append({"color": "green", "logs": green_log_ids})
                totals["green"] += 1
            elif absent_logs:
                cells.append({
                    "color": "orange",
                    "logs": [lr["log_id"] for lr in absent_logs],
                })
                totals["orange"] += 1
            else:
                cells.append({"color": "red", "logs": []})
                totals["red"] += 1

        persons_out.append({"name": p["name"], "cells": cells})

    coloured = totals["green"] + totals["orange"] + totals["red"]
    green_pct = (100.0 * totals["green"] / coloured) if coloured else 0.0
    totals["green_pct"] = round(green_pct, 1)

    return {
        "year": year,
        "today": today.isoformat(),
        "weeks": weeks,
        "persons": persons_out,
        "totals": totals,
    }


# ── Section Cleanness analyser ────────────────────────────────────────────────

@router.get("/cleanness")
def get_cleanness():
    """For each active section, compute the last-cleaned date, days past, and
    whether it's overdue (days past > clean_cooldown).  Parents are listed
    first, followed by their children in the same order the Sections list uses.
    """
    today = date.today()
    with get_conn() as conn:
        sections = conn.execute(
            "SELECT * FROM house_sections WHERE active = 1 ORDER BY "
            "CASE WHEN parent_name IS NULL THEN 0 ELSE 1 END, "
            "COALESCE(parent_name, name), "
            "CASE type WHEN 'Primary' THEN 0 ELSE 1 END, "
            "name"
        ).fetchall()
        logs = conn.execute(
            "SELECT type, sections, date, person_name FROM house_logs "
            "WHERE type = 'Cleaning' ORDER BY date DESC"
        ).fetchall()

    # Walk logs once and remember the latest date + person for every section
    # name seen.  Logs are already date-DESC so the first hit wins.
    last_cleaned: dict[str, str] = {}
    last_by: dict[str, str] = {}
    for lr in logs:
        for nm in json.loads(lr["sections"]):
            if nm not in last_cleaned:
                last_cleaned[nm] = lr["date"]
                last_by[nm] = lr["person_name"]

    # Pre-compute primary children per parent (across ALL sections, not just
    # active — historical logs may still reference deactivated children).
    primary_children_of: dict[str, list[str]] = {}
    for s in sections:
        if s["parent_name"] and s["type"] == "Primary":
            primary_children_of.setdefault(s["parent_name"], []).append(s["name"])
    # Also include children that exist but aren't in the active list — query
    # the full table so a deactivated primary child can still hold a parent
    # back from looking "fresh".
    with get_conn() as conn:
        all_primary_children = conn.execute(
            "SELECT parent_name, name FROM house_sections "
            "WHERE parent_name IS NOT NULL AND type = 'Primary'"
        ).fetchall()
    for r in all_primary_children:
        kids = primary_children_of.setdefault(r["parent_name"], [])
        if r["name"] not in kids:
            kids.append(r["name"])

    out = []
    for s in sections:
        is_parent = s["parent_name"] is None
        primary_kids = primary_children_of.get(s["name"], []) if is_parent else []

        if is_parent and primary_kids:
            # Parent's "Last Cleaned" = EARLIEST of its primary children's
            # last_cleaned dates.  The earliest child is the bottleneck — the
            # parent isn't truly clean until every primary child has been
            # cleaned, so the oldest child sets the parent's freshness.
            kid_dates = [(last_cleaned[k], last_by.get(k, ""))
                         for k in primary_kids if k in last_cleaned]
            if len(kid_dates) == len(primary_kids):
                # All primary children have at least one clean log.
                kid_dates.sort()  # ascending → earliest first
                last, by = kid_dates[0]
            else:
                # At least one primary child has never been cleaned →
                # parent shows as never cleaned too.
                last, by = None, None
        else:
            last = last_cleaned.get(s["name"])
            by = last_by.get(s["name"])

        days_past: Optional[int] = None
        overdue = False
        if last:
            try:
                last_d = datetime.strptime(last[:10], "%Y-%m-%d").date()
                days_past = (today - last_d).days
                overdue = days_past > s["clean_cooldown"]
            except ValueError:
                pass
        else:
            # Never cleaned — treat as overdue so the panel highlights it.
            overdue = True

        out.append({
            "id": s["id"],
            "name": s["name"],
            "parent_name": s["parent_name"],
            "type": s["type"],
            "clean_cooldown": s["clean_cooldown"],
            "last_cleaned": last,
            "last_cleaned_by": by,
            "days_past": days_past,
            "overdue": overdue,
        })

    return out


# ── Section counts per person ─────────────────────────────────────────────────

@router.get("/section_counts")
def get_section_counts(year: Optional[int] = None):
    """Per-person × per-section count table.

    Returns sections in the interleaved order: Parent1, Child1.1, Child1.2, ...,
    Parent2, ..., with active sections only.  Children within each parent
    are sorted primaries-first, then secondaries (matches the Settings list).

    For each active person we return a `counts` array aligned with `sections`,
    plus a `secondary_total` summing only Secondary-typed child columns.
    """
    today = date.today()
    year = year or today.year
    weeks = _build_year_weeks(year)

    # Visible week range: from the first week of the year through the current
    # week (past years show every week; future years show none yet).
    start_idx = 0
    if year > today.year:
        end_idx = start_idx - 1          # future year — no weeks elapsed yet
    elif year == today.year:
        cur_idx = _week_index_for_date(today, weeks)
        end_idx = cur_idx if cur_idx is not None else len(weeks) - 1
    else:
        end_idx = len(weeks) - 1         # past year — all weeks visible

    total_available_weeks = max(0, end_idx - start_idx + 1)

    # Cleaning logs need person + sections list — type='Cleaning' only.
    with get_conn() as conn:
        persons = conn.execute(
            "SELECT name FROM house_persons WHERE active = 1 ORDER BY name"
        ).fetchall()
        sections_rows = conn.execute(
            "SELECT * FROM house_sections WHERE active = 1"
        ).fetchall()
        logs = conn.execute(
            "SELECT person_name, sections FROM house_logs "
            "WHERE type = 'Cleaning' AND date LIKE ?",
            (f"{year}-%",),
        ).fetchall()
        absent_logs_rows = conn.execute(
            "SELECT person_name, date FROM house_logs "
            "WHERE type = 'Absent' AND date LIKE ?",
            (f"{year}-%",),
        ).fetchall()

    # Build interleaved section order.
    parents = [s for s in sections_rows if not s["parent_name"]]
    parents.sort(key=lambda s: s["name"].lower())
    children_by_parent: dict[str, list[sqlite3.Row]] = {}
    for s in sections_rows:
        if s["parent_name"]:
            children_by_parent.setdefault(s["parent_name"], []).append(s)
    for kids in children_by_parent.values():
        kids.sort(key=lambda s: (0 if s["type"] == "Primary" else 1, s["name"].lower()))

    ordered_sections: list[dict] = []
    for parent in parents:
        ordered_sections.append({
            "name": parent["name"], "type": parent["type"],
            "is_parent": True, "parent_name": None,
        })
        for c in children_by_parent.get(parent["name"], []):
            ordered_sections.append({
                "name": c["name"], "type": c["type"],
                "is_parent": False, "parent_name": parent["name"],
            })

    # Build (person_name, section_name) -> count from the year's cleaning logs.
    tally: dict[tuple[str, str], int] = {}
    for lr in logs:
        try:
            sects = json.loads(lr["sections"])
        except (TypeError, ValueError):
            continue
        for nm in sects:
            key = (lr["person_name"], nm)
            tally[key] = tally.get(key, 0) + 1

    name_idx = {s["name"]: i for i, s in enumerate(ordered_sections)}
    secondary_idx = {
        i for i, s in enumerate(ordered_sections)
        if not s["is_parent"] and s["type"] == "Secondary"
    }
    primary_child_idx = {
        i for i, s in enumerate(ordered_sections)
        if not s["is_parent"] and s["type"] == "Primary"
    }

    # Count distinct absent weeks per person within the visible range.
    absent_week_sets: dict[str, set] = {p["name"]: set() for p in persons}
    for lr in absent_logs_rows:
        pname = lr["person_name"]
        if pname not in absent_week_sets:
            continue
        try:
            d = datetime.strptime(lr["date"][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        idx = _week_index_for_date(d, weeks)
        if idx is not None and start_idx <= idx <= end_idx:
            absent_week_sets[pname].add(idx)

    persons_out = []
    for p in persons:
        counts = [0] * len(ordered_sections)
        for (pn, sn), n in tally.items():
            if pn != p["name"]:
                continue
            i = name_idx.get(sn)
            if i is not None:
                counts[i] += n
        secondary_total = sum(counts[i] for i in secondary_idx)
        primary_total   = sum(counts[i] for i in primary_child_idx)
        persons_out.append({
            "name": p["name"],
            "counts": counts,
            "secondary_total": secondary_total,
            "primary_total": primary_total,
            "absent_weeks": len(absent_week_sets.get(p["name"], set())),
        })

    return {
        "year": year,
        "sections": ordered_sections,
        "persons": persons_out,
        "total_available_weeks": total_available_weeks,
    }
