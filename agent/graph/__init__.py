"""OneBox agent graph (LangGraph).

Structure (inspired by the reference architecture in send/chatbot/graph):
  - agent.graph.runner       → run_agent() (public contract, graph singleton)
  - agent.graph.builder      → build_graph(llms) + routers
  - agent.graph.state        → typed AgentState
  - agent.graph.personality  → shared identity/style/language
  - agent.graph.llm_factory  → per-node LLMs (env NODE_LLM_*, fallbacks)
  - agent.graph.nodes.*      → per-node packages (node.py + prompts.py + schemas.py)

Flow: context_resolver → planner → (executor → validator)* → narrator
"""
from agent.graph.builder import build_graph  # noqa: F401
from agent.graph.runner import run_agent  # noqa: F401
