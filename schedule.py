#!/usr/bin/env python3
"""
Stage 2: Swapcard meeting scheduler.

Fetches slot availability for all candidates in the candidates table, then solves
a maximum-weight bipartite matching to find the schedule that fits the most
high-priority meetings without conflicts.

Usage:
    python schedule.py

Configuration:
    Copy .env.example to .env and fill in at minimum SWAPCARD_TOKEN and CANDIDATES_FILE.

How to export my_availability.json:
    Open Swapcard → your schedule page → DevTools (F12) → Network tab →
    refresh the page → find requests to api/graphql with "agenda" data →
    right-click → Copy as fetch → save the responses as a JSON array in
    my_availability.json. See README for a step-by-step guide.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv
from scipy.optimize import linear_sum_assignment

load_dotenv()

# ── CONFIG (from .env) ────────────────────────────────────────────────────────

TOKEN = os.getenv("SWAPCARD_TOKEN", "")
EVENT_ID = os.getenv("EVENT_ID", "RXZlbnRfNDQzNjA4NQ==")
DATE_RANGE = {
    "start": os.getenv("DATE_RANGE_START", "2026-05-29T00:00:00+01:00"),
    "end":   os.getenv("DATE_RANGE_END",   "2026-06-01T23:59:59+01:00"),
}
CANDIDATES_FILE = Path(os.getenv("CANDIDATES_FILE", "output/meeting_candidates_latest.md"))
MY_AVAILABILITY_FILE = Path(os.getenv("MY_AVAILABILITY_FILE", "my_availability.json"))
RANKINGS_TO_CONSIDER = {
    int(x) for x in os.getenv("RANKINGS_TO_CONSIDER", "4,5").split(",") if x.strip()
}
PREFER_MEETING_GAP = os.getenv("PREFER_MEETING_GAP", "true").lower() == "true"
MANUAL_BUSY_FILE = Path(os.getenv("MANUAL_BUSY_FILE", "manual_busy.json"))

# ── INTERNALS ─────────────────────────────────────────────────────────────────

_URL = "https://app.swapcard.com/api/graphql"
_HASH = "8be60e0a3635c30fe5574b3a33c4434c22bf6671194c419496f84de47dbae13c"


def _get_headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "x-client-origin": "app.swapcard.com",
        "x-client-platform": "Event App",
        "x-client-version": "2.310.89",
        "x-feature-flags": "fixBackwardPaginationOrder",
    }


def _fetch_slots(person_id: str) -> list[dict]:
    payload = [{
        "operationName": "MeetSlotsQuery",
        "variables": {
            "eventId": EVENT_ID,
            "peopleIds": [person_id],
            "exhibitorIds": [],
            "dateRange": DATE_RANGE,
            "first": 1152,
        },
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": _HASH}
        },
    }]
    r = requests.post(_URL, json=payload, headers=_get_headers(), timeout=15)
    if r.status_code in (401, 403):
        print(
            "\nError: Swapcard token expired or invalid (HTTP {}).".format(r.status_code),
            "\nGet a fresh token:",
            "\n  Open Swapcard in your browser → DevTools (F12) → Network tab →",
            "\n  click any request → Headers → copy the Authorization value.",
            "\nThen update SWAPCARD_TOKEN in your .env file and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)
    r.raise_for_status()
    return r.json()[0]["data"]["event"]["availableMeetingSlots"]["nodes"]


def fetch_all_availability(candidates: list[tuple]) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_slots, pid): (pid, name) for pid, name, _ in candidates}
        for future in as_completed(futures):
            pid, name = futures[future]
            try:
                slots = future.result()
                results[pid] = slots
                print(f"  {name}: {len(slots)} available slots")
            except SystemExit:
                raise
            except Exception as e:
                print(f"  {name}: FAILED ({e})")
                results[pid] = []
    return results


def _overlaps(slot_start: datetime, slot_end: datetime,
               busy: list[tuple[datetime, datetime]]) -> bool:
    return any(slot_start < b_end and slot_end > b_start for b_start, b_end in busy)


def _apply_gap_preference(
    scheduled: list[tuple],
    availability: dict[str, list[dict]],
    my_busy: list[tuple[datetime, datetime]],
) -> list[tuple]:
    """
    Post-processing pass: try to insert a 25-minute gap between back-to-back meetings
    by moving the later meeting to an alternative available slot.  Never drops a meeting.
    Pairs involving a rating-5 person are attempted first.

    Each item in `scheduled`: (start, end, name, weight, slot_dict, pid)
    """
    if len(scheduled) < 2:
        return scheduled

    gap = timedelta(minutes=25)
    scheduled = sorted(scheduled, key=lambda x: x[0])

    # Occupied slot IDs in the current solution
    occupied: set[str] = {s[4]["id"] for s in scheduled}

    # Build a name→available_slots lookup keyed by pid for fast access
    avail_by_pid: dict[str, list[dict]] = {
        s[5]: sorted(availability.get(s[5], []), key=lambda sl: sl["starts"])
        for s in scheduled
    }

    # Identify back-to-back pairs, sorted so rating-5 pairs come first
    pairs_to_try: list[tuple[int, int]] = []
    for i in range(len(scheduled) - 1):
        curr_end = scheduled[i][1]
        next_start = scheduled[i + 1][0]
        if (next_start - curr_end) < gap:
            priority = max(scheduled[i][3], scheduled[i + 1][3])
            pairs_to_try.append((i, priority))
    pairs_to_try.sort(key=lambda x: -x[1])  # highest priority first

    for i, _ in pairs_to_try:
        # Re-check: an earlier swap may have already introduced a gap here
        curr_end = scheduled[i][1]
        next_start = scheduled[i + 1][0]
        if (next_start - curr_end) >= gap:
            continue

        # Determine the latest start time the moved meeting must finish by
        # (either before the meeting after it, or unconstrained if it's last)
        if i + 2 < len(scheduled):
            hard_deadline = scheduled[i + 2][0]
        else:
            hard_deadline = None

        pid = scheduled[i + 1][5]
        name = scheduled[i + 1][2]
        weight = scheduled[i + 1][3]

        for slot in avail_by_pid.get(pid, []):
            slot_start = datetime.fromisoformat(slot["starts"])
            slot_end   = datetime.fromisoformat(slot["ends"])

            # Must be at least one gap after the current meeting ends
            if (slot_start - curr_end) < gap:
                continue
            # Must not push into the next-next meeting
            if hard_deadline is not None and slot_end > hard_deadline:
                continue
            # Must not conflict with my own busy times
            if _overlaps(slot_start, slot_end, my_busy):
                continue
            # Must not already be taken by another person
            if slot["id"] in occupied:
                continue

            # Apply the move
            old_start = scheduled[i + 1][0]
            occupied.discard(scheduled[i + 1][4]["id"])
            occupied.add(slot["id"])
            scheduled[i + 1] = (slot_start, slot_end, name, weight, slot, pid)
            print(f"  [gap] moved {name}: "
                  f"{old_start.strftime('%H:%M')} → {slot_start.strftime('%H:%M')} "
                  f"(gap after {scheduled[i][2]})")
            break  # moved — continue to next pair

    return sorted(scheduled, key=lambda x: x[0])


def solve_schedule(
    candidates: list[tuple],
    availability: dict[str, list[dict]],
    my_busy: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple]:
    my_busy = my_busy or []

    # Build global slot index, excluding slots that overlap my busy times
    all_slot_ids = sorted({
        s["id"]
        for pid, _, _ in candidates
        for s in availability.get(pid, [])
        if not _overlaps(
            datetime.fromisoformat(s["starts"]),
            datetime.fromisoformat(s["ends"]),
            my_busy,
        )
    })
    if not all_slot_ids:
        print("No slots found for any candidate.")
        return []

    slot_index = {sid: i for i, sid in enumerate(all_slot_ids)}
    slot_meta: dict[str, dict] = {}
    for pid, _, _ in candidates:
        for s in availability.get(pid, []):
            slot_meta[s["id"]] = s

    n_people = len(candidates)
    n_slots = len(all_slot_ids)

    # cost[i, j] = relevance weight if person i can attend slot j (and I'm free), else 0
    cost = np.zeros((n_people, n_slots))
    for i, (pid, _, weight) in enumerate(candidates):
        for s in availability.get(pid, []):
            j = slot_index.get(s["id"])
            if j is not None:
                cost[i, j] = weight

    row_ind, col_ind = linear_sum_assignment(-cost)

    scheduled: list[tuple] = []
    unscheduled: list[tuple] = []
    assigned_rows = set(row_ind)

    for i, j in zip(row_ind, col_ind):
        pid, name, weight = candidates[i]
        if cost[i, j] > 0:
            slot = slot_meta[all_slot_ids[j]]
            scheduled.append((
                datetime.fromisoformat(slot["starts"]),
                datetime.fromisoformat(slot["ends"]),
                name, weight, slot, pid,
            ))
        else:
            unscheduled.append((pid, name, weight))

    for i, candidate in enumerate(candidates):
        if i not in assigned_rows:
            unscheduled.append(candidate)

    if PREFER_MEETING_GAP:
        scheduled = _apply_gap_preference(scheduled, availability, my_busy)
    else:
        scheduled.sort()

    print("\n── Optimal Schedule ──────────────────────────────────────────────")
    for start, end, name, weight, _, _ in scheduled:
        day  = start.strftime("%a %d %b")
        time = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        print(f"  {day}  {time}   {name}  (priority {weight})")

    total        = sum(w for _, _, _, w, _, _ in scheduled)
    max_possible = sum(w for _, _, w in candidates)
    print(f"\nScheduled: {len(scheduled)}/{len(candidates)} meetings")
    print(f"Weight achieved: {total}/{max_possible}")

    if unscheduled:
        print("\nCould not fit into schedule:")
        scheduled_slot_ids = {s[4]["id"] for s in scheduled}
        for pid, name, weight in sorted(unscheduled, key=lambda x: -x[2]):
            raw = availability.get(pid, [])
            free = [s for s in raw if not _overlaps(
                datetime.fromisoformat(s["starts"]),
                datetime.fromisoformat(s["ends"]),
                my_busy,
            )]
            taken = sum(1 for s in free if s["id"] in scheduled_slot_ids)
            if not raw:
                reason = "no slots returned by API"
            elif not free:
                reason = "all their slots overlap your busy times"
            elif taken == len(free):
                reason = f"all {len(free)} free slot(s) taken by other meetings"
            else:
                reason = f"{len(free) - taken} free slot(s) left but couldn't be fitted"
            print(f"  {name} (priority {weight}): {reason}")

    return scheduled


def parse_manual_busy(entries: list[str]) -> list[tuple[datetime, datetime]]:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/London")
    result = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            date_part, time_part = entry.split()
            start_str, end_str = time_part.split("-")
            month, day = map(int, date_part.split("-"))
            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            result.append((
                datetime(2026, month, day, sh, sm, tzinfo=tz),
                datetime(2026, month, day, eh, em, tzinfo=tz),
            ))
        except Exception:
            print(f"Warning: could not parse MANUAL_BUSY entry '{entry}' — skipping.")
    return result


def load_my_busy_slots(path: Path) -> list[tuple[datetime, datetime]]:
    """
    Parse busy intervals from the Swapcard my-schedule GraphQL export.

    Busy = scheduled sessions/talks (Core_Planning) + meeting requests you sent
           (Core_UserMeetingBooked where you are the organizer).
    Incoming meeting requests from others are left as free so you can be
    double-booked by the optimiser — you can decline/reschedule those later.

    Processes ALL agenda-containing responses in the JSON array (not just the first),
    then deduplicates by (start, end) to handle multi-session captures.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    seen: set[tuple] = set()
    busy: list[tuple[datetime, datetime]] = []

    for response in data:
        agenda = response.get("data", {}).get("agenda")
        if not agenda:
            continue

        for item in agenda:
            typename = item.get("__typename")
            try:
                start = datetime.fromisoformat(item.get("starts") or item.get("beginsAt"))
                end   = datetime.fromisoformat(item.get("ends")   or item.get("endsAt"))
            except (TypeError, ValueError):
                continue

            add = False
            if typename == "Core_Planning":
                add = True
            elif typename == "Core_UserMeetingBooked":
                mtg = item.get("meeting") or {}
                org_id  = (mtg.get("organizer") or {}).get("id")
                part_id = (mtg.get("userParticipant") or {}).get("id")
                if org_id and org_id == part_id:
                    add = True

            if add:
                key = (start, end)
                if key not in seen:
                    seen.add(key)
                    busy.append((start, end))

    return busy


