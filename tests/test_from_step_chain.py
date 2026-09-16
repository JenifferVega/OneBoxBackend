# -*- coding: utf-8 -*-
"""Reproduce la traza real de 'que tengo pendiente de farmacia hussman'.

El fallo: resolve_entity encontraba el proyecto (0.938) pero el paso siguiente
recibia el dict entero en project_id en vez del id. Corre sin AWS.
"""
import sys, types
from unittest import mock

ft = mock.MagicMock()
fb = types.ModuleType('boto3')
fb.resource = lambda *a, **k: mock.MagicMock(Table=lambda n: ft)
fb.client = lambda *a, **k: mock.MagicMock()
sys.modules['boto3'] = fb

from agent.tools import resolve as R

# Se cargan por ruta a proposito: agent.graph.__init__ importa langgraph, que
# no hace ninguna falta para probar la resolucion de parametros. Asi el test
# corre en cualquier entorno, incluido CI sin las dependencias del grafo.
import importlib.util
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
resolve_params = _load('_ex_resolve', 'agent/graph/nodes/executor/resolve.py').resolve_params
validate_tool_params = _load('_ex_validators', 'agent/graph/nodes/executor/validators.py').validate_tool_params

ok = True
def check(c, l):
    global ok; ok &= bool(c); print(f"  {'OK ' if c else 'FALLA'} {l}")

PROJECTS = {
  "proj-b8f8cea7": {"projectId": "proj-b8f8cea7", "name": "Farmacia Haussman",
                    "description": "Es un proyecto de automatizacion", "status": "active",
                    "participants": [{"name": "Jesus Vega", "email": "j@x.com"}]},
  "proj-ghost":    {"projectId": "proj-ghost", "name": "GhostLink", "participants": []},
  "proj-q3":       {"projectId": "proj-q3", "name": "Marketing Q3", "participants": []},
  "proj-q4":       {"projectId": "proj-q4", "name": "Marketing Q4", "participants": []},
}
R._accessible_project_ids = lambda *a, **k: set(PROJECTS)
ft.get_item.side_effect = lambda Key: {"Item": PROJECTS[Key["projectId"]]}

print("=== paso 1: resolve_entity con el typo real ===")
r1 = R.resolve_entity("farmacia hussman")
check(r1["count"] == 1 and r1["candidates"][0]["name"] == "Farmacia Haussman",
      f"encuentra el proyecto @ {r1['candidates'][0]['score']}")
check(r1["exact"] is None, "exact=None -> el narrador debe declarar la correccion")
check(r1.get("projectId") == "proj-b8f8cea7",
      "projectId EXPUESTO arriba pese al typo (esto era el bug)")

print("\n=== paso 2: el plan que fallaba, encadenado ===")
p = resolve_params({"project_id": {"from_step": 1, "extract": "projectId"}}, {1: r1})
check(p["project_id"] == "proj-b8f8cea7", f"project_id = {p['project_id']!r}, un string")
check(validate_tool_params("list_tasks", 2, p) is None, "el validador lo acepta")

print("\n=== extraccion fallida: falla fuerte, no cuela basura ===")
bad = resolve_params({"project_id": {"from_step": 1, "extract": "noSuchField"}}, {1: r1})
check(bad["project_id"] is None, "campo inexistente -> None, NO el dict entero")
err = validate_tool_params("list_tasks", 2, bad)
check(err and err.get("_validation_error"), "el validador lo detiene")
check("came back empty" in err["error"], "y el mensaje dice el problema real")

print("\n=== match sin coincidencia: ya no inventa el primero ===")
lp = {"count": 4, "projects": list(PROJECTS.values())}
m = resolve_params({"project_id": {"from_step": 1,
      "match": {"key": "name", "value": "Proyecto Que No Existe"},
      "extract": "projectId"}}, {1: lp})
check(m["project_id"] is None, "sin coincidencia -> None (antes devolvia GhostLink)")
check(validate_tool_params("list_tasks", 2, m) is not None, "el validador lo detiene")

print("\n=== match correcto sigue funcionando ===")
m2 = resolve_params({"project_id": {"from_step": 1,
      "match": {"key": "name", "value": "Farmacia Haussman"},
      "extract": "projectId"}}, {1: lp})
check(m2["project_id"] == "proj-b8f8cea7", "el camino de list_projects intacto")

print("\n=== ambiguo: NO expone id, obliga a preguntar ===")
amb = R.resolve_entity("Marketing Q")
check(amb["count"] >= 2, f"{amb['count']} candidatos: {[c['name'] for c in amb['candidates']]}")
check("projectId" not in amb, "sin projectId arriba -> imposible encadenar a ciegas")
a2 = resolve_params({"project_id": {"from_step": 1, "extract": "projectId"}}, {1: amb})
check(validate_tool_params("list_tasks", 2, a2) is not None,
      "el validador obliga al planner a elegir")

print("\n" + ("TODO OK" if ok else "HAY FALLAS"))
sys.exit(0 if ok else 1)
