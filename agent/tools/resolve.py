"""Name resolution: ranks project and person candidates for the LLM to judge.

Moved verbatim out of the old single-file agent/tools.py.
"""

import re

from agent.tools.access import _accessible_project_ids
from agent.tools.db import projects_table
from agent.tools.registry import register_tool


import unicodedata

RECALL_FLOOR = 0.35     # noise cutoff only -- NOT a match/no-match decision
MAX_CANDIDATES = 8      # keeps the prompt bounded on big workspaces

# Above this, and with no rival candidate, the id is exposed at the top level
# so the next step can chain to it. Below it the candidate is still returned
# for the model to consider -- it simply is not handed an id to act on.
#
# This is NOT a match/no-match verdict either: it is how much evidence is
# required before a MACHINE may act without the model choosing. Measured on a
# real workspace, mistyped real names score 0.762 and up, while names of
# projects that do not exist top out at 0.400.
AUTO_CHAIN_FLOOR = 0.6


def _norm_name(s: str) -> str:
    """Lowercase, strip accents and collapse whitespace/punctuation."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize('NFD', s)
    s = "".join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _edits(a: str, b: str) -> int:
    """Levenshtein distance. Small strings, so the plain DP is plenty."""
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _similarity(query: str, candidate: str) -> float:
    """How close two names look, 0.0..1.0. A ranking signal, not a verdict.

    1.0 means the query appears verbatim inside the candidate (or vice
    versa). Below that, the score is the share of the query that did NOT have
    to be corrected, measured against the whole candidate and against every
    same-length window of its tokens -- so a mistyped surname still ranks
    "Farmacia Haussman" highly without the leading word counting against it.

    Deliberately does no interpreting: no digit rules, no nickname table, no
    threshold. Two names that look alike score alike, and the caller decides
    what that means.
    """
    q, c = _norm_name(query), _norm_name(candidate)
    if not q or not c:
        return 0.0
    if q in c or c in q:
        return 1.0

    c_tokens = c.split()
    n = len(q.split())
    targets = [c] + [" ".join(c_tokens[i:i + n]) for i in range(len(c_tokens) - n + 1)]
    dist = min(_edits(q, t) for t in targets)
    return round(max(0.0, 1.0 - dist / len(q)), 3)


RESOLVE_ENTITY_DESC = """resolve_entity(query)
    - Looks up ANY named thing the user mentions -- a PROJECT or a PERSON --
      in a single pass, tolerating typos.
    - Returns RANKED CANDIDATES, not an answer. YOU decide which one (if any)
      the user meant, using meaning the score cannot see.
