#!/usr/bin/env python3
"""
Stage 1: Generate meeting recommendations.

Two-call LLM pipeline:
  1. Build a semantic search query + keyword lists from the requester's profile.
  2. Pick the best N people from the retrieved candidate pool and rate them 1-5.

Outputs two files to OUTPUT_DIR:
  - recommendations_<slug>_<date>.md  — detailed cards, rating 5 first
  - meeting_candidates_<slug>_<date>.md  — table ready for schedule.py

Usage:
    python recommend.py [requester_context.json]

requester_context.json fields:
    name          — full name as it appears in the Swapcard sheet
    swapcard_id   — your attendee ID (null → auto-discovered from name search)
    extra_context — bio, website text, current projects; anything relevant
    goals         — what you specifically want from this conference
"""

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
_LLM_MODEL_OVERRIDE = os.getenv("LLM_MODEL", "").strip()
NUM_RECOMMENDATIONS = int(os.getenv("NUM_RECOMMENDATIONS", "25"))
RECALL_TOP = int(os.getenv("RECALL_TOP", "150"))
RECALL_TOP_LATERAL = int(os.getenv("RECALL_TOP_LATERAL", "40"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))

PROVIDER_DEFAULTS = {
    "anthropic": "claude-sonnet-4-6",
    "gemini":    "gemini-2.5-flash",
    "openai":    "gpt-5.5",
}

REPO_ROOT = Path(__file__).parent

# Country name → flag emoji (common EA countries; falls back to empty string)
_FLAGS: dict[str, str] = {
    "United Kingdom": "🇬🇧", "UK": "🇬🇧", "England": "🇬🇧", "Scotland": "🇬🇧", "Wales": "🇬🇧",
    "United States": "🇺🇸", "USA": "🇺🇸", "US": "🇺🇸",
    "Germany": "🇩🇪", "France": "🇫🇷", "Netherlands": "🇳🇱", "Sweden": "🇸🇪",
    "Switzerland": "🇨🇭", "Norway": "🇳🇴", "Denmark": "🇩🇰", "Finland": "🇫🇮",
    "Austria": "🇦🇹", "Belgium": "🇧🇪", "Spain": "🇪🇸", "Italy": "🇮🇹",
    "Poland": "🇵🇱", "Czech Republic": "🇨🇿", "Hungary": "🇭🇺",
    "Canada": "🇨🇦", "Australia": "🇦🇺", "New Zealand": "🇳🇿",
    "India": "🇮🇳", "Singapore": "🇸🇬", "Japan": "🇯🇵", "China": "🇨🇳",
    "Brazil": "🇧🇷", "Mexico": "🇲🇽", "Argentina": "🇦🇷", "Chile": "🇨🇱",
    "South Africa": "🇿🇦", "Kenya": "🇰🇪", "Nigeria": "🇳🇬", "Ethiopia": "🇪🇹",
    "Israel": "🇮🇱", "UAE": "🇦🇪", "Turkey": "🇹🇷",
    "Ireland": "🇮🇪", "Portugal": "🇵🇹", "Greece": "🇬🇷",
    "Romania": "🇷🇴", "Ukraine": "🇺🇦",
}


# ── LLM ABSTRACTION ───────────────────────────────────────────────────────────

def _get_model() -> str:
    return _LLM_MODEL_OVERRIDE or PROVIDER_DEFAULTS.get(LLM_PROVIDER, "")


