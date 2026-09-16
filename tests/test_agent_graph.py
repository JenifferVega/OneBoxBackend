"""Tests for the agent graph with stub LLMs (no AWS or external APIs).

Run with:  python3 tests/test_agent_graph.py
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
    """Stub with the same interface as NodeLLM (.invoke / .with_structured_output)."""

    def __init__(self, structured_outputs=None, text_outputs=None):
        self.structured_outputs = list(structured_outputs or [])
        self.text_outputs = list(text_outputs or [])
        self.invoke_count = 0
        self.structured_count = 0

    def invoke(self, msgs, **kw):
        self.invoke_count += 1
        text = self.text_outputs.pop(0) if self.text_outputs else "stub response"
        return AIMessage(text)

    def with_structured_output(self, schema, **kw):
        outer = self

        class _Structured:
            def invoke(self, msgs, **kw2):
                outer.structured_count += 1
                if not outer.structured_outputs:
                    raise RuntimeError("stub has no more structured outputs")
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
    out = PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")])
    assert out.plan[0].params == {}
    try:
        ValidatorOutput(decision="MAYBE")
        raise AssertionError("should have rejected an invalid decision")
    except Exception:
        pass
    print("✅ test_schemas")


def test_history_conversion():
    msgs = to_lc_messages([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ])
    assert msgs[0].__class__.__name__ == "HumanMessage"
    assert msgs[1].__class__.__name__ == "AIMessage"
    print("✅ test_history_conversion")


def test_greeting_fast_path():
    """Greeting: with no history the resolver skips the LLM; planner uses regex (0 LLM)."""
    llms = make_llms()
    graph = build_graph(llms)
    final = graph.invoke(initial_state("Hi!"), config={"recursion_limit": 25})
    assert final["response"] == GREETING_RESPONSE, final["response"][:100]
    assert llms["context_resolver"].invoke_count == 0
    assert llms["planner"].structured_count == 0
    assert llms["narrator"].invoke_count == 0  # passthrough without LLM
    assert final["resolution_method"] == "regex"
    print("✅ test_greeting_fast_path")


def test_plan_flow_with_chaining(monkeypatched_calls):
    """Two-step plan with from_step → executor → validator DONE → narrator."""
    plan = PlannerOutput(plan=[
        PlanStep(step=1, tool="list_projects", params={}),
        PlanStep(step=2, tool="create_task", params={"data": {"from_step": 1}, "text": "t"}),
    ])
    llms = make_llms(
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE", summary="ok")]),
        narrator=StubLLM(text_outputs=["📋 **Done**: task created"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("create a task t in my project"),
                         config={"recursion_limit": 25})
    assert final["response"] == "📋 **Done**: task created"
    assert final["tools_used"] == ["list_projects", "create_task"]
    # from_step resolved: step 2 (create_task) received the result of step 1
    assert monkeypatched_calls[2][0] == "create_task"
    assert monkeypatched_calls[2][1]["data"] == {"fake": "projects"}
    assert final["intent"] == "projects"
    print("✅ test_plan_flow_with_chaining")


def test_replan_once_then_done(monkeypatched_calls):
    """Validator ERROR once → replan with feedback → DONE."""
    p1 = PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")])
    p2 = PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")])
    planner = StubLLM(structured_outputs=[p1, p2])
    llms = make_llms(
        planner=planner,
        validator=StubLLM(structured_outputs=[
            ValidatorOutput(decision="ERROR", feedback="timeout, retry"),
            ValidatorOutput(decision="DONE"),
        ]),
        narrator=StubLLM(text_outputs=["done"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("list my projects"), config={"recursion_limit": 25})
    assert planner.structured_count == 2
    assert final["response"] == "done"
    print("✅ test_replan_once_then_done")


def test_replan_cap_degrades_gracefully():
    """Validator always ERROR → iteration cap → still narrates (no infinite loop)."""
    plans = [PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")]) for _ in range(5)]
    errors = [ValidatorOutput(decision="ERROR", feedback="failure") for _ in range(5)]
    llms = make_llms(
        planner=StubLLM(structured_outputs=plans),
        validator=StubLLM(structured_outputs=errors),
        narrator=StubLLM(text_outputs=["partial response"]),
    )
    graph = build_graph(llms)
    final = graph.invoke(initial_state("list my projects"), config={"recursion_limit": 25})
    assert final["response"], "should produce a response even if the validator keeps failing"
    print("✅ test_replan_cap_degrades_gracefully")


def test_unknown_tool_sanitized():
    """The planner discards hallucinated tools; without a valid plan → clarification."""
    bad = PlannerOutput(plan=[PlanStep(step=1, tool="made_up_tool")])
    llms = make_llms(planner=StubLLM(structured_outputs=[bad]))
    graph = build_graph(llms)
    final = graph.invoke(initial_state("do something weird"), config={"recursion_limit": 25})
    assert "didn't quite understand" in final["response"]
    print("✅ test_unknown_tool_sanitized")


def test_context_resolver_rewrites_followups():
    """With history, the resolver rewrites and the planner sees the resolved message."""
    plan = PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")])
    llms = make_llms(
        context_resolver=StubLLM(text_outputs=["show me the tasks of my projects"]),
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE")]),
        narrator=StubLLM(text_outputs=["here they are"]),
    )
    graph = build_graph(llms)
    history = [{"role": "user", "content": "show me my projects"},
               {"role": "assistant", "content": "You have 3 projects..."}]
    final = graph.invoke(initial_state("and the tasks?", history), config={"recursion_limit": 25})
    assert final["resolved_message"] == "show me the tasks of my projects"
    assert llms["context_resolver"].invoke_count == 1
    print("✅ test_context_resolver_rewrites_followups")


def test_multitenant_guard_does_not_crash_graph():
    """Without set_current_user the tool fails with a controlled error and the graph still narrates."""
    plan = PlannerOutput(plan=[PlanStep(step=1, tool="list_projects")])
    llms = make_llms(
        planner=StubLLM(structured_outputs=[plan]),
        validator=StubLLM(structured_outputs=[ValidatorOutput(decision="DONE")]),
        narrator=StubLLM(text_outputs=["no access"]),
    )
    # execute_tool is NOT monkeypatched: the real tool should fail due to missing context
    graph = build_graph(llms)
    final = graph.invoke(initial_state("list my projects"), config={"recursion_limit": 25})
    assert final["response"] == "no access"
    result = final["results"][1]
    assert "error" in str(result).lower(), f"expected a context error: {result}"
    print("✅ test_multitenant_guard_does_not_crash_graph")


def test_run_agent_contract():
    """run_agent preserves the {response, tools_used} contract with a stub graph."""
    import agent.graph.runner as runner
    llms = make_llms()
    runner._GRAPH = build_graph(llms)
    try:
        result = runner.run_agent("Hi", [])
        assert set(result.keys()) == {"response", "tools_used"}
        assert result["response"] == GREETING_RESPONSE
        assert result["tools_used"] == []
    finally:
        runner._GRAPH = None
    print("✅ test_run_agent_contract")


def main():
    # Monkeypatch execute_tool for the flow tests (the executor imported it by name)
    import agent.graph.nodes.executor.node as executor_module
    calls = {}
    real_execute_tool = executor_module.execute_tool

    def fake_execute_tool(tool_name, params):
        calls[len(calls) + 1] = (tool_name, params)
        if tool_name == "list_projects":
            return {"fake": "projects"}
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

    print("\n🎉 ALL TESTS PASSED")


if __name__ == "__main__":
    main()
