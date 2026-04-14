# ==============================================================================
# onebox-agent-py/agent/graph.py
# ==============================================================================
# Grafo que conecta los 4 nodos: Planner → Executor → Validator → Narrator
# ==============================================================================

from typing import List, Dict, Any
from agent.state import AgentState
from agent.nodes import planner_node, executor_node, validator_node, narrator_node

"""
Arquitectura del Grafo:

    START
      │
      ▼
  ┌────────┐
  │PLANNER │ ←──────────────────┐
  └────┬───┘                    │
       │                        │
       ├─── tiene plan ───┐     │
       │                  │     │
       │              ┌───▼───┐ │
       │              │EXECUTOR│ │
       │              └───┬───┘ │
       │                  │     │
       │              ┌───▼────┐│
       │              │VALIDATOR├┘
       │              └───┬────┘ (si incompleto)
       │                  │
       │                  │ (si completo)
       │                  │
       ├── sin plan ──────┼─────┐
       │                  │     │
       │              ┌───▼───┐ │
       │              │NARRATOR│◄┘
       │              └───┬───┘
       │                  │
       ▼                  ▼
      END               END
"""


def run_agent(user_message: str, history: List[dict] = None) -> Dict[str, Any]:
    """
    Ejecuta el agente completo.
    
    Args:
        user_message: Mensaje del usuario
        history: Historial de conversación
    
    Returns:
        dict: {"response": str, "tools_used": List[str]}
    """
    print("\n" + "="*70)
    print("🤖 ONEBOX AGENT - INICIO")
    print("="*70)
    print(f"Mensaje: {user_message}")
    
    # Estado inicial
    state: AgentState = {
        "user_message": user_message,
        "history": history or [],
        "plan": [],
        "results": {},
        "tools_used": [],
        "validation_feedback": "",
        "iteration": 0,
        "status": "planning",
        "response": "",
        "direct_response": ""
    }
    
    # Loop principal del grafo
    max_loops = 10  # Seguridad contra loops infinitos
    loop_count = 0
    
    while loop_count < max_loops:
        loop_count += 1
        current_status = state.get("status", "")
        
        print(f"\n--- Loop {loop_count}, Status: {current_status} ---")
        
        # PLANNER
        if current_status == "planning":
            state = planner_node(state)
            
            # Decidir siguiente paso
            new_status = state.get("status", "")
            if new_status == "done":
                # Ir directo al narrator
                state["status"] = "narrating"
            elif new_status == "executing":
                # Continuar con executor
                pass
            continue
        
        # EXECUTOR
        if current_status == "executing":
            state = executor_node(state)
            continue
        
        # VALIDATOR
        if current_status == "validating":
            state = validator_node(state)
            
            new_status = state.get("status", "")
            if new_status == "continue":
                # Volver al planner
                state["status"] = "planning"
            elif new_status == "done":
                # Ir al narrator
                state["status"] = "narrating"
            continue
        
        # NARRATOR
        if current_status == "narrating":
            state = narrator_node(state)
            break  # Terminamos
        
        # Status "done" sin narrating = error, forzar narrator
        if current_status == "done":
            state["status"] = "narrating"
            continue
        
        # Status desconocido
        print(f"⚠️ Status desconocido: {current_status}")
        break
    
    print("\n" + "="*70)
    print("🤖 ONEBOX AGENT - FIN")
    print("="*70)
    
    return {
        "response": state.get("response", "Lo siento, no pude procesar tu solicitud."),
        "tools_used": state.get("tools_used", [])
    }


# ==============================================================================
# Para testing local
# ==============================================================================
if __name__ == "__main__":
    # Test básico
    print("\n" + "="*70)
    print("TEST LOCAL")
    print("="*70)
    
    test_messages = [
        "Hola, ¿qué puedes hacer?",
        "Muéstrame mis correos recientes",
        "Busca correos de OneBox",
        "¿Tengo correos con adjuntos?",
    ]
    
    for msg in test_messages:
        print(f"\n\n>>> TEST: {msg}")
        result = run_agent(msg, [])
        print(f"\n<<< RESPUESTA: {result['response'][:200]}...")
        print(f"<<< TOOLS: {result['tools_used']}")
        print("-"*50)