def call_llm(system: str, user: str, max_tokens: int = 8192) -> str:
    """Call the configured LLM provider and return the raw text response."""
    model = _get_model()
    if not model:
        print(f"Error: unknown LLM_PROVIDER '{LLM_PROVIDER}'. Use anthropic, gemini, or openai.",
              file=sys.stderr)
        sys.exit(1)

    if LLM_PROVIDER == "anthropic":
        try:
            import anthropic
        except ImportError:
            print("Error: anthropic package not installed. Run: pip install anthropic",
                  file=sys.stderr)
            sys.exit(1)
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            print("Error: ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
            sys.exit(1)
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text

    elif LLM_PROVIDER == "openai":
        try:
            from openai import OpenAI
        except ImportError:
            print("Error: openai package not installed. Run: pip install openai", file=sys.stderr)
            sys.exit(1)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print("Error: OPENAI_API_KEY not set in .env", file=sys.stderr)
            sys.exit(1)
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content

    elif LLM_PROVIDER == "gemini":
        try:
            import google.generativeai as genai
        except ImportError:
            print("Error: google-generativeai not installed. Run: pip install google-generativeai",
                  file=sys.stderr)
            sys.exit(1)
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY not set in .env", file=sys.stderr)
            sys.exit(1)
        genai.configure(api_key=api_key)
        gemini_model = genai.GenerativeModel(
            model_name=model,
            generation_config={"response_mime_type": "application/json",
                               "max_output_tokens": max_tokens},
        )
        resp = gemini_model.generate_content(f"{system}\n\n{user}")
        return resp.text

    else:
        print(f"Error: unknown LLM_PROVIDER '{LLM_PROVIDER}'.", file=sys.stderr)
        sys.exit(1)


def parse_json_response(raw: str, context: str = "") -> dict | list:
    """Parse JSON from an LLM response, stripping markdown code fences if present."""
    text = raw.strip()
    # Strip ```json ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Error: could not parse LLM JSON response{' (' + context + ')' if context else ''}.",
              file=sys.stderr)
        print(f"Parse error: {e}", file=sys.stderr)
        print("Raw response (first 500 chars):", raw[:500], file=sys.stderr)
        sys.exit(1)


# ── NPM SUBPROCESS HELPERS ────────────────────────────────────────────────────

