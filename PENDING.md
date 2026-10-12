# OneBox — pending work

Living list of what is known to be missing or weak. Most important first in
each section. When something is done, move it to "Done" with the date.

## 0. Before full production

- [ ] **Verify the login token on the backend.** Today the API trusts the
      `x-user-id` / `x-user-email` headers the browser sends; anyone who knows an
      email can act as that person. Verify the Cognito JWT and take the identity
      from it.
- [ ] **Production env:** `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` and the
      `NODE_LLM_*` lines (without them the planner silently runs on Gemini).
      Check afterwards in Settings → System details (no warnings).
- [ ] **Remove the dev-only features:** "view as another user"
      (`api/view_as.py`, `src/devViewAs.ts`, `ViewAsBanner`, `VIEW_AS_TOKEN`) and
      the closable mandatory update window (`VITE_DEV_MODE`).
- [ ] Remove every user's old Trello token
      (`scripts/dynamo/reset_trello_links.py --apply --tokens`) once owners are
      ready to reconnect.

## 1. Intelligence gaps

- [ ] **Task changes are inconsistent** (the biggest one). The same reported
      fact sometimes closes the task and sometimes becomes only a note
      (`bion_preguntas` turn 9). `bion_trello` / `onebox_semana` still fail on
      judgment: closing a task whose own note says it is unfinished; not asking
      when two sources disagree. Plan: more cases of exactly these situations,
      plus code-side rules (a fact reported as done must close/update its task,
      never become only a note).
- [ ] **Learn from rejections.** Rejected proposals are stored but never read
      back: the replan can propose a fourth time what the owner rejected three
      times. Feed "what the owner rejected before, and why" to the replan step.
- [ ] **Project memory of decisions.** Why the delivery moved, what was agreed
      with the client, what left the scope: scattered in notes and proposal
      facts (older proposals have no saved facts). A short decision log per
      project, written on approval, read by the chat answers and future replans.
- [ ] **Update chat repeats a question** the member did not answer (seen in
      `member_espera_lider`: "¿Cómo van los gráficos…?" three turns in a row).
      Ask once, then move on; add a test check for repeated questions.
- [ ] Dates like "next Tuesday" only count when the update chat turns them
      into a real date (`due_date`); otherwise the member is asked again.
- [ ] Blockers recorded without "who must act" leave the member being asked
      instead of notifying the lead.
- [ ] No notion of people's workload or capacity beyond counting tasks.

## 2. Product

- [ ] **"Review the plan"** (guided full replan) in the planning panel:
      the system looks (plan analysis + last updates + waits on the lead) →
      asks 3–5 overall questions → shows a status summary → "Prepare the
      proposal" (optionally re-reading all documents) → review and approve.
- [ ] **Cross-project view for the owner:** "today: 2 proposals to approve,
      1 member waiting on you in BION, Farmacia's delivery at risk".
- [ ] **Translations, next step:** code-written texts (notifications, digests,
      planning chat fixed messages) as key + params rendered by the interface;
      planning chat answers/proposals in the viewer's language.
- [ ] Trello: audit of boards created by members under the old rules.
- [ ] Planning conversations still failing on AI behavior: `bion_trello`,
      `onebox_semana`.
- [ ] Planner on cheaper models failed the benchmark (DeepSeek, GLM too slow):
      revisit when there is time; optional Gemini 2.5/3.8 split test.

## Done

- 2026-10-11 Insight cards follow the en/es switch: the API sends both versions
  (`i18n`), the interface picks the app language; new cards are translated in
  the background (queue + worker thread), hourly sweep for anything missed.

- 2026-10-11 Follow-up asks whoever is responsible, also when: the member
  reported tasks done that the owner has not approved (the owner is asked to
  approve), or a project-level blocker ("the project was cancelled") stops all
  their tasks. 14-day calendar in the update prompts ("next Tuesday" = right date).

- 2026-10-11 Planning panel replaces the Intelligence > Replanning tab
  (chat button with project selector, opens by itself after an upload replan,
  pending-proposal indicators). Mandatory update window closable in dev mode.
- 2026-10-11 Follow-up: pause per member/team; whoever is responsible gets asked
  (members waiting on the lead → the lead is notified); updates in the
  member's language; no daily re-asking after the member gave a date.
- 2026-10-11 es/en storage + insight backfill (`scripts/dynamo/backfill_translations.py`).
- 2026-10-11 Gemini 3: `thinkingLevel` instead of `thinkingBudget=0`.
- 2026-10-10 Planning chat talks by default; saved facts per proposal; System
  details in Settings.
