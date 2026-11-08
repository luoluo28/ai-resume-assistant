"""本地 Web 服务：浏览器/手机访问，异步任务 + 轮询。

启动：
    python -m pipeline.webapp [--host 127.0.0.1] [--port 8000] [--password <口令>]

默认只绑 127.0.0.1；手机/局域网访问需 --host 0.0.0.0，此时必须设口令。
cloudflared 隧道是公网可见的，务必设口令或「用完就关」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from fastapi import Cookie, FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as e:  # 友好降级：缺依赖给安装提示，而不是甩 Traceback
    sys.stderr.write(
        "启动 Web 服务缺少依赖（fastapi / uvicorn / python-multipart）。\n"
        "请先安装：\n"
        "  python -m ensurepip --upgrade\n"
        "  pip install fastapi uvicorn python-multipart\n"
        f"原始错误：{e}\n"
    )
    raise SystemExit(1)

from . import export, steps

STATIC_DIR = Path(__file__).resolve().parent / "static"
_PASSWORD = os.environ.get("WEB_PASSWORD", "")
_SESSIONS: set = set()
_state = {"resume_name": "", "last_run": ""}
_executor = ThreadPoolExecutor(max_workers=2)
_tasks: dict = {}
_lock = threading.Lock()

app = FastAPI(title="AI简历助手")


def _read_upload(f: UploadFile | None) -> str:
    if f is None:
        return ""
    data = f.file.read()
    suffix = Path(f.filename or "").suffix or ".txt"
    from . import ingest

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        return ingest.ingest(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _require_auth(token: str | None = Cookie(default=None, alias="pipeline_token")) -> None:
    if _PASSWORD and token not in _SESSIONS:
        raise HTTPException(status_code=401, detail="需要口令")


@app.post("/api/login")
def login(password: str = Form(...)):
    if _PASSWORD and password != _PASSWORD:
        raise HTTPException(status_code=401, detail="口令错误")
    token = uuid.uuid4().hex
    _SESSIONS.add(token)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("pipeline_token", token, httponly=True, samesite="lax")
    return resp


@app.post("/api/analyze")
def analyze(
    resume_text: str = Form(""),
    resume_file: UploadFile | None = File(None),
    jd_text: str = Form(""),
    jd_file: UploadFile | None = File(None),
    company_text: str = Form(""),
    position: str = Form(""),
    token: str | None = Cookie(default=None, alias="pipeline_token"),
):
    _require_auth(token)
    if resume_file is not None and resume_file.filename:
        _state["resume_name"] = resume_file.filename
    resume = resume_text.strip() or _read_upload(resume_file)
    jd = jd_text.strip() or _read_upload(jd_file)
    if not resume or not jd:
        raise HTTPException(status_code=400, detail="需要提供简历和 JD")

    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {"status": "running", "step": 0, "elapsed": 0, "result": None, "error": None}
    _executor.submit(_run_task, task_id, resume, jd, company_text.strip(), position.strip())
    return {"task_id": task_id}


def _run_task(task_id: str, resume: str, jd: str, company: str, position: str) -> None:
    t0 = time.time()

    def on_step(n: int, name: str) -> None:
        with _lock:
            _tasks[task_id]["step"] = n

    try:
        result = steps.run_pipeline(resume, jd, company, position=position, on_step=on_step)
        with _lock:
            _tasks[task_id].update(status="done", result=result, elapsed=round(time.time() - t0, 1))
            _state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # noqa: BLE001
        with _lock:
            _tasks[task_id].update(status="error", error=str(e), elapsed=round(time.time() - t0, 1))


@app.get("/api/task/{task_id}")
def task_status(task_id: str, token: str | None = Cookie(default=None, alias="pipeline_token")):
    _require_auth(token)
    t = _tasks.get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"status": t["status"], "step": t["step"], "elapsed": t["elapsed"], "error": t["error"]}


@app.get("/api/task/{task_id}/result")
def task_result(task_id: str, token: str | None = Cookie(default=None, alias="pipeline_token")):
    _require_auth(token)
    t = _tasks.get(task_id)
    if t is None or t["status"] != "done":
        raise HTTPException(status_code=404, detail="任务未完成或不存在")
    return {"advice": t["result"]["advice"], "coverage": t["result"]["coverage"]}


@app.post("/api/task/{task_id}/export")
def task_export(task_id: str, format: str = "xlsx", token: str | None = Cookie(default=None, alias="pipeline_token")):
    _require_auth(token)
    t = _tasks.get(task_id)
    if t is None or t["status"] != "done":
        raise HTTPException(status_code=404, detail="任务未完成或不存在")
    advice = t["result"]["advice"]
    run_dir = Path(t["result"]["run_dir"])
    if format == "xlsx":
        out = run_dir / "投递建议.xlsx"
        export.to_excel([advice], out)
        return FileResponse(
            out,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="投递建议.xlsx",
        )
    if format == "pdf":
        out = run_dir / "投递建议.pdf"
        export.to_pdf(advice, out)
        return FileResponse(out, media_type="application/pdf", filename="投递建议.pdf")
    raise HTTPException(status_code=400, detail="format 只支持 xlsx|pdf")


@app.get("/api/status")
def status(token: str | None = Cookie(default=None, alias="pipeline_token")):
    _require_auth(token)
    return {
        "resume_name": _state["resume_name"],
        "last_run": _state["last_run"],
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def main(argv=None) -> int:
    global _PASSWORD
    import argparse

    parser = argparse.ArgumentParser(description="本地 Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1；手机访问用 0.0.0.0（需口令）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--password", default=_PASSWORD, help="访问口令（公网/局域网务必设置）")
    args = parser.parse_args(argv)

    _PASSWORD = args.password or os.environ.get("WEB_PASSWORD", "")
    if _PASSWORD:
        print("已启用口令保护")
    if not args.host.startswith("127.") and args.host != "localhost" and not _PASSWORD:
        print("错误：--host 0.0.0.0（局域网/公网可达）时必须设置 --password 口令，否则拒绝启动", file=sys.stderr)
        return 1

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
