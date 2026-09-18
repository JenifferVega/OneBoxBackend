#!/usr/bin/env bash
# Run before pushing:   ./scripts/check.sh
#
# Three passes, cheapest first. Each catches a class the others cannot:
#   ruff   -> names that do not exist          (the 500 on project-from-document)
#   mypy   -> calls that cannot bind           (missing/unknown arguments)
#   tests  -> contracts between prompt & code, then every tool and every
#             service function actually RUN once with the database, the
#             network and the LLM faked. A contract check cannot tell you a
#             function is broken inside; running it can.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
bar() { printf '\n\033[1m%s\033[0m\n' "$1"; }

bar "1/3  ruff - undefined names, dead code"
if python -m ruff check . ; then echo "  ok"; else fail=1; fi

bar "2/3  mypy - malformed calls"
# Only the two error codes that are real crashes; the rest is noise on a
# codebase without annotations.
if python -m mypy . 2>&1 | grep -E "error:.*\[(call-arg|attr-defined)\]" ; then
  echo "  ^ malformed calls found"; fail=1
else
  echo "  ok"
fi

bar "3/3  contracts"
for t in tests/test_contracts.py tests/test_tool_schemas.py tests/test_smoke_tools.py \
         tests/test_smoke_services.py tests/test_participants.py \
         tests/test_trello_diff.py tests/test_pending_goal.py \
         tests/test_resolve_chaining.py tests/test_chain_ref.py tests/test_executor_ledger.py ; do
  [ -f "$t" ] || continue
  printf '  %s\n' "$t"
  if ! python "$t" > /tmp/onebox_check.log 2>&1 ; then
    sed 's/^/      /' /tmp/onebox_check.log | tail -25
    fail=1
  fi
done

bar "$([ $fail -eq 0 ] && echo 'ALL CLEAR - safe to push' || echo 'FAILURES - see above')"
exit $fail