"""


@register_tool("resolve_entity")
def resolve_entity(query: str) -> dict:
    """Ranks projects and people by name similarity to `query`. Decides nothing.

    resolve_person only ever looked inside participants, so a project name was
    invisible to it and the agent answered "not found" for projects that
    plainly exist. This searches both in one pass and returns everything that
    is even plausibly related, ranked, for the LLM to judge.

    Returns:
      - candidates: ranked desc, each {type: "project"|"person", name, score,
        plus projectId/description/status or email/phone/role/projects}.
      - exact: a candidate whose name contains the query verbatim, if any.
        This is a FACT, safe to act on without asking. When it is None but a
        single candidate came back, the id fields below still point at that
        candidate -- usable, but say out loud which name you resolved to.
      - count / dropped_below_floor: how many were returned vs filtered as noise.

    The caller is expected to pick. A score near 1.0 that is not `exact` is a
    likely typo worth confirming; two close scores mean genuine ambiguity.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "Provide a name to look up", "count": 0, "candidates": []}
    try:
        pids = _accessible_project_ids()
        scored, people = [], {}
        dropped = 0

        for pid in pids:
            proj = projects_table.get_item(Key={'projectId': pid}).get('Item') or {}
            pname = proj.get('name', '') or ''

            score = _similarity(q, pname)
            if score >= RECALL_FLOOR:
                entry = {
                    "type": "project", "name": pname, "score": score,
                    "projectId": pid,
                    "description": proj.get('description', '') or '',
                    "status": proj.get('status', '') or '',
                    "participantsCount": len(proj.get('participants') or []),
                }
                # Whether this project already has a Trello board travels WITH
                # the project. Asked to "create a Trello board for X", the
                # planner used to propose it -- and ask the user to confirm --
                # without ever checking, because the only thing that knew was
                # create_trello_board itself, which does not run until AFTER
                # the confirmation. Consent was being requested for something
                # already known to be wrong.
                if proj.get('trelloListId'):
                    entry["trelloBoard"] = {
                        "boardName": proj.get('trelloBoardName', '') or '',
                        "listName": proj.get('trelloListName', '') or '',
                        "note": "This project ALREADY has a Trello board. Do "
                                "not propose creating another one; a project "
                                "has ONE board. To send tasks, call "
                                "push_tasks_to_trello with no board or list.",
                    }
                else:
                    entry["trelloBoard"] = None
                scored.append(entry)
            elif pname:
                dropped += 1

            for p in (proj.get('participants') or []):
                if not isinstance(p, dict):
                    continue
                pn = (p.get('name', '') or '').strip()
                if not pn:
                    continue
                pscore = _similarity(q, pn)
                if pscore < RECALL_FLOOR:
                    dropped += 1
                    continue
                email = (p.get('email', '') or '').strip().lower()
                tel = (p.get('phone', '') or '').strip()
                key = email or tel or _norm_name(pn)
                if key not in people:
                    people[key] = {
                        "type": "person", "name": pn, "score": pscore,
                        "email": email, "phone": tel,
                        "role": (p.get('role', '') or ''), "projects": [],
                    }
                    scored.append(people[key])
                entry = people[key]
                entry["score"] = max(entry["score"], pscore)
                if email and not entry["email"]:
                    entry["email"] = email
                if tel and not entry["phone"]:
                    entry["phone"] = tel
                entry["projects"].append({"projectId": pid, "projectName": pname})

        scored.sort(key=lambda m: m["score"], reverse=True)
        if len(scored) > MAX_CANDIDATES:
            dropped += len(scored) - MAX_CANDIDATES
            scored = scored[:MAX_CANDIDATES]

        result = {
            "query": query,
            "count": len(scored),
            "candidates": scored,
            "dropped_below_floor": dropped,
        }

        # An exact containment hit is a fact, not a judgement, so we surface it
        # separately: the caller can act on it without confirming.
        exact = [m for m in scored if m["score"] >= 1.0]
        result["exact"] = exact[0] if len(exact) == 1 else None

        # Deterministic top-level fields for {"from_step": N, "extract": ...}
        # chaining. ONE candidate is not enough on its own: "Panaderia Lopez"
        # -- a project that does not exist -- scored 0.4 against "Plataforma
        # de aprendizaje en linea", came back as the single candidate, and the
        # planner chained its id and listed 11 tasks of an unrelated project.
        # With a write tool instead of a read, that is a write to the wrong
        # project.
        #
        # So the id is handed over only when the evidence is strong. Measured
        # against this workspace, the two populations separate cleanly:
        #   legitimate typos      0.762 .. 1.000   ("Famarcia Hussman" = 0.812)
        #   names that do not exist   up to 0.400
        # AUTO_CHAIN_FLOOR sits in that gap. Below it the candidate is still
        # RETURNED -- the model can read it and ask "did you mean...?" -- it
        # just does not get a machine-usable id for free.
        #
        # Disclosure is still required above the floor: "exact" is None for a
        # corrected name, and the narrator must say which name it used.
        # ONE STRONG candidate and no strong rival. Requiring a single
        # candidate outright was too brittle: "Plataforma de aprendisaje"
        # scores 0.960 on the right project and drags in a 0.360 neighbour
        # that is plainly noise, and as a workspace grows that noise would
        # block more and more legitimate resolutions. Noise below the chain
        # floor does not make a strong match ambiguous -- another STRONG
        # candidate does, and then the model picks.
        strong = [m for m in scored if m["score"] >= AUTO_CHAIN_FLOOR]
        if len(strong) == 1:
            m = strong[0]
            result["type"] = m["type"]
            result["name"] = m["name"]
            if m["type"] == "project":
                result["projectId"] = m["projectId"]
                result["projectName"] = m["name"]
            else:
                result["email"] = m.get("email", "")
                result["phone"] = m.get("phone", "")

        # Withholding the id stops the MACHINE from acting on a weak match. It
        # says nothing to the MODEL, and the model is the one writing the
        # answer. Asked about "Farmacia Moderna" -- a project that does not
        # exist, 0.562 against "Farmacia Haussman" -- the same question three
        # times produced: an assumed correction with the tasks listed, an
        # assumed correction, and a proper "did you mean?". A coin flip,
        # because the result carried a number and no reading of it.
        #
        # So the reading travels WITH the data. Every path sees it: the
        # planner composing a direct_response and the narrator alike.
        if not scored:
            result["confidence"] = "none"
            result["instruction"] = (
                f"Nothing resembles {query!r}. Say it does not exist. Do NOT "
                f"offer an unrelated project as if it were a correction.")
        elif not strong:
            result["confidence"] = "low"
            result["instruction"] = (
                f"NO candidate is close enough to assume. {query!r} most "
                f"likely does not exist -- a name this far off is usually a "
                f"different thing, not a typo. ASK which one they mean, or "
                f"say nothing matched. Do NOT claim you corrected the name, "
                f"and do NOT fetch anything for a candidate the user has not "
                f"confirmed.")
        elif len(strong) > 1:
            result["confidence"] = "ambiguous"
            result["instruction"] = (
                "Several candidates are equally plausible. Ask which one; do "
                "not pick for the user.")
        else:
            result["confidence"] = "high"

        top = f"{scored[0]['name']!r} @ {scored[0]['score']}" if scored else "nothing"
        print(f"[Tool] resolve_entity('{query}') -> {len(scored)} candidate(s), top={top}")
        return result
    except Exception as e:
        return {"error": str(e), "count": 0, "candidates": []}