def run_npm(args: list[str], context: str = "") -> str:
    """Run an npm script and return stdout. Exits on failure."""
    result = subprocess.run(
        ["npm", "run", "--silent"] + args,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "No embedding cache" in stderr or "cache is stale" in stderr:
            print(
                "\nError: the embedding cache is missing or stale.\n"
                "Build it first (takes a few minutes on first run):\n"
                "  npm run embed\n"
                "Then re-run recommend.py.",
                file=sys.stderr,
            )
        else:
            print(f"Error running npm {' '.join(args)}{' (' + context + ')' if context else ''}:",
                  file=sys.stderr)
            print(stderr or result.stdout, file=sys.stderr)
        sys.exit(1)
    return result.stdout


def _brief_display(a: dict) -> str:
    """Name from either a brief search result ('name') or a full profile ('firstName'/'lastName')."""
    return (a.get("name")
            or f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
            or a.get("id", "?"))


def find_requester(name: str, swapcard_id: str | None) -> tuple[str, dict]:
    """
    Return (attendee_id, full_profile_dict) for the requester.

    swapcard_id here is the TOOL's internal attendee ID (e.g. 'jakub-krys-6dc475a704'),
    shown in search results.  It is NOT the Swapcard community-profile URL ID.
    Leave null to auto-discover from the name search.
    """
    if swapcard_id:
        raw = run_npm(["find", "--", "--id", swapcard_id, "--json"], "profile lookup")
        data = parse_json_response(raw, "requester profile")
        profile = (data[0] if isinstance(data, list) else data) if data else None
        if not profile:
            print(
                f"Error: no attendee found with id '{swapcard_id}'.\n"
                "Use the tool's internal ID shown in name-search results "
                "(e.g. 'jakub-krys-6dc475a704'), NOT the Swapcard URL ID.",
                file=sys.stderr,
            )
            sys.exit(1)
        return swapcard_id, profile

    # Name search returns brief objects — different schema from full profiles
    raw = run_npm(["find", "--", name, "--json"], "name search")
    matches = parse_json_response(raw, "name search")

    if not matches:
        print(
            f"Error: no attendee found matching '{name}'.\n"
            "Check the spelling matches the Swapcard sheet exactly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(matches) == 1:
        brief = matches[0]
        attendee_id = brief["id"]
        print(f"Found: {_brief_display(brief)} — "
              f"{brief.get('jobTitle', '?')}, {brief.get('company', '?')} [{attendee_id}]")
        print(f"  Tip: set \"swapcard_id\": \"{attendee_id}\" in requester_context.json "
              f"to skip this search next time.")
    else:
        # Multiple matches — ask user to pick
        print(f"Multiple matches for '{name}':")
        for i, a in enumerate(matches, 1):
            print(f"  {i}. {_brief_display(a)} — "
                  f"{a.get('jobTitle', '?')}, {a.get('company', '?')} [{a['id']}]")
        print(
            "\nAdd the correct 'swapcard_id' to your requester_context.json and re-run.\n"
            "Use the ID shown above (e.g. 'jakub-krys-6dc475a704'), "
            "NOT the Swapcard URL ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Fetch full profile (biography, expertise, needHelp, etc.) via --id lookup
    full_raw = run_npm(["find", "--", "--id", attendee_id, "--json"], "full profile")
    full_data = parse_json_response(full_raw, "full profile")
    # --id with a single ID returns either a list or a single object depending on the script
    if isinstance(full_data, list):
        full_profile = full_data[0] if full_data else None
    else:
        full_profile = full_data if full_data else None
    if not full_profile:
        print(f"Error: could not fetch full profile for id '{attendee_id}'.", file=sys.stderr)
        sys.exit(1)
    return attendee_id, full_profile


def format_profile_for_llm(p: dict) -> str:
    """Render a full Attendee dict as a compact text block for LLM context."""
    lines = []
    name = f"{p.get('firstName', '')} {p.get('lastName', '')}".strip()
    meta = filter(None, [p.get("jobTitle"), p.get("company") and f"at {p['company']}",
                          p.get("country") and f"({p['country']})", p.get("careerStage")])
    lines.append(f"**{name}** — {', '.join(meta)}")
    if p.get("biography"):
        lines.append(f"Bio: {p['biography']}")
    if p.get("expertise"):
        lines.append(f"Expertise: {'; '.join(p['expertise'])}")
    if p.get("interests"):
        lines.append(f"Interests: {'; '.join(p['interests'])}")
    if p.get("helpOthers"):
        lines.append(f"Offers: {p['helpOthers']}")
    if p.get("needHelp"):
        lines.append(f"Seeking: {p['needHelp']}")
    return "\n".join(lines)


# ── LLM CALL 1: build search query ───────────────────────────────────────────

_QUERY_SYSTEM = """\
You are a conference matchmaking assistant. Given an attendee's profile and their
goals for the conference, produce a JSON object with exactly these keys:

"query"   — 2-5 sentence prose paragraph describing who this person should meet
            AND who would benefit from meeting them. Be concrete: name sub-fields,
            types of role, problems they're working on. This drives semantic search.

"wanted"  — list of 15-30 short (1-3 word) keywords/phrases: jargon, sub-fields,
            methodologies, and especially specific organisation or product names
            (e.g. "GovAI", "METR", "Eclypsium"). These are exact-term BM25 hits.
            Avoid generic filler like "AI", "research", "effective", "global".

"lateral" — list of 12-25 short keywords that are DELIBERATELY off-axis: adjacent
            or orthogonal domains that share the same underlying problem but use
            entirely different vocabulary. The test: these should surface people
            the requester would never think to search for. No near-synonyms of
            "wanted" terms.

Return ONLY a valid JSON object, no other text or markdown fences.\
"""


def build_search_query(requester_profile: dict, extra_context: str, goals: str) -> dict:
    print("  [LLM call 1/2] Building search query...")
    user = (
        f"Requester profile:\n{format_profile_for_llm(requester_profile)}\n\n"
        f"Additional context:\n{extra_context}\n\n"
        f"Conference goals:\n{goals}"
    )
    raw = call_llm(_QUERY_SYSTEM, user, max_tokens=1024)
    result = parse_json_response(raw, "search query")
    if not isinstance(result, dict) or "query" not in result:
        print("Error: LLM did not return the expected query structure.", file=sys.stderr)
        sys.exit(1)
    return result


# ── LLM CALL 2: pick recommendations ─────────────────────────────────────────

_RECS_SYSTEM_TEMPLATE = """\
You are a conference matchmaking expert. Your task is to pick the {n} best people
for the requester to meet at this conference and rate each one 1-5.

Rating guide:
  5 — strongest direct match; clear mutual fit (they need what you offer, you need what they offer)
  4 — strong on one important dimension, or clearly adjacent sub-field
  3 — relevant but more generic overlap, or an interesting lateral connection
  2 — weak or speculative link, worth a quick coffee
  1 — long-shot; include only to round out the list if needed

For each pick, produce a JSON object with these exact keys:
  name            — full name
  role            — job title
  company         — organisation
  country         — country name as in their profile
  rating          — integer 1-5
  why             — 60-90 word paragraph citing SPECIFIC phrases from both profiles.
                    Acknowledge mutual fit where visible. No filler like
                    "this would be a great opportunity".
  talking_points  — list of exactly 3 short, open-ended prompts the requester can lead with.
                    Specific to this person, not generic.
  suggested_opener — one or two sentences the requester can paste into Swapcard when
                     requesting the meeting. Warm but professional; names a concrete reason;
                     ends with a request for a 30-minute slot.
  swapcard_url    — their Swapcard profile URL (from their profile data, empty string if absent)
  linkedin_url    — their LinkedIn URL (from their profile data, empty string if absent)

Diversity rules:
  - At most 1 person per organisation across all picks. You may make one exception
    (2 from one org) only when both would have clearly distinct conversations.
  - Spread country, seniority, and sub-field. A varied list beats a tight monoculture.
  - Do NOT recommend the requester themselves.

Return ONLY a valid JSON object with a single key "recommendations" whose value is
an array of the pick objects. No markdown, no other text.\
"""


def pick_recommendations(
    requester_profile: dict,
    extra_context: str,
    goals: str,
    all_profiles: list[dict],
    recall_data: dict,
    n_recs: int,
) -> list[dict]:
    print(f"  [LLM call 2/2] Picking {n_recs} recommendations from "
          f"{len(all_profiles)} candidate profiles...")

    # Build candidate text, annotating each with their retrieval channel
    id_to_meta: dict[str, dict] = {}
    for c in recall_data.get("candidates", []):
        id_to_meta[c["id"]] = {"pool": "primary",
                                "sem_rank": c.get("semRank"), "bm25_rank": c.get("bm25Rank")}
    for c in recall_data.get("lateralCandidates", []):
        id_to_meta[c["id"]] = {"pool": "lateral",
                                "sem_rank": c.get("semRank"), "bm25_rank": c.get("bm25Rank")}

    candidate_blocks = []
    for p in all_profiles:
        meta = id_to_meta.get(p["id"], {})
        pool_tag = f"[{meta.get('pool', 'primary')}]"
        candidate_blocks.append(f"{pool_tag}\n{format_profile_for_llm(p)}")

    system = _RECS_SYSTEM_TEMPLATE.format(n=n_recs)
    user = (
        f"Requester profile:\n{format_profile_for_llm(requester_profile)}\n\n"
        f"Requester's additional context:\n{extra_context}\n\n"
        f"Requester's conference goals:\n{goals}\n\n"
        f"Retrieved candidates ({len(all_profiles)} total; "
        f"[primary] = strong match, [lateral] = serendipitous):\n\n"
        + "\n\n---\n\n".join(candidate_blocks)
    )
    raw = call_llm(system, user, max_tokens=8192)
    result = parse_json_response(raw, "recommendations")
    if isinstance(result, dict) and "recommendations" in result:
        recs = result["recommendations"]
    elif isinstance(result, list):
        recs = result
    else:
        print("Error: unexpected recommendations structure from LLM.", file=sys.stderr)
        sys.exit(1)

    # Validate and normalise
    valid = []
    for r in recs:
        if not isinstance(r, dict) or "name" not in r or "rating" not in r:
            continue
        r["rating"] = int(r.get("rating", 3))
        r.setdefault("why", "")
        r.setdefault("talking_points", [])
        r.setdefault("suggested_opener", "")
        r.setdefault("swapcard_url", "")
        r.setdefault("linkedin_url", "")
        valid.append(r)

    return valid


# ── OUTPUT RENDERING ──────────────────────────────────────────────────────────

def _flag(country: str) -> str:
    return _FLAGS.get(country, "")


def render_detailed_md(
    requester_name: str,
    requester_profile: dict,
    extra_context: str,
    goals: str,
    recommendations: list[dict],
    recall_data: dict,
) -> str:
    lines: list[str] = []
    lines += [
        f"# EAG London 2026 — Recommendations: {requester_name}",
        f"*Generated {date.today().isoformat()} · "
        f"{recall_data.get('totalAttendees', '?')} attendees in sheet · "
        f"{len(recall_data.get('candidates', []))} primary + "
        f"{len(recall_data.get('lateralCandidates', []))} lateral candidates retrieved*",
        "",
        "## Requester context",
        "",
        format_profile_for_llm(requester_profile),
        "",
        f"**Goals:** {goals}",
        "",
        f"**Additional context:** {extra_context}",
        "",
        "---",
        "",
    ]

    # Group by rating, highest first
    from collections import defaultdict
    by_rating: dict[int, list[dict]] = defaultdict(list)
    for rec in recommendations:
        by_rating[rec["rating"]].append(rec)

    for rating in range(5, 0, -1):
        if rating not in by_rating:
            continue
        lines += [f"## Rating {rating}", ""]
        for rec in by_rating[rating]:
            flag = _flag(rec.get("country", ""))
            header = f"### {rec['name']} — {rec.get('role', '?')}, {rec.get('company', '?')} {flag}"
            lines += [header.strip(), ""]
            lines += [rec.get("why", ""), ""]
            lines += ["**Talking points:**"]
            for tp in rec.get("talking_points", []):
                lines.append(f"- {tp}")
            lines += [
                "",
                f"**Suggested opener:** {rec.get('suggested_opener', '')}",
                "",
            ]
            links = []
            if rec.get("swapcard_url"):
                links.append(f"[Swapcard profile]({rec['swapcard_url']})")
            if rec.get("linkedin_url"):
                links.append(f"[LinkedIn]({rec['linkedin_url']})")
            if links:
                lines += [" · ".join(links), ""]
            lines += ["---", ""]

    return "\n".join(lines)


def _extract_swapcard_id(url: str) -> str:
    """Extract the last path segment from a Swapcard profile URL."""
    if not url:
        return ""
    return url.rstrip("/").split("/")[-1]


def render_candidates_table(
    requester_name: str,
    recommendations: list[dict],
    profile_map: dict[str, dict],
) -> str:
    """
    Render the 5-column candidates table consumed by schedule.py.
    Swapcard IDs are extracted from profile URLs (both CommunityProfile and EventPeople
    formats are accepted by the scheduler API).
    """
    lines: list[str] = [
        f"# EAG London 2026 — Meeting Candidates: {requester_name}",
        f"*Generated {date.today().isoformat()}*",
        "*Review this file, adjust Relevance scores, add/remove people, then run schedule.py.*",
        "",
        "| Name | Link | Description | Relevance | Swapcard ID |",
        "|------|------|-------------|-----------|-------------|",
    ]

    for rec in sorted(recommendations, key=lambda r: -r["rating"]):
        name = rec["name"].replace("|", "\\|")
        swapcard_url = rec.get("swapcard_url", "")
        swapcard_id = _extract_swapcard_id(swapcard_url)
        link = f"[Swapcard]({swapcard_url})" if swapcard_url else ""

        # One-line description: first clause of "why" (up to ~80 chars)
        why = rec.get("why", "")
        desc = re.split(r"[.;]", why)[0].strip()[:80].replace("|", "\\|") if why else ""

        lines.append(f"| {name} | {link} | {desc} | {rec['rating']} | {swapcard_id} |")

    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main() -> None:
    context_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("requester_context.json")
    if not context_file.exists():
        print(
            f"Error: context file not found: {context_file}\n"
            "Copy requester_context.example.json → requester_context.json and fill it in.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Allow JS-style comments in the JSON (strip them before parsing)
    raw_json = context_file.read_text(encoding="utf-8")
    raw_json = re.sub(r"//[^\n]*", "", raw_json)  # remove // comments
    ctx = json.loads(raw_json)

    requester_name = ctx.get("name", "").strip()
    if not requester_name:
        print("Error: 'name' field is missing in requester context.", file=sys.stderr)
        sys.exit(1)

    extra_context = ctx.get("extra_context", "").strip() or "(none provided)"
    goals = ctx.get("goals", "").strip() or "(none provided)"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n=== Recommendation run for: {requester_name} ===\n")

    # ── Step 1: find the requester in the sheet ───────────────────────────────
    print("Step 1/4  Finding requester in attendee sheet...")
    requester_id, requester_profile = find_requester(requester_name, ctx.get("swapcard_id"))

    # ── Step 2: build search query via LLM ───────────────────────────────────
    print("Step 2/4  Building search query...")
    query_data = build_search_query(requester_profile, extra_context, goals)
    print(f"  Query: {query_data['query'][:120]}...")
    print(f"  Wanted keywords ({len(query_data.get('wanted', []))}): "
          f"{', '.join(query_data.get('wanted', [])[:8])}...")
    print(f"  Lateral keywords ({len(query_data.get('lateral', []))}): "
          f"{', '.join(query_data.get('lateral', [])[:6])}...")

    # ── Step 3: retrieve candidates via hybrid search ─────────────────────────
    print(f"Step 3/4  Retrieving candidates (top {RECALL_TOP} primary + "
          f"{RECALL_TOP_LATERAL} lateral)...")
    recall_raw = run_npm([
        "recall", "--", requester_id,
        "--query", query_data["query"],
        "--wanted", ", ".join(query_data.get("wanted", [])),
        "--lateral", ", ".join(query_data.get("lateral", [])),
        "--top", str(RECALL_TOP),
        "--top-lateral", str(RECALL_TOP_LATERAL),
        "--json",
    ], "recall")
    recall_data = parse_json_response(recall_raw, "recall")

    all_ids = [c["id"] for c in recall_data.get("candidates", [])] + \
              [c["id"] for c in recall_data.get("lateralCandidates", [])]
    # Remove duplicates while preserving order
    seen_ids: set[str] = set()
    unique_ids = [i for i in all_ids if not (i in seen_ids or seen_ids.add(i))]

    print(f"  Retrieved {len(recall_data.get('candidates', []))} primary + "
          f"{len(recall_data.get('lateralCandidates', []))} lateral candidates.")
    print(f"  Fetching full profiles ({len(unique_ids)} attendees)...")
    profiles_raw = run_npm(
        ["find", "--", "--id", ",".join(unique_ids), "--json"],
        "full profiles"
    )
    profiles_data = parse_json_response(profiles_raw, "profiles")
    all_profiles: list[dict] = profiles_data if isinstance(profiles_data, list) else [profiles_data]
    profile_map = {p["id"]: p for p in all_profiles}
    print(f"  Loaded {len(all_profiles)} full profiles.")

    # ── Step 4: pick recommendations ─────────────────────────────────────────
    print(f"Step 4/4  Picking {NUM_RECOMMENDATIONS} recommendations...")
    recommendations = pick_recommendations(
        requester_profile, extra_context, goals,
        all_profiles, recall_data, NUM_RECOMMENDATIONS,
    )
    print(f"  Got {len(recommendations)} recommendations.")

    # ── Write output files ────────────────────────────────────────────────────
    slug = re.sub(r"[^a-z0-9]+", "_", requester_name.lower()).strip("_")
    today = date.today().isoformat()

    detailed_path = OUTPUT_DIR / f"recommendations_{slug}_{today}.md"
    candidates_path = OUTPUT_DIR / f"meeting_candidates_{slug}_{today}.md"
    latest_path = OUTPUT_DIR / "meeting_candidates_latest.md"

    detailed_path.write_text(
        render_detailed_md(requester_name, requester_profile, extra_context,
                           goals, recommendations, recall_data),
        encoding="utf-8",
    )
    candidates_md = render_candidates_table(requester_name, recommendations, profile_map)
    candidates_path.write_text(candidates_md, encoding="utf-8")
    latest_path.write_text(candidates_md, encoding="utf-8")

    rating_counts = {}
    for r in recommendations:
        rating_counts[r["rating"]] = rating_counts.get(r["rating"], 0) + 1

    print(f"\n✓ Done.")
    print(f"  Detailed recommendations → {detailed_path}")
    print(f"  Candidates table         → {candidates_path}")
    print(f"  (also symlinked to)      → {latest_path}")
    print(f"\n  Rating breakdown: " +
          " | ".join(f"{r}★ ×{c}" for r, c in sorted(rating_counts.items(), reverse=True)))
    print(f"\nNext steps:")
    print(f"  1. Read {detailed_path}")
    print(f"  2. Edit {candidates_path} — adjust ratings, add/remove people")
    print(f"  3. Add your availability: export my_availability.json (see README)")
    print(f"  4. Run: python schedule.py")


if __name__ == "__main__":
    main()
