"""Projects and tasks narrator (list/create project, tasks, reminders)."""

GUIDANCE = """## GUIDE FOR PROJECTS AND TASKS:
1. If projects were listed WITHOUT creating a task (only a list_projects result exists):
   — The user probably did not specify which project to act on.
   — List the available projects with their names and types.
   — Ask in which project they want to perform the pending action.
   — Example: "I found these projects:\\n- Alpha (Backend)\\n- Nova (Marketing)\\nWhich one do you want to create the task in?"

2. If a project, task or reminder was created, confirm WHAT was created, in which
   project, the owner and dates if any.

3. If a project AND multiple tasks were created in the same plan (conversation
   or document ingestion), do NOT say "you can ask me to assign the tasks" — they're already assigned.
   Instead:
   — List the detected participants with their roles.
   — Show the created tasks with their owner and due date.
   — Example:
     "Project **DW - FUSTE CARRERAS** created with 5 participants and 7 tasks:
      - Replicate theme design → **Belen Alberdi** (by 05/14)
      - Define category schema → **Mat Two Zero** (by 05/22)
      ..."

3. If projects were listed AND something was also created/modified, show both parts:
   what was available and what was done.

4. If there are errors (project not found, no permission), explain them in a friendly way.

## EXAMPLE listing for selection:
"**Available projects:**
- **Alpha** — Backend (active)
- **Nova** — Marketing (active)

Which of these projects do you want to create the task 'review Q3 budget' in?\""""
