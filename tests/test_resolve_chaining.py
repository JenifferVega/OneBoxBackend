"""An id is handed to the next step only when the evidence is strong.

The bug: "Panaderia Lopez" -- a project that does not exist -- scored 0.4
against "Plataforma de aprendizaje en linea", was the only candidate, and the
planner chained its projectId and listed 11 tasks of an unrelated project.
With a write tool instead of list_tasks, that is a write to the wrong project.
"""
import ast, re, sys, unicodedata

SRC = "/mnt/user-data/uploads/oneboxproject/OneBoxBackend/agent/tools/resolve.py"
src = open(SRC).read()
ns = {"unicodedata": unicodedata, "re": re}
for n in ast.parse(src).body:
    if isinstance(n, (ast.Assign, ast.AnnAssign)):
        tgt = getattr(n, "target", None) or n.targets[0]
        if getattr(tgt, "id", "") in ("RECALL_FLOOR", "AUTO_CHAIN_FLOOR", "MAX_CANDIDATES"):
            exec(compile(ast.Module([n], []), "x", "exec"), ns)
    if isinstance(n, ast.FunctionDef) and n.name in ("_norm_name", "_edits", "_similarity"):
        exec(compile(ast.Module([n], []), "x", "exec"), ns)

sim = ns["_similarity"]
FLOOR = ns["RECALL_FLOOR"]
CHAIN = ns.get("AUTO_CHAIN_FLOOR")

PROJECTS = ["Farmacia Haussman", "Plataforma de aprendizaje en linea",
            "Sistema de Gestion de Inventario", "Onebox2026", "Marketing Q3"]


def resolve(query):
    """The gate as resolve_entity applies it: rank, cut noise, then decide
    whether the single survivor earns a machine-usable id."""
    scored = sorted(((sim(query, p), p) for p in PROJECTS), reverse=True)
    kept = [(s, p) for s, p in scored if s >= FLOOR]
    strong = [(s, p) for s, p in kept if s >= CHAIN]
    return kept, (strong[0][1] if len(strong) == 1 else None)


ok = True
def check(label, got, want):
    global ok
    good = got == want
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {got!r}" +
          ("" if good else f"  (expected {want!r})"))


print(f"\nRECALL_FLOOR={FLOOR}  AUTO_CHAIN_FLOOR={CHAIN}")
check("the two floors are distinct", CHAIN is not None and CHAIN > FLOOR, True)

print("\nA project that does NOT exist never yields an id")
for q in ["Panaderia Lopez", "Cafeteria Andina", "Ferreteria El Sol",
          "Clinica Santa Ana", "Hotel Las Palmas", "Juan", "Vega"]:
    kept, chained = resolve(q)
    check(f"{q!r}", chained, None)

print("\nIt is still OFFERED when it is close enough to be worth asking about")
kept, _ = resolve("Panaderia Lopez")
check("Panaderia Lopez still returns a candidate to ask about", len(kept), 1)

print("\nA mistyped REAL name still chains")
for q, want in [("Famarcia Hussman", "Farmacia Haussman"),
                ("farmacia hausman", "Farmacia Haussman"),
                ("Hussman", "Farmacia Haussman"),
                ("Plataforma de aprendisaje", "Plataforma de aprendizaje en linea"),
                ("sistema de inventario", "Sistema de Gestion de Inventario"),
                ("Onebox 2026", "Onebox2026")]:
    _, chained = resolve(q)
    check(f"{q!r}", chained, want)

print("\nNoise below the floor does not block a strong match")
kept, chained = resolve("Plataforma de aprendisaje")
check("2 candidates, but only one is strong", (len(kept), chained),
      (2, "Plataforma de aprendizaje en linea"))

print("\nTwo STRONG candidates: the model picks, not the code")
PROJECTS.append("Farmacia Central")
kept, chained = resolve("Farmacia")
check("'Farmacia' matches two pharmacies -> no id handed over", chained, None)
check("both are still offered", len(kept), 2)
PROJECTS.pop()

print("\nThe margin that justifies the threshold")
worst_ok = min(sim(q, w) for q, w in [
    ("Famarcia Hussman", "Farmacia Haussman"),
    ("sistema de inventario", "Sistema de Gestion de Inventario"),
    ("Hussman", "Farmacia Haussman")])
best_bad = max(max(sim(q, p) for p in PROJECTS) for q in
               ["Panaderia Lopez", "Cafeteria Andina", "Hotel Las Palmas"])
print(f"       lowest legitimate {worst_ok:.3f} > {CHAIN} > highest bogus {best_bad:.3f}")
check("the threshold sits inside the gap", best_bad < CHAIN < worst_ok, True)

print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