@register_tool("resolve_person")
def resolve_person(name: str) -> dict:
    """Looks up a person by NAME among the participants of the user's projects
    and returns their contact info (email, phone, role, project).

    Use: when the user mentions someone by name WITHOUT giving email/phone
    (e.g.: "send a WhatsApp to Jesus Vega"). Lets us resolve name → contact.

    Returns:
      - matches: DISTINCT matching people (deduped by email > phone > name),
        each with the projects where they appear.
      - count: number of distinct people.
      - When count == 1: also exposes email/phone/name/projectId/projectName
        at the top level, to reference them with {"from_step": N, "extract": "phone"}.
    """
    q = (name or "").strip().lower()
    if not q:
        return {"error": "Provide the name of the person to look up", "count": 0, "matches": []}
    try:
        pids = _accessible_project_ids()
        found = {}  # key (email|phone|name) -> aggregate
        for pid in pids:
            proj = projects_table.get_item(Key={'projectId': pid}).get('Item') or {}
            pname = proj.get('name', '')
            for p in (proj.get('participants') or []):
                if not isinstance(p, dict):
                    continue
                pn = (p.get('name', '') or '').strip()
                if not pn:
                    continue
                pnl = pn.lower()
                # Flexible match: substring in either direction.
                if q not in pnl and pnl not in q:
                    continue
                email = (p.get('email', '') or '').strip().lower()
                tel = (p.get('phone', '') or p.get('phone', '') or '').strip()
                key = email or tel or pnl
                if key not in found:
                    found[key] = {"name": pn, "email": email, "phone": tel,
                                  "role": (p.get('role', '') or ''), "projects": []}
                if email and not found[key]["email"]:
                    found[key]["email"] = email
                if tel and not found[key]["phone"]:
                    found[key]["phone"] = tel
                found[key]["projects"].append({"projectId": pid, "projectName": pname})
        # Typo fallback. The substring pass above is untouched, so every
        # lookup that resolved before resolves identically. Only when it
        # comes back empty do we rank by similarity, so a mistyped name gets
        # candidates instead of "that person does not exist".
        #
        # These candidates are SUGGESTIONS, not resolutions: they carry a
        # score and "fuzzy": true, and the top-level email/phone shortcut
        # below is deliberately withheld for them. Sending a WhatsApp to the
        # wrong person is irreversible, so a guessed identity must pass
        # through the user before anything is sent -- that is a safety
        # decision, not a semantic one, which is why it lives in code.
        if not found:
            for pid in pids:
                proj = projects_table.get_item(Key={'projectId': pid}).get('Item') or {}
                pname = proj.get('name', '')
                for p in (proj.get('participants') or []):
                    if not isinstance(p, dict):
                        continue
                    pn = (p.get('name', '') or '').strip()
                    if not pn:
                        continue
                    score = _similarity(q, pn)
                    if score < RECALL_FLOOR:
                        continue
                    email = (p.get('email', '') or '').strip().lower()
                    tel = (p.get('phone', '') or '').strip()
                    key = email or tel or _norm_name(pn)
                    if key not in found:
                        found[key] = {"name": pn, "email": email, "phone": tel,
                                      "role": (p.get('role', '') or ''), "projects": [],
                                      "fuzzy": True, "score": score}
                    found[key]["score"] = max(found[key].get("score", 0), score)
                    if email and not found[key]["email"]:
                        found[key]["email"] = email
                    if tel and not found[key]["phone"]:
                        found[key]["phone"] = tel
                    found[key]["projects"].append({"projectId": pid, "projectName": pname})

        matches = sorted(found.values(), key=lambda m: m.get("score", 1.0), reverse=True)
        if len(matches) > MAX_CANDIDATES:
            matches = matches[:MAX_CANDIDATES]
        result = {"query": name, "count": len(matches), "matches": matches}
        if len(matches) == 1 and not matches[0].get("fuzzy"):
            m = matches[0]
            result["name"] = m["name"]
            result["email"] = m["email"]
            result["phone"] = m["phone"]
            if m["projects"]:
                result["projectId"] = m["projects"][0]["projectId"]
                result["projectName"] = m["projects"][0]["projectName"]
        elif matches and all(m.get("fuzzy") for m in matches):
            # Make the situation unmissable to the planner: nothing was
            # actually resolved, these are guesses awaiting confirmation.
            result["needs_confirmation"] = True
        return result
    except Exception as e:
        return {"error": str(e), "count": 0, "matches": []}
