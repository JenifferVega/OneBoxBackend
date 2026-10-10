"""Model lab: switch a graph node's model live, and benchmark models.

Debug only, like planning_debug: answered for requests from this same machine
(behind the load balancer the client is never 127.0.0.1, so production
returns 404). Used by the MCP tools models_get / models_set / bench_run /
bench_report.

  GET  /debug/models          which provider/model each node uses now
  POST /debug/models          {node, model}: route a node to provider:model
                              (model "" = back to .env). Takes effect on the
                              next chat message; nothing is written to .env.
  POST /debug/bench/run       {models, nodes, cases, repeat}: runs
                              tests/bench/run_bench.py in its own process
  GET  /debug/bench/report    the latest report, or "still running"
"""
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import List, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
NODES = ("context_resolver", "planner", "validator", "narrator")
WAIT_S = 45
# The .env value of each node at startup, so "" can put it back.
_ORIGINAL = {n: os.environ.get(f"NODE_LLM_{n.upper()}", "") for n in NODES}
_BENCH: dict = {"status": "idle"}
_LOCK = threading.Lock()


def _local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in _LOCAL:
        raise HTTPException(status_code=404, detail="Not found")


def _routing() -> list:
    from agent.graph.llm_factory import _available_fallback_providers, _default_provider, _routing_for
    out = []
    for n in NODES:
        provider, model = _routing_for(n)
        provider = provider or _default_provider()
        from agent.graph.llm_factory import _DEFAULT_MODELS
        model = model or _DEFAULT_MODELS.get(provider, "?")
        env = os.environ.get(f"NODE_LLM_{n.upper()}", "")
        out.append({"node": n, "provider": provider, "model": model,
                    "switched": env != _ORIGINAL[n],
                    "env_default": _ORIGINAL[n] or "(routing table)",
                    "fallbacks": [] if os.environ.get("LLM_FALLBACKS", "").lower() in ("off", "0", "false", "no")
                    else _available_fallback_providers(provider)})
    return out


@router.get("/debug/models")
def get_models(request: Request):
    _local_only(request)
    from agent.graph.llm_factory import PROVIDERS, _provider_available
    return {"nodes": _routing(),
            "providers": {p: _provider_available(p) for p in PROVIDERS}}


class SetModel(BaseModel):
    node: str                 # a node name, or "all"
    model: str = ""           # provider:model; "" = back to .env


@router.post("/debug/models")
def set_model(req: SetModel, request: Request):
    _local_only(request)
    from agent.graph import runner
    from agent.graph.llm_factory import _parse_config, create_llm
    nodes = list(NODES) if req.node == "all" else [req.node]
    if any(n not in NODES for n in nodes):
        raise HTTPException(status_code=400, detail=f"node must be one of {NODES} or 'all'")
    value = (req.model or "").strip()
    if value:
        try:
            _parse_config(value)
            create_llm(nodes[0], env_value=value)      # fails now if the key is missing
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    for n in nodes:
        new = value or _ORIGINAL[n]
        if new:
            os.environ[f"NODE_LLM_{n.upper()}"] = new
        else:
            os.environ.pop(f"NODE_LLM_{n.upper()}", None)
    runner.reset_graph()
    return {"ok": True, "nodes": _routing()}


class BenchRun(BaseModel):
    models: Union[List[str], str]
    nodes: str = "all"
    cases: str = "quick"
    repeat: int = 1


def _latest_report() -> str:
    p = ROOT / "tests" / "bench" / "runs" / "latest" / "report.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@router.post("/debug/bench/run")
def bench_run(req: BenchRun, request: Request):
    _local_only(request)
    models = req.models if isinstance(req.models, list) else [m for m in req.models.split(",")]
    models = [m.strip() for m in models if m.strip()]
    if not models:
        raise HTTPException(status_code=400, detail="models: at least one provider:model (or 'env')")
    with _LOCK:
        if _BENCH.get("status") == "running":
            return {"done": False, "reused": True, "message": _running_msg()}
        _BENCH.clear()
        _BENCH.update(status="running", started=time.time(), log="", error="",
                      args={"models": models, "nodes": req.nodes, "cases": req.cases, "repeat": req.repeat})

    def work():
        # Its own process: the benchmark reroutes nodes through env vars and
        # must not change what the live server answers with meanwhile.
        cmd = [sys.executable, "tests/bench/run_bench.py", "--models", ",".join(models),
               "--nodes", req.nodes, "--cases", req.cases, "--repeat", str(max(1, req.repeat))]
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")
            # Everything the run prints goes to THIS terminal too (it used to
            # be captured and dropped, so the backend showed nothing while a
            # benchmark ran) and to tests/bench/runs/latest/run.log.
            log_path = ROOT / "tests" / "bench" / "runs" / "latest" / "run.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as log:
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    if line.startswith("[bench]"):
                        _BENCH["log"] = (_BENCH.get("log", "") + line)[-4000:]
            proc.wait(timeout=4 * 3600)
            _BENCH["status"] = "done" if proc.returncode == 0 else "error"
            if proc.returncode != 0:
                _BENCH["error"] = f"exit {proc.returncode}\n{_BENCH.get('log', '')[-1500:]}"
        except Exception:
            _BENCH["status"] = "error"
            _BENCH["error"] = traceback.format_exc()[-3000:]

    threading.Thread(target=work, daemon=True).start()
    deadline = time.time() + WAIT_S
    while _BENCH["status"] == "running" and time.time() < deadline:
        time.sleep(1)
    return _bench_state()


def _running_msg() -> str:
    return (f"⏳ Benchmark running ({int(time.time() - _BENCH.get('started', time.time()))} s). "
            f"Progress:\n{_BENCH.get('log', '')[-1500:]}")


def _bench_state() -> dict:
    st = _BENCH.get("status", "idle")
    if st == "running":
        return {"done": False, "message": _running_msg()}
    if st == "error":
        return {"done": True, "error": _BENCH.get("error", ""), "report": _latest_report()}
    return {"done": st == "done", "report": _latest_report(), "status": st}


@router.get("/debug/bench/report")
def bench_report(request: Request):
    _local_only(request)
    return _bench_state()
