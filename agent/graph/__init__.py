"""Grafo del agente OneBox (LangGraph).

Estructura (inspirada en la arquitectura de referencia send/chatbot/graph):
  - agent.graph.runner       → run_agent() (contrato público, singleton del grafo)
  - agent.graph.builder      → build_graph(llms) + routers
  - agent.graph.state        → AgentState tipado
  - agent.graph.personality  → identidad/estilo/idioma compartidos
  - agent.graph.llm_factory  → LLMs por nodo (env NODE_LLM_*, fallbacks)
  - agent.graph.nodes.*      → paquetes por nodo (node.py + prompts.py + schemas.py)

Flujo: context_resolver → planner → (executor → validator)* → narrator
"""
from agent.graph.builder import build_graph  # noqa: F401
from agent.graph.runner import run_agent  # noqa: F401
