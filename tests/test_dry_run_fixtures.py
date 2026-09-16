# -*- coding: utf-8 -*-
"""Toda herramienta registrada debe tener fixture de dry-run.

Sin fixture, el executor en modo debug devuelve un resultado vacio: el
validador juzga el plan incompleto, el planner replanifica lo mismo, y el
turno muere tras tres iteraciones sin datos. El unico sintoma es una
conversacion que no llega a ninguna parte, asi que se detecta tarde o nunca
-- resolve_entity estuvo asi un mes.

Este test convierte ese silencio en un fallo visible. Corre sin AWS.
"""
import sys, types, re, io, ast
from unittest import mock

ft = mock.MagicMock()
fb = types.ModuleType('boto3')
fb.resource = lambda *a, **k: mock.MagicMock(Table=lambda n: ft)
fb.client = lambda *a, **k: mock.MagicMock()
sys.modules['boto3'] = fb

from agent.tools import TOOL_MAP

# Se lee el fichero en vez de importarlo: agent.graph.__init__ arrastra
# langgraph, que no hace falta para comprobar una tabla de constantes.
SRC = 'agent/graph/nodes/executor/node.py'
src = io.open(SRC, encoding='utf-8').read()
block = src[src.index('_DRY_RUN_RESULTS = {'):src.index('def _simulate_tool')]
con_fixture = set(re.findall(r'"([a-z_]+)":\s+lambda', block))

# Estas se resuelven en _simulate_tool con logica propia, antes de la tabla.
ESPECIALES = {'list_projects', 'get_project_contacts',
              'create_trello_board', 'link_project_to_trello',
              'push_tasks_to_trello'}
con_fixture |= ESPECIALES

registradas = set(TOOL_MAP)
faltan = sorted(registradas - con_fixture)
sobran = sorted(con_fixture - registradas - ESPECIALES)

print(f"  herramientas registradas: {len(registradas)}")
print(f"  con fixture de dry-run:   {len(registradas & con_fixture)}")

ok = True
if faltan:
    ok = False
    print(f"\n  FALLA  Sin fixture de dry-run: {faltan}")
    print("         Añádelas a _DRY_RUN_RESULTS en")
    print(f"         {SRC}")
    print("         Si no, cualquier conversacion que las use morira en 3 iteraciones.")
else:
    print("  OK     todas las herramientas registradas tienen fixture")

if sobran:
    print(f"\n  AVISO  Fixtures de herramientas que ya no existen: {sobran}")
    print("         No rompen nada, pero sobran.")

print("\n" + ("TODO OK" if ok else "HAY FALLAS"))
sys.exit(0 if ok else 1)
