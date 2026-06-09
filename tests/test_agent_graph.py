"""Tests del grafo del agente con LLMs stub (sin AWS ni APIs externas).

Ejecutar:  python3 tests/test_agent_graph.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage

from agent.graph.builder import _route_after_planner, _route_after_validator, build_graph
from agent.graph.history import to_lc_messages
from agent.graph.nodes.planner.schemas import PlannerOutput, PlanStep
from agent.graph.nodes.validator.schemas import ValidatorOutput
from agent.graph.personality import GREETING_RESPONSE


class StubLLM:
    """Stub con la misma interfaz que NodeLLM (.invoke / .with_structured_output)."""

    def __init__(self, structured_outputs=None, text_outputs=None):
        self.structured_outputs = list(structured_outputs or [])
        self.text_outputs = list(text_outputs or [])
        self.invoke_count = 0
        self.structured_count = 0

    def invoke(self, msgs, **kw):
        self.invoke_count += 1
        text = self.text_outputs.pop(0) if self.text_outputs else "respuesta stub"
        return AIMessage(text)

    def with_structured_output(self, schema, **kw):
        outer = self

        class _Structured:
            def invoke(self, msgs, **kw2):
                outer.structured_count += 1
                if not outer.structured_outputs:
                    raise RuntimeError("stub sin más salidas estructuradas")
                return outer.structured_outputs.pop(0)

        return _Structured()


def make_llms(**stubs):
    return {
        "context_resolver": stubs.get("context_resolver", StubLLM()),
        "planner": stubs.get("planner", StubLLM()),
        "validator": stubs.get("validator", StubLLM()),
        "narrator": stubs.get("narrator", StubLLM()),
    }


def initial_state(message, history=None):
    return {
        "user_message": message,
        "history": history or [],
        "plan": [], "results": {}, "tools_used": [],
        "validation_feedback": None, "iteration": 0,
        "status": "resolving", "response": "", "direct_response": None,
    }


def test_routers():
    assert _route_after_planner({"status": "direct"}) == "narrator"
    assert _route_after_planner({"status": "executing"}) == "executor"
    assert _route_after_validator({"status": "replan"}) == "planner"
    assert _route_after_validator({"status": "narrate"}) == "narrator"
    print("✅ test_routers")


def test_schemas():
    out = PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")])
    assert out.plan[0].params == {}
    try:
        ValidatorOutput(decision="MAYBE")
        raise AssertionError("debió rechazar decision inválida")
    except Exception:
        pass
    print("✅ test_schemas")


def test_history_conversion():
    msgs = to_lc_messages([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "buenas"},
    ])
    assert msgs[0].__class__.__name__ == "HumanMessage"
    assert msgs[1].__class__.__name__ == "AIMessage"
    print("✅ test_history_conversion")


def test_greeting_fast_path():
    """Saludo: sin historial el resolver no llama LLM; planner usa regex (0 LLM)."""
    llms = make_llms()
    graph = build_graph(llms)
    final = graph.invoke(initial_state("Hola!"), config={"recursion_limit": 25})
    assert final["response"] == GREETING_RESPONSE, final["response"][:100]
    assert llms["context_resolver"].invoke_count == 0
    assert llms["planner"].structured_count == 0
    assert llms["narrator"].invoke_count == 0  # passthrough sin LLM
    assert final["resolution_method"] == "regex"
    print("✅ test_greeting_fast_path")


def test_plan_flow_with_chaining(monkeypatched_calls):
    """Plan de 2 pasos con from_step → executor → validator DONE → narrator."""
    plan = PlannerOutput(plan=[
        PlanStep(step=1, tool="listar_proyectos", params={}),
        PlanStep(step=2, tool="crear_tarea", params={"datos": {"from_step": 1}, "text": "t"}),
    ])
    llms = make_llms(
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE", summary="ok")]),
        narrator=StubLLM(text_outputs=["📋 **Listo**: tarea creada"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("crea una tarea t en mi proyecto"),
                         config={"recursion_limit": 25})
    assert final["response"] == "📋 **Listo**: tarea creada"
    assert final["tools_used"] == ["listar_proyectos", "crear_tarea"]
    # from_step resuelto: el paso 2 (crear_tarea) recibió el resultado del paso 1
    assert monkeypatched_calls[2][0] == "crear_tarea"
    assert monkeypatched_calls[2][1]["datos"] == {"fake": "proyectos"}
    assert final["intent"] == "projects"
    print("✅ test_plan_flow_with_chaining")


def test_replan_once_then_done(monkeypatched_calls):
    """Validator ERROR una vez → replaneo con feedback → DONE."""
    p1 = PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")])
    p2 = PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")])
    planner = StubLLM(structured_outputs=[p1, p2])
    llms = make_llms(
        planner=planner,
        validator=StubLLM(structured_outputs=[
            ValidatorOutput(decision="ERROR", feedback="timeout, reintenta"),
            ValidatorOutput(decision="DONE"),
        ]),
        narrator=StubLLM(text_outputs=["hecho"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("lista mis proyectos"), config={"recursion_limit": 25})
    assert planner.structured_count == 2
    assert final["response"] == "hecho"
    print("✅ test_replan_once_then_done")


def test_replan_cap_degrades_gracefully():
    """Validator siempre ERROR → tope de iteraciones → narra igual (no loop infinito)."""
    plans = [PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")]) for _ in range(5)]
    errors = [ValidatorOutput(decision="ERROR", feedback="falla") for _ in range(5)]
    llms = make_llms(
        planner=StubLLM(structured_outputs=plans),
        validator=StubLLM(structured_outputs=errors),
        narrator=StubLLM(text_outputs=["respuesta parcial"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("lista mis proyectos"), config={"recursion_limit": 25})
    assert final["response"], "debe haber respuesta aunque el validator falle siempre"
    print("✅ test_replan_cap_degrades_gracefully")


def test_unknown_tool_sanitized():
    """El planner descarta herramientas alucinadas; sin plan válido → clarificación."""
    bad = PlannerOutput(plan=[PlanStep(step=1, tool="herramienta_inventada")])
    llms = make_llms(planner=StubLLM(structured_outputs=[bad]))
    graph = build_graph(llms)
    final = graph.invoke(initial_state("haz algo raro"), config={"recursion_limit": 25})
    assert "No entendí" in final["response"]
    print("✅ test_unknown_tool_sanitized")


def test_context_resolver_rewrites_followups():
    """Con historial, el resolver reescribe y el planner ve el mensaje resuelto."""
    plan = PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")])
    llms = make_llms(
        context_resolver=StubLLM(text_outputs=["muéstrame las tareas de mis proyectos"]),
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE")]),
        narrator=StubLLM(text_outputs=["aquí están"]),
    )
    graph = build_graph(llms)
    history = [{"role": "user", "content": "muéstrame mis proyectos"},
               {"role": "assistant", "content": "Tienes 3 proyectos..."}]
    final = graph.invoke(initial_state("y las tareas?", history), config={"recursion_limit": 25})
    assert final["resolved_message"] == "muéstrame las tareas de mis proyectos"
    assert llms["context_resolver"].invoke_count == 1
    print("✅ test_context_resolver_rewrites_followups")


def test_multitenant_guard_does_not_crash_graph():
    """Sin set_current_user, la tool falla con error controlado y el grafo narra igual."""
    plan = PlannerOutput(plan=[PlanStep(step=1, tool="listar_proyectos")])
    llms = make_llms(
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE")]),
        narrator=StubLLM(text_outputs=["sin acceso"]),
    )
    # NO se monkeypatchea execute_tool: la tool real debe fallar por falta de contexto
    graph = build_graph(llms)
    final = graph.invoke(initial_state("lista mis proyectos"), config={"recursion_limit": 25})
    assert final["response"] == "sin acceso"
    result = final["results"][1]
    assert "error" in str(result).lower(), f"esperaba error de contexto: {result}"
    print("✅ test_multitenant_guard_does_not_crash_graph")


def test_run_agent_contract():
    """run_agent mantiene el contrato {response, tools_used} con grafo stub."""
    import agent.graph.runner as runner
    llms = make_llms()
    runner._GRAPH = build_graph(llms)
    try:
        result = runner.run_agent("Hola", [])
        assert set(result.keys()) == {"response", "tools_used"}
        assert result["response"] == GREETING_RESPONSE
        assert result["tools_used"] == []
    finally:
        runner._GRAPH = None
    print("✅ test_run_agent_contract")


def main():
    # Monkeypatch de execute_tool para los tests de flujo (el executor lo importó por nombre)
    import agent.graph.nodes.executor.node as executor_module
    calls = {}
    real_execute_tool = executor_module.execute_tool

    def fake_execute_tool(tool_name, params):
        calls[len(calls) + 1] = (tool_name, params)
        if tool_name == "listar_proyectos":
            return {"fake": "proyectos"}
        return {"success": True, "tool": tool_name}

    test_routers()
    test_schemas()
    test_history_conversion()
    test_greeting_fast_path()

    executor_module.execute_tool = fake_execute_tool
    try:
        calls.clear(); test_plan_flow_with_chaining(calls)
        calls.clear(); test_replan_once_then_done(calls)
        test_replan_cap_degrades_gracefully()
    finally:
        executor_module.execute_tool = real_execute_tool

    test_unknown_tool_sanitized()

    executor_module.execute_tool = fake_execute_tool
    try:
        test_context_resolver_rewrites_followups()
    finally:
        executor_module.execute_tool = real_execute_tool

    test_multitenant_guard_does_not_crash_graph()
    test_run_agent_contract()

    print("\n🎉 TODOS LOS TESTS PASARON")


if __name__ == "__main__":
    main()
