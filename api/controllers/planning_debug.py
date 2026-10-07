"""Debug endpoints for the planning graph's test cases (used by the MCP).

They run tests/planning cases -- local JSON fixtures, never DynamoDB -- with
the real LLM. Only answered for requests from this same machine: behind the
load balancer the client is never 127.0.0.1, so production returns 404.

A case takes minutes and the desktop bridge gives a tool call 60 seconds, so
work runs in a background thread: each endpoint waits up to WAIT_S and then
answers "still running". planning_report (and chat with message '--status')
returns the result once it is there. Errors are kept with their traceback --
a failure nobody can see is how the first run of this vanished.

Each run is a fresh Python process (this interpreter, so the same
environment). The backend often runs from WSL on /mnt/c, where file changes
made from Windows never reach the reloader: in-process imports kept running
code that had already been fixed on disk.
"""
import json
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "planning" / "cases"
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
WAIT_S = 45
RUN_TIMEOUT_S = 1800


def _py(args: list, timeout: int = RUN_TIMEOUT_S) -> str:
    proc = subprocess.run([sys.executable, *args], cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode not in (0, 1):      # run_planning_case exits 1 on FAIL
        raise RuntimeError(f"exit {proc.returncode}\n{(proc.stdout + proc.stderr)[-3500:]}")
    return proc.stdout
_JOBS: dict = {}
_LOCK = threading.Lock()


def _local_only(request: Request):
    host = request.client.host if request.client else ""
    if host not in _LOCAL or not CASES.exists():
        raise HTTPException(status_code=404, detail="Not found")


def _report(case: str) -> str:
    p = CASES / case / "runs" / "latest" / "report.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _case_dir(case: str) -> Path:
    d = (CASES / case).resolve()
    if d.parent != CASES.resolve() or not (d / "before.json").exists():
        raise HTTPException(status_code=404, detail=f"Case '{case}' not found or without before.json")
    return d


def _start(key: str, fn) -> dict:
    with _LOCK:
        job = _JOBS.get(key)
        if job and job["status"] == "running":
            job["reused"] = True      # the caller must say no new run was started
            return job
        job = {"status": "running", "started": time.time(), "result": None, "error": "", "reused": False}
        _JOBS[key] = job

    def work():
        try:
            job["result"] = fn()
            job["status"] = "done"
        except Exception:
            job["error"] = traceback.format_exc()[-4000:]
            job["status"] = "error"
            print(f"[planning_debug] {key} failed:\n{job['error']}")
    threading.Thread(target=work, daemon=True).start()
    return job


def _wait(job: dict) -> dict:
    deadline = time.time() + WAIT_S
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.5)
    return job


def _running_msg(job: dict) -> str:
    return f"⏳ Sigue corriendo ({int(time.time() - job['started'])} s)."


class RunCase(BaseModel):
    case: str


class ChatTurn(BaseModel):
    case: str
    message: str
    session_id: str = "default"


@router.get("/debug/planning/cases")
def list_cases(request: Request):
    _local_only(request)
    out = []
    for d in sorted(p for p in CASES.iterdir() if p.is_dir()):
        rep = _report(d.name)
        job = _JOBS.get(f"run:{d.name}") or {}
        out.append({"case": d.name, "ready": (d / "before.json").exists(),
                    "last": rep.splitlines()[0] if rep else "", "job": job.get("status", "")})
    return {"cases": out}


@router.post("/debug/planning/run")
def run_case(req: RunCase, request: Request):
    _local_only(request)
    _case_dir(req.case)

    def fn():
        _py(["tests/run_planning_case.py", req.case])
        p = CASES / req.case / "runs" / "latest" / "result.json"
        return json.loads(p.read_text(encoding="utf-8"))
    job = _start(f"run:{req.case}", fn)
    if job.get("reused"):
        return {"passed": None, "report": f"⚠️ Ya había una corrida de '{req.case}' en curso (hace "
                                          f"{int(time.time() - job['started'])} s): NO se lanzó otra. "
                                          "Espera a que termine y consulta planning_report."}
    job = _wait(job)
    if job["status"] == "running":
        return {"passed": None, "report": _running_msg(job) + " Consulta el resultado con planning_report."}
    if job["status"] == "error":
        return {"passed": False, "violations": ["run crashed"], "report": "```\n" + job["error"] + "\n```"}
    r = job["result"] or {}
    return {"passed": r.get("passed"), "existing_accuracy": r.get("existing_accuracy"),
            "plan_items_recall": r.get("plan_items_recall"), "violations": r.get("violations"),
            "report": _report(req.case)}


