# -*- coding: utf-8 -*-
"""El validador rechazaba planes correctos porque veia los resultados cortados
a 2000 chars, y un replan fallido borraba respuestas ya obtenidas.
Reproduce la traza de 'que tengo pendiente de farmacia hussman'. Sin AWS.
"""
import sys, json, types, importlib.util

# El nodo importa langchain_core y agent.graph.state, que arrastran las
# dependencias del grafo. Aqui solo se prueba la preparacion de datos, asi
# que se sustituyen por stubs y el test corre en cualquier entorno.
_lc = types.ModuleType('langchain_core'); _msg = types.ModuleType('langchain_core.messages')
_msg.HumanMessage = _msg.SystemMessage = lambda **k: None
sys.modules.setdefault('langchain_core', _lc)
sys.modules.setdefault('langchain_core.messages', _msg)

# agent.graph.__init__ importa el builder, que necesita langgraph. Se registra
# el paquete vacio para que los submodulos se puedan cargar sin el.
for _n in ('agent.graph', 'agent.graph.nodes', 'agent.graph.nodes.validator'):
    if _n not in sys.modules:
        _m = types.ModuleType(_n); _m.__path__ = [_n.replace('.', '/')]
        sys.modules[_n] = _m
_st = types.ModuleType('agent.graph.state')
_st.MAX_PLANNER_ITERATIONS = 3
_st.AgentState = dict
sys.modules['agent.graph.state'] = _st
_pr = types.ModuleType('agent.graph.nodes.validator.prompts')
_pr.VALIDATOR_PROMPT = "{user_message}{plan}{results}"
sys.modules['agent.graph.nodes.validator.prompts'] = _pr
_sc = types.ModuleType('agent.graph.nodes.validator.schemas')
_sc.ValidatorOutput = object
sys.modules['agent.graph.nodes.validator.schemas'] = _sc

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

V = _load('_v', 'agent/graph/nodes/validator/node.py')

ok = True
def check(c, l):
    global ok; ok &= bool(c); print(f"  {'OK ' if c else 'FALLA'} {l}")

# --- datos del caso real ----------------------------------------------------
PROJ = lambda i, n, d: {"projectId": f"proj-{i}", "name": n, "description": d,
                        "status": "active", "channels": ["Gmail"],
                        "createdAt": "2026-05-27T02:04:38.108926", "deliveryDate": "",
                        "participants": [{"name": "Jesus Vega", "email": "j.vega@ethermed.ai",
                                          "role": "owner", "phone": "+573001112233"}]}
LIST_PROJECTS = {"count": 5, "projects": [
    PROJ(1, "GhostLink", "GhostLink es una aplicacion de mensajeria con tematica futurista cyberpunk " * 3),
    PROJ(2, "Farmacia Haussman", "Es un proyecto de automatizacion de formulas magistrales " * 3),
    PROJ(3, "Marketing Q3", "Campania trimestral " * 3),
    PROJ(4, "Marketing Q4", "Campania trimestral " * 3),
    PROJ(5, "Onboarding", "Proceso de alta de clientes " * 3)]}
LIST_TASKS = {"count": 4, "tasks": [
    {"taskId": "4120feba", "text": "Desarrollar una solucion de automatizacion para formulas magistrales unicas.",
     "status": "pending", "assignedTo": "Jesus Vega", "dueDate": "2026-09-15"},
    {"taskId": "b22c1d90", "text": "Integrar el catalogo de principios activos.", "status": "pending"},
    {"taskId": "cc7711aa", "text": "Validar dosificacion con el regente.", "status": "blocked"},
    {"taskId": "dd0099bb", "text": "Publicar la primera version interna.", "status": "pending"}]}

print("=== el tamano real que rompia el validador ===")
blob = json.dumps({1: LIST_PROJECTS, 2: LIST_TASKS}, ensure_ascii=False, default=str)
print(f"    resultados completos: {len(blob)} chars   (el corte viejo era 2000)")
viejo = blob[:2000]
check("Farmacia Haussman" not in viejo or "tasks" not in viejo,
      "con el corte viejo el validador NO podia ver las tareas del paso 2")

print("\n=== con el arreglo, ningun paso desaparece ===")
out = V._summarize_results({1: LIST_PROJECTS, 2: LIST_TASKS})
check('"step 1"' in out and '"step 2"' in out, "los dos pasos aparecen")
check("Farmacia Haussman" in out, "el proyecto es visible")
check("Desarrollar una solucion" in out, "las tareas son visibles")

print("\n=== proyeccion: mas informacion en menos espacio ===")
crudo = len(json.dumps({1: LIST_PROJECTS, 2: LIST_TASKS}, ensure_ascii=False, default=str))
proy = len(out)
print(f"    JSON crudo: {crudo} chars   ->   proyectado: {proy} chars   ({100-proy*100//crudo}% menos)")
check(proy < 2000, f"cabe de sobra en el presupuesto viejo de 2000 ({proy})")
check("2026-05-27T02:04:38" not in out and "cyberpunk" not in out,
      "los VALORES de relleno (fechas, descripciones largas) desaparecen")
check("_omitted_fields" in out, "pero se declara QUE se omitio, por nombre")
check(out.count("Marketing") >= 2 and "Onboarding" in out,
      "los 5 proyectos siguen ahi, ninguno se pierde")

print("\n=== y si hay que cortar, se dice ===")
enorme = {1: {"projects": [PROJ(i, f"P{i}", "x" * 800) for i in range(40)]}}
o2 = V._summarize_results(enorme)
check('"count": 40' in o2, "40 proyectos: se declara el total real, no los 25 mostrados")
check("more item(s) exist but are not listed here" in o2,
      "se avisa de los que faltan")
check("do not treat them as missing" in o2,
      "y se le prohibe concluir ausencia de lo que no ve")

print("\n=== un paso pequenio no se pierde detras de uno enorme ===")
o3 = V._summarize_results({1: enorme[1], 2: {"count": 1, "tasks": [{"text": "aguja en el pajar"}]}})
check("aguja en el pajar" in o3, "el paso 2 sobrevive al paso 1 gigante")

print("\n=== la red: un replan fallido no borra la respuesta buena ===")
buenos = {1: LIST_PROJECTS, 2: LIST_TASKS}
check(V._keep_good({}, buenos) == {"last_good_results": buenos},
      "resultados limpios se guardan antes del replan")
# La iteracion 3 real: paso 1 correcto, pasos 2 y 3 con error de validacion.
fallidos = {1: {"query": "farmacia hussman", "count": 1},
            2: {"_validation_error": True, "error": "project_id is a JSON object"},
            3: {"_validation_error": True, "error": "project_id is a JSON object"}}
check(V._keep_good({}, fallidos) == {}, "resultados con error NO se guardan")
check(V._all_clean(fallidos) is False,
      "un paso bueno NO salva un intento con pasos fallidos (era el fallo de mi red)")
check(V._all_clean(buenos) is True, "el intento limpio si cuenta como limpio")

r = V._narrate_with_best({"last_good_results": buenos}, fallidos)
check(r["status"] == "narrate" and r.get("results") == buenos,
      "al agotar iteraciones narra las 4 tareas, no los errores")
r2 = V._narrate_with_best({"last_good_results": buenos}, {1: LIST_TASKS})
check("results" not in r2, "si lo actual sirve, no se pisa con el snapshot")

print("\n" + ("TODO OK" if ok else "HAY FALLAS"))
sys.exit(0 if ok else 1)
