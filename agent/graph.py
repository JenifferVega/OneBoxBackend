# ==============================================================================
# onebox-agent-py/agent/graph.py
# ==============================================================================
# Grafo que conecta los 4 nodos: Planner → Executor → Validator → Narrator
# ==============================================================================

from typing import List, Dict, Any
from agent.state import AgentState
from agent.nodes import planner_node, executor_node, validator_node, narrator_node

"""
Arquitectura del Grafo

   
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
    
   
    max_loops = 10  
    loop_count = 0
    
    while loop_count < max_loops:
        loop_count += 1
        current_status = state.get("status", "")
        
        print(f"\n--- Loop {loop_count}, Status: {current_status} ---")
        
        if current_status == "planning":
            state = planner_node(state)
            
            new_status = state.get("status", "")
            if new_status == "done":
                state["status"] = "narrating"
            elif new_status == "executing":
                pass
            continue
        
        if current_status == "executing":
            state = executor_node(state)
            continue
        
        
        if current_status == "validating":
            state = validator_node(state)
            
            new_status = state.get("status", "")
            if new_status == "continue":
                state["status"] = "planning"
            elif new_status == "done":
                state["status"] = "narrating"
            continue
        
        if current_status == "narrating":
            state = narrator_node(state)
            break 

        if current_status == "done":
            state["status"] = "narrating"
            continue
        
        print(f"⚠️ Status desconocido: {current_status}")
        break
    
    print("\n" + "="*70)
    print("🤖 ONEBOX AGENT - FIN")
    print("="*70)
    
    return {
        "response": state.get("response", "Lo siento, no pude procesar tu solicitud."),
        "tools_used": state.get("tools_used", [])
    }


if __name__ == "__main__":
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