@router.get("/debug/planning/report/{case}")
def report(case: str, request: Request):
    _local_only(request)
    job = _JOBS.get(f"run:{case}")
    if job and job["status"] == "running":
        return {"report": _running_msg(job)}
    if job and job["status"] == "error":
        return {"report": "La última corrida falló:\n```\n" + job["error"] + "\n```"}
    return {"report": _report(case)}


@router.post("/debug/planning/chat")
def chat(req: ChatTurn, request: Request):
    _local_only(request)
    _case_dir(req.case)
    key = f"chat:{req.case}:{req.session_id or 'default'}"
    if req.message.strip() == "--status":
        job = _JOBS.get(key)
        if not job:
            return {"reply": "No hay ningún turno en curso en esta sesión."}
    else:
        current = _JOBS.get(key)
        if current and current["status"] == "running":
            return {"reply": _running_msg(current) + " Espera y envía '--status'."}

        def fn():
            out = _py(["tests/planning/chat_cli.py", req.case, req.session_id or "default", req.message], 900)
            start = out.find("{\n")
            if start < 0:
                raise RuntimeError(out[-3500:])
            return json.loads(out[start:])
        job = _start(key, fn)
    job = _wait(job)
    if job["status"] == "running":
        return {"reply": _running_msg(job) + " Envía '--status' en un momento para ver la respuesta."}
    if job["status"] == "error":
        return {"reply": "El turno falló:\n```\n" + job["error"] + "\n```"}
    return job["result"]


# ── Conversations (tests/planning/conversations): scripted chats graded per turn ──
CONVERSATIONS = ROOT / "tests" / "planning" / "conversations"


class RunConversation(BaseModel):
    name: str
    upto: int = 0           # only the first N turns
    dry: bool = False       # no LLM: plumbing only


def _conv_dir(name: str) -> Path:
    d = (CONVERSATIONS / name).resolve()
    if d.parent != CONVERSATIONS.resolve() or not (d / "script.json").exists():
        raise HTTPException(status_code=404, detail=f"Conversation '{name}' not found")
    return d


def _conv_latest(name: str, file: str) -> str:
    p = CONVERSATIONS / name / "runs" / "latest" / file
    return p.read_text(encoding="utf-8") if p.exists() else ""


@router.get("/debug/planning/conversations")
def list_conversations(request: Request):
    _local_only(request)
    out = []
    for d in sorted(p for p in CONVERSATIONS.iterdir() if (p / "script.json").exists()):
        s = json.loads((d / "script.json").read_text(encoding="utf-8"))
        last = _conv_latest(d.name, "result.json")
        job = _JOBS.get(f"conv:{d.name}") or {}
        out.append({"name": d.name, "title": s.get("title", ""), "kind": s.get("kind", "replan"),
                    "turns": len(s.get("turns") or s.get("inputs") or []),
                    "last": json.loads(last) if last else None, "job": job.get("status", "")})
    return {"conversations": out}


@router.post("/debug/planning/conversation/run")
def run_conversation(req: RunConversation, request: Request):
    _local_only(request)
    _conv_dir(req.name)
    args = ["tests/planning/run_conversation.py", req.name]
    if req.upto:
        args += ["--upto", str(req.upto)]
    if req.dry:
        args.append("--dry")

    def fn():
        _py(args, timeout=3600)
        return json.loads(_conv_latest(req.name, "result.json") or "{}")
    job = _start(f"conv:{req.name}", fn)
    if job.get("reused"):
        return {"done": False, "report": f"⚠️ Ya había una corrida de '{req.name}' en curso (hace "
                                         f"{int(time.time() - job['started'])} s): NO se lanzó otra."}
    job = _wait(job)
    if job["status"] == "running":
        return {"done": False, "report": _running_msg(job) + " Una conversación tarda ~1–2 min por turno. "
                                                            "Consulta planning_conversation_report."}
    if job["status"] == "error":
        return {"done": True, "report": "La corrida falló:\n```\n" + job["error"] + "\n```"}
    return {"done": True, "result": job["result"], "report": _conv_latest(req.name, "report.md")}


@router.get("/debug/planning/conversation/report/{name}")
def conversation_report(name: str, request: Request):
    _local_only(request)
    job = _JOBS.get(f"conv:{name}")
    if job and job["status"] == "running":
        return {"done": False, "report": _running_msg(job)}
    if job and job["status"] == "error":
        return {"done": True, "report": "La última corrida falló:\n```\n" + job["error"] + "\n```"}
    return {"done": True, "report": _conv_latest(name, "report.md") or "Sin corridas todavía."}
