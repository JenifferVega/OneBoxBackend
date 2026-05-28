

from typing import TypedDict, List, Optional, Any

class AgentState(TypedDict, total=False):
    """Estado que fluye entre los nodos del grafo."""
    
    user_message: str
    history: List[dict]
    
    plan: List[dict]  
    
    results: dict 
    tools_used: List[str]
    
    validation_feedback: str
    iteration: int
    
    status: str 
    
    response: str
    direct_response: str  