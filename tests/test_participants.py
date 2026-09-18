"""Participants render as people, whatever the writer called the fields."""
import os
import sys

# Resolve against the REPOSITORY, not against whoever's machine wrote this.
# These tests read the real source and exercise it; pointing them at an
# absolute path outside the repo made them unrunnable for everyone else.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def repo_file(*parts):
    return os.path.join(REPO, *parts)

import ast, sys

src = open(repo_file("api", "services", "projects.py"), encoding="utf-8").read()
ns = {}
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_PARTICIPANT_ALIASES":
        exec(compile(ast.Module([n], []), "x", "exec"), ns)
    if isinstance(n, ast.FunctionDef) and n.name in (
            "normalize_participant", "normalize_participants", "initials"):
        exec(compile(ast.Module([n], []), "x", "exec"), ns)
norm, norms, ini = ns["normalize_participant"], ns["normalize_participants"], ns["initials"]

ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got!r}" +
          ("" if good else f"  (expected {want!r})"))


print("\nThe bug on screen: Spanish keys")
es = {"nombre": "Kevin Martinez", "rol": "Desarrollador",
      "telefono": "+50494622817", "correo": "kevin@acme.com"}
r = norm(es)
check("name", r["name"], "Kevin Martinez")
check("role", r["role"], "Desarrollador")
check("phone", r["phone"], "+50494622817")
check("email", r["email"], "kevin@acme.com")
check("initials are real initials", ini(r["name"]), "KM")

print("\nThe old behaviour, for contrast")
old_name = es.get("name", str(es))
check("it used to print the whole dict", old_name.startswith("{'nombre'"), True)

print("\nEnglish keys keep working")
en = {"name": "Ana Torres", "role": "Coordinator", "phone": "+1", "email": "a@b.c"}
check("unchanged", norm(en), {"name": "Ana Torres", "role": "Coordinator",
                              "phone": "+1", "email": "a@b.c"})

print("\nOther spellings seen in stored data")
check("role_inferred (wizard)", norm({"name": "X", "role_inferred": "QA"})["role"], "QA")
check("celular", norm({"name": "X", "celular": "+504"})["phone"], "+504")
check("fullName", norm({"fullName": "Luis Paz"})["name"], "Luis Paz")
check("a bare string is a name", norm("Pedro")["name"], "Pedro")

print("\nNothing unknown ever reaches the screen")
check("unknown keys dropped",
      norm({"nombre": "Z", "notas_internas": {"x": 1}}),
      {"name": "Z", "role": "", "email": "", "phone": ""})
check("a participant with nothing identifying is dropped",
      norms([{"notas": "sin datos"}, {"nombre": "Z"}]),
      [{"name": "Z", "role": "", "email": "", "phone": ""}])
check("None is not a person", norms([None]), [])

print("\nNo value is ever a dict")
for case in ({"nombre": {"a": 1}}, {"name": ["x"]}, {"phone": 5}):
    bad = [k for k, v in norm(case).items() if not isinstance(v, str)]
    check(f"all strings for {case}", bad, [])

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
