"""JSON Schema derived from the tools' real signatures.

    from agent.tools.schema import tool_schema, param_union_schema

Nothing here is written by hand. Every type and every required flag is read
off `inspect.signature`, so the description the model receives cannot drift
from the function it describes. That drift is not hypothetical: the planner
prompt and the signatures are two hand-written copies of the same facts, and
`tests/test_contracts.py` exists only to compare them. What is generated does
not need to be compared.

WHY IT EXISTS
On 2026-09-16 the agent emitted, for a real user, several turns in a row like

    step=2 tool=push_tasks_to_trello params={}
    step=2 tool=resolve_entity       params={}

Every parameter of every tool, empty, deterministic across four replans. The
cause was not the model. `PlanStep.params` was declared as a bare `dict`,
which becomes `{"type": "object", "additionalProperties": true}`. Gemini's
responseSchema does not support `additionalProperties`, so the adapter strips
it (correctly -- Gemini rejects it) and what reaches the model is

    {"type": "object"}

an object with no declared properties. Gemini's structured output is a
CONSTRAINED DECODER, not a hint: with no properties declared and no
additionalProperties allowed, the set of legal keys is empty and `{}` is the
only output the grammar permits. Anthropic and Bedrock treat the same schema
as a description and fill it from the catalog, which is why it worked locally
and failed for the user.

The fix is to declare the properties. `param_union_schema()` builds them from
the tools themselves: 43 distinct names across the 30 tools, with no name used
at two different types. Gemini can now emit them; Anthropic is unaffected.
"""
import inspect
import re
import typing

_JSON_TYPES = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    list: {"type": "array", "items": {"type": "string"}},
    dict: {"type": "object"},
}


def _json_type(annotation) -> dict:
    """A JSON Schema fragment for one annotation.

    Unknown or unannotated -> string, which is what the planner writes anyway
    and what every provider can represent.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}
    if annotation in _JSON_TYPES:
        return dict(_JSON_TYPES[annotation])
    origin = typing.get_origin(annotation)
    if origin in (list, typing.List):
        args = typing.get_args(annotation)
        inner = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": inner}
    if origin in (dict, typing.Dict):
        return {"type": "object"}
    if origin is typing.Union:            # Optional[X] -> X
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
    return {"type": "string"}


_PARAM_BLOCK = re.compile(
    r"^[ \t]*(?:Parameters|Args|Arguments):[ \t]*$(.*?)(?=^\S|\Z)", re.M | re.S)
_PARAM_LINE = re.compile(
    r"^\s*-\s*(\w+)\s*:\s*(.*(?:\n(?!\s*-\s*\w+\s*:).*)*)", re.M)


def param_docs(fn) -> dict:
    """Per-parameter descriptions from a `Parameters:` block, when the
    docstring has one. Absent is fine: the type and the required flag are the
    part that must be exact, and the prose catalog still explains the rest."""
    block = _PARAM_BLOCK.search(inspect.getdoc(fn) or "")
    if not block:
        return {}
    return {name: " ".join(text.split())
            for name, text in _PARAM_LINE.findall(block.group(1))}


def tool_schema(name: str, fn) -> dict:
    """One tool, in the shape every provider's native tool-calling expects.

    Not used by the planner yet -- it plans with a single `params` object.
    This is what a later move to `bind_tools` consumes, and it is already
    correct, so the move does not start by writing 30 schemas by hand.
    """
    docs = param_docs(fn)
    props, required = {}, []
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        schema = _json_type(p.annotation)
        if pname in docs:
            schema["description"] = docs[pname]
        props[pname] = schema
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    summary = (inspect.getdoc(fn) or "").strip().split("\n\n")[0]
    return {
        "name": name,
        "description": " ".join(summary.split()),
        "input_schema": {"type": "object", "properties": props,
                         "required": required},
    }


def all_tool_schemas(tool_map: dict = None) -> list:
    if tool_map is None:
        from agent.tools import TOOL_MAP as tool_map
    return [tool_schema(n, f) for n, f in sorted(tool_map.items())]


class ConflictingParameterType(Exception):
    """One parameter name used at two different types across tools.

    The planner writes ONE `params` object for whichever tool it picked, so a
    name must mean the same thing everywhere. Today none conflict; if that
    changes this raises instead of quietly typing the field wrong.
    """


def param_union_schema(tool_map: dict = None) -> dict:
    """Every parameter name any tool accepts, as optional typed properties.

    This is what `PlanStep.params` declares. Optional, because which ones
    apply depends on the tool; declared, because a provider that decodes
    under the schema can only emit names it has been shown.
    """
    if tool_map is None:
        from agent.tools import TOOL_MAP as tool_map
    props, seen_in = {}, {}
    for tname, fn in sorted(tool_map.items()):
        docs = param_docs(fn)
        for pname, p in inspect.signature(fn).parameters.items():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            schema = _json_type(p.annotation)
            if pname in props and props[pname]["type"] != schema["type"]:
                raise ConflictingParameterType(
                    f"'{pname}' is {props[pname]['type']} in {seen_in[pname]} "
                    f"but {schema['type']} in {tname}. The planner writes one "
                    f"params object for every tool, so a name cannot have two "
                    f"types. Rename one of them.")
            if pname not in props:
                props[pname], seen_in[pname] = schema, tname
            if pname in docs and "description" not in props[pname]:
                props[pname]["description"] = docs[pname]
    return {"type": "object", "properties": props, "additionalProperties": True}
