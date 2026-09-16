import sys, types, io
from unittest import mock

fake_table = mock.MagicMock()
fake_boto = types.ModuleType('boto3')
fake_boto.resource = lambda *a, **k: mock.MagicMock(Table=lambda n: fake_table)
fake_boto.client = lambda *a, **k: mock.MagicMock()
sys.modules['boto3'] = fake_boto
from agent import tools

ok = True
def check(cond, label):
    global ok
    ok &= bool(cond)
    print(f"  {'OK ' if cond else 'FALLA'} {label}")

print("=== _similarity: solo ordena, nunca decide ===")
for q, c in [("Famarcia Hussman", "Farmacia Haussman"),
             ("Jesuz Bega", "Jesus Vega"),
             ("Marketing Q4", "Marketing Q3"),
             ("Juan", "Julian"),
             ("Vega", "Bega Corp"),
             ("Farmacía Haussmán", "Farmacia Haussman"),
             ("Hussman", "Farmacia Haussman"),
             ("Famarcia Hussman", "Marketing Q3")]:
    print(f"    {tools._similarity(q, c):.3f}  {q!r} vs {c!r}")

PROJECTS = {
    "p1": {"projectId": "p1", "name": "Farmacia Haussman", "description": "Cliente retail",
           "status": "active",
           "participants": [{"name": "Jesus Vega", "email": "j.vega@ethermed.ai", "phone": "+57300", "role": "owner"},
                            {"name": "Julian Ruiz", "email": "julian@x.com", "role": "dev"}]},
    "p2": {"projectId": "p2", "name": "Marketing Q3", "description": "", "status": "active",
           "participants": [{"name": "Ana Lopez", "email": "ana@x.com", "role": "dev"}]},
    "p3": {"projectId": "p3", "name": "Marketing Q4", "description": "", "status": "active",
           "participants": []},
}
# OJO: hay que parchear el MODULO donde vive la funcion, no la fachada.
# agent.tools re-exporta el nombre, pero agent.tools.resolve ya se quedo con
# su propia referencia al importarlo, asi que parchear agent.tools no llega.
from agent.tools import resolve as _resolve_mod
_resolve_mod._accessible_project_ids = lambda *a, **k: set(PROJECTS)
fake_table.get_item.side_effect = lambda Key: {"Item": PROJECTS[Key["projectId"]]}

print("\n=== el caso real: el typo llega arriba ===")
r = tools.resolve_entity("Famarcia Hussman")
check(r["candidates"] and r["candidates"][0]["name"] == "Farmacia Haussman",
      f"top = {r['candidates'][0]['name']!r} (score {r['candidates'][0]['score']})")
check(r["exact"] is None, "exact=None -> el narrador debe confirmar el typo")
check(r["candidates"][0]["type"] == "project", "lo identifica como proyecto, no persona")

print("\n=== Q3/Q4: NO los descarta, los manda ambos al LLM ===")
r = tools.resolve_entity("Marketing Q4")
names = [c["name"] for c in r["candidates"]]
check("Marketing Q4" in names and "Marketing Q3" in names,
      f"ambos presentes -> {names}  (el LLM decide, no la funcion)")
check(r["exact"] and r["exact"]["name"] == "Marketing Q4",
      "exact apunta al literal correcto, asi no se equivoca")

print("\n=== Juan/Julian: tampoco se descarta en silencio ===")
r = tools.resolve_entity("Juan")
names = [c["name"] for c in r["candidates"]]
check("Julian Ruiz" in names, f"'Julian Ruiz' llega como candidato -> {names}")
check(r["exact"] is None, "sin exact -> obliga a preguntar, no a asumir")

print("\n=== match literal: no molesta al usuario ===")
r = tools.resolve_entity("Farmacia Haussman")
check(r["exact"] is not None and r["exact"]["projectId"] == "p1", "exact resuelto directo")

print("\n=== inexistente ===")
r = tools.resolve_entity("Zzzz Inexistente")
check(r["count"] == 0, f"count=0, dropped={r['dropped_below_floor']} (piso de ruido)")

print("\n=== resolve_person: regresion + seguridad ===")
rp = tools.resolve_person("Jesus Vega")
check(rp["count"] == 1 and rp.get("email") == "j.vega@ethermed.ai",
      "exacto: expone email para el from_step, igual que antes")
rpf = tools.resolve_person("Jesuz Bega")
check(rpf["count"] >= 1 and rpf["matches"][0].get("fuzzy") is True, "typo: marcado fuzzy")
check(rpf.get("needs_confirmation") is True, "needs_confirmation=True")
check("email" not in rpf, "email NO expuesto -> imposible enviar a un adivinado")
check(tools.resolve_person("Farmacia Haussman")["count"] == 0,
      "un proyecto sigue sin ser persona")

print("\n=== registro ===")
for t in ("resolve_entity", "resolve_person"):
    check(t in tools.TOOL_MAP, f"{t} registrada")
check('- resolve_entity(query): Ranks' in tools.TOOLS_DESCRIPTION,
      "descrita en TOOLS_DESCRIPTION")
src = io.open('agent/tools/resolve.py', encoding='utf-8').read()
check('FUZZY_THRESHOLD' not in src, "sin restos del umbral viejo")

print("\n" + ("TODO OK" if ok else "HAY FALLAS"))
sys.exit(0 if ok else 1)
