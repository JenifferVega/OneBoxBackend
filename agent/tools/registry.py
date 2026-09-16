"""TOOL_MAP, the @register_tool decorator and execute_tool.

Moved verbatim out of the old single-file agent/tools.py.
"""



TOOL_MAP = {}

def register_tool(name):
    """Decorator to register tools."""
    def decorator(func):
        TOOL_MAP[name] = func
        return func
    return decorator


def execute_tool(tool_name: str, params: dict) -> dict:
    """
    Executes a tool by name.
    """
    if tool_name not in TOOL_MAP:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        tool_func = TOOL_MAP[tool_name]

        if params:
            result = tool_func(**params)
        else:
            result = tool_func()

        return result

    except TypeError as e:
        return {"error": f"Invalid parameters for {tool_name}: {str(e)}"}
    except Exception as e:
        return {"error": f"Error executing {tool_name}: {str(e)}"}