def load_candidates(path: Path, rankings: set[int]) -> list[tuple]:
    """
    Parse the candidates markdown table.
    Expected columns: Name | Link | Description | Relevance | Swapcard ID
    Accepts any non-empty Swapcard ID (both CommunityProfile and EventPeople formats work).
    """
    candidates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) != 5:
            continue
        name, _, _, ranking_cell, pid_cell = cols
        try:
            ranking = int(ranking_cell)
        except ValueError:
            continue
        pid = pid_cell.strip()
        if not pid:
            continue
        if ranking in rankings:
            candidates.append((pid, name, ranking))
    return candidates


def main() -> None:
    if not TOKEN:
        print(
            "Error: SWAPCARD_TOKEN is not set.\n"
            "Add it to .env (copy .env.example as a starting point).\n"
            "Get a fresh token: open Swapcard → DevTools → Network tab → "
            "any request → Authorization header.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not CANDIDATES_FILE.exists():
        print(
            f"Error: candidates file not found: {CANDIDATES_FILE}\n"
            "Run recommend.py first to generate it, or set CANDIDATES_FILE in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    candidates = load_candidates(CANDIDATES_FILE, RANKINGS_TO_CONSIDER)
    if not candidates:
        print(
            f"No candidates found in {CANDIDATES_FILE} "
            f"with rankings {sorted(RANKINGS_TO_CONSIDER)}.\n"
            "Check RANKINGS_TO_CONSIDER in .env matches the scores in your candidates file.",
            file=sys.stderr,
        )
        sys.exit(1)

    my_busy: list[tuple[datetime, datetime]] = []
    if MY_AVAILABILITY_FILE.exists():
        my_busy = load_my_busy_slots(MY_AVAILABILITY_FILE)
        print(f"Loaded {len(my_busy)} busy intervals from your agenda.")
    else:
        print(f"Warning: {MY_AVAILABILITY_FILE} not found — not filtering by your availability.")

    manual_entries: list[str] = []
    if MANUAL_BUSY_FILE.exists():
        manual_entries = json.loads(MANUAL_BUSY_FILE.read_text(encoding="utf-8"))
    else:
        print(f"Note: {MANUAL_BUSY_FILE} not found — no manual busy intervals loaded.")
        print(f"  Copy manual_busy.example.json → manual_busy.json and fill it in.")
    manual = parse_manual_busy(manual_entries)
    if manual:
        print(f"Loaded {len(manual)} manual busy intervals.")
        my_busy.extend(manual)

    print(f"Loaded {len(candidates)} candidates (rankings {sorted(RANKINGS_TO_CONSIDER)}).")
    print("Fetching availability...")
    availability = fetch_all_availability(candidates)
    solve_schedule(candidates, availability, my_busy)


if __name__ == "__main__":
    main()
