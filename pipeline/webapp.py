"""本地 Web 服务：浏览器/手机访问，异步任务 + 轮询。

启动：
    python -m pipeline.webapp [--host 127.0.0.1] [--port 8000] [--password <口令>]

默认只绑 127.0.0.1；手机/局域网访问需 --host 0.0.0.0，此时必须设口令。
cloudflared 隧道是公网可见的，务必设口令或「用完就关」。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from fastapi import Body, Cookie, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
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

from . import advisor, advisor_guard, export, llm, steps, store

STATIC_DIR = Path(__file__).resolve().parent / "static"
_PASSWORD = os.environ.get("WEB_PASSWORD", "")
_SESSIONS: set = set()
_state = {"resume_name": "", "last_run": ""}
_executor = ThreadPoolExecutor(max_workers=2)
_tasks: dict = {}
_lock = threading.Lock()
store.init()

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


_KEY_PLACEHOLDERS = ("your", "xxx", "placeholder", "example", "changeme")

# 对外部署开关：BYOK_ONLY=1 时，服务端 .env 里的 key 一律不兜底，
# 没带 X-LLM-Key 的请求直接 400。用来保证"别人用的一定是他自己的 key"，
# 不会因为服务器上配了主人的 key 而被白用。本机自用不设这个开关。
_BYOK_ONLY = os.environ.get("BYOK_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def _validate_header_key(key: str | None) -> str | None:
    """校验请求头带来的 key：长度≥20 且不含占位词；格式不对直接 400，不拿去调 API 试错（那是白花钱）。"""
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    low = key.lower()
    if len(key) < 20 or any(bad in low for bad in _KEY_PLACEHOLDERS):
        raise HTTPException(status_code=400, detail="API Key 格式不对：长度不足 20 或像占位符，请填真实的 key")
    return key


def _resolve_creds(x_llm_key: str | None, x_llm_base_url: str | None, x_llm_model: str | None) -> dict | None:
    """从请求头解析 BYOK 凭据。

    有 key → 用他的 key；无 key → BYOK_ONLY 模式下直接 400（绝不用服务器 key 代跑），
    否则若 .env 配了口令/自用 key 则回退（给本机自用留方便）。
    """
    key = _validate_header_key(x_llm_key)
    if key:
        return {
            "api_key": key,
            "base_url": (x_llm_base_url or "").strip() or None,
            "model": (x_llm_model or "").strip() or None,
        }
    if _BYOK_ONLY:
        raise HTTPException(
            status_code=400,
            detail="本服务要求使用你自己的 API Key（不代付费用）。请点右上角「设置」填写后再分析。",
        )
    if not llm.env_has_key():
        raise HTTPException(
            status_code=400,
            detail="还没有 API Key：请在网页「设置」里填你自己的 key，或在本地 .env 里配 LLM_API_KEY。本工具不代付 API 费用。",
        )
    return None


DEVICE_COOKIE = "pipeline_device"


def _ensure_device(device: str | None, response: Response) -> str:
    """任何请求进来：有 device cookie 就用它并 touch；没有就发一个新的、在 users 插一行。"""
    if device:
        store.get_or_create_user(device)
        return device
    device = uuid.uuid4().hex
    response.set_cookie(DEVICE_COOKIE, device, httponly=True, samesite="lax", max_age=365 * 24 * 3600)
    store.get_or_create_user(device)
    return device


def _is_localhost(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _check_task_owner(owner: str, device_id: str, request: Request) -> None:
    """task 归属校验：owner 必须是当前设备；存量 'local' 记录仅本机可见。"""
    if owner == device_id:
        return
    if owner == "local" and _is_localhost(request):
        return
    raise HTTPException(status_code=403, detail="这不是你的任务")


def _build_summary(advice: dict) -> str:
    """把上次分析的 advice 压成 ≤300 字摘要，注入追问 prompt（绝不塞整份 JSON / 简历原文）。"""
    score = advice.get("score")
    parts = [
        f"【上次结论】{advice.get('position', '')}｜{score if score is not None else '?'} 分 "
        f"{advice.get('verdict', '')}｜{advice.get('conclusion', '')}"
    ]
    sr = advice.get("score_reasons") or {}
    scores = advice.get("scores") or {}
    best_k, best_v = None, -1
    for k in ("technical", "experience", "behavioral", "career"):
        if k in sr:
            v = scores.get(k)
            if isinstance(v, (int, float)) and v > best_v:
                best_v, best_k = v, k
    if best_k:
        parts.append(f"【最强项】{best_k}：{sr[best_k]}")
    gaps = advice.get("gaps") or []
    if gaps:
        parts.append(f"【最大缺口】{gaps[0]}")
    adv = str(advice.get("advice") or "")
    if adv:
        parts.append(f"【上次建议】{adv[:60]}")
    summary = "\n".join(parts)
    if len(summary) > 300:
        summary = summary[:299] + "…"
    return summary


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
    x_llm_key: str | None = Header(default=None),
    x_llm_base_url: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    device: str | None = Cookie(default=None, alias="pipeline_device"),
    response: Response = None,
):
    _require_auth(token)
    device_id = _ensure_device(device, response)
    creds = _resolve_creds(x_llm_key, x_llm_base_url, x_llm_model)  # 无 key 且无 .env 时这里直接 400，不发起调用
    if resume_file is not None and resume_file.filename:
        _state["resume_name"] = resume_file.filename
    resume = resume_text.strip() or _read_upload(resume_file)
    jd = jd_text.strip() or _read_upload(jd_file)
    missing = []
    if not resume:
        missing.append("简历")
    if not jd:
        missing.append("岗位 JD")
    if missing:
        raise HTTPException(
            status_code=400,
            detail="还缺少：%s。请在输入框里填写，或上传文件后再点「开始分析」。"
            % "、".join(missing),
        )

    task_id = uuid.uuid4().hex[:12]
    with _lock:
        _tasks[task_id] = {
            "status": "running", "step": 0, "elapsed": 0, "result": None, "error": None,
            "device_id": device_id,
        }
    store.save_task(
        task_id,
        status="running",
        resume_name=_state.get("resume_name", "") or "",
        position=position.strip(),
        device_id=device_id,
    )
    _executor.submit(_run_task, task_id, resume, jd, company_text.strip(), position.strip(), creds, device_id)
    return {"task_id": task_id}


def _run_task(task_id: str, resume: str, jd: str, company: str, position: str, creds: dict | None = None, device_id: str = "local") -> None:
    t0 = time.time()
    # BYOK：凭据在这个 worker 线程里设，跑完清空；contextvars 每个线程独立，不会串到别的请求。
    if creds:
        llm.set_request_credentials(api_key=creds["api_key"], base_url=creds.get("base_url"), model=creds.get("model"))

    def on_step(n: int, name: str) -> None:
        with _lock:
            _tasks[task_id]["step"] = n
        store.save_task(task_id, step=n)

    try:
        result = steps.run_pipeline(resume, jd, company, position=position, on_step=on_step, device_id=device_id)
        with _lock:
            _tasks[task_id].update(status="done", result=result, elapsed=round(time.time() - t0, 1))
            _state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        store.save_task(
            task_id,
            status="done",
            elapsed=round(time.time() - t0, 1),
            run_dir=result["run_dir"],
            position=result["advice"].get("position", "") or position,
        )
    except Exception as e:  # noqa: BLE001
        with _lock:
            _tasks[task_id].update(status="error", error=str(e), elapsed=round(time.time() - t0, 1))
        store.save_task(task_id, status="error", error=str(e), elapsed=round(time.time() - t0, 1))
    finally:
        if creds:
            llm.clear_request_credentials()


def _load_task(task_id: str) -> dict:
    """内存优先；服务重启后从 SQLite + run 目录恢复，不再一刷新就空。"""
    with _lock:
        t = _tasks.get(task_id)
    if t is not None:
        return t
    row = store.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在，可能已经清理过了")
    t = {
        "status": row["status"],
        "step": row["step"] or 0,
        "elapsed": row["elapsed"] or 0,
        "error": row["error"] or None,
        "device_id": row.get("device_id") or "local",
        "result": None,
    }
    run_dir = row.get("run_dir") or ""
    if row["status"] == "done" and run_dir and Path(run_dir).exists():
        d = Path(run_dir)
        try:
            t["result"] = {
                "run_dir": run_dir,
                "advice": json.loads((d / "04_apply_advice.json").read_text(encoding="utf-8")),
                "coverage": json.loads((d / "05_coverage.json").read_text(encoding="utf-8")),
            }
        except FileNotFoundError:
            pass
    return t


def _load_owned_task(task_id: str, device_id: str, request: Request) -> dict:
    t = _load_task(task_id)
    _check_task_owner(t.get("device_id") or "local", device_id, request)
    return t


@app.get("/api/task/{task_id}")
def task_status(task_id: str, token: str | None = Cookie(default=None, alias="pipeline_token"),
                device: str | None = Cookie(default=None, alias="pipeline_device"),
                response: Response = None, request: Request = None):
    _require_auth(token)
    device_id = _ensure_device(device, response)
    t = _load_owned_task(task_id, device_id, request)
    return {"status": t["status"], "step": t["step"], "elapsed": t["elapsed"], "error": t["error"]}


@app.get("/api/task/{task_id}/result")
def task_result(task_id: str, token: str | None = Cookie(default=None, alias="pipeline_token"),
                device: str | None = Cookie(default=None, alias="pipeline_device"),
                response: Response = None, request: Request = None):
    _require_auth(token)
    device_id = _ensure_device(device, response)
    t = _load_owned_task(task_id, device_id, request)
    if t["status"] != "done" or not t["result"]:
        raise HTTPException(status_code=404, detail="任务还没跑完，稍等几秒再看")
    return {"advice": t["result"]["advice"], "coverage": t["result"]["coverage"]}


@app.post("/api/task/{task_id}/advisor")
def task_advisor(
    task_id: str,
    token: str | None = Cookie(default=None, alias="pipeline_token"),
    x_llm_key: str | None = Header(default=None),
    x_llm_base_url: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    device: str | None = Cookie(default=None, alias="pipeline_device"),
    response: Response = None,
    request: Request = None,
):
    """简历顾问：复用该任务的 01/02/04，只花 1 次模型调用；同输入第二次命中缓存。"""
    _require_auth(token)
    device_id = _ensure_device(device, response)
    creds = _resolve_creds(x_llm_key, x_llm_base_url, x_llm_model)
    t = _load_owned_task(task_id, device_id, request)
    if t["status"] != "done" or not t["result"]:
        raise HTTPException(status_code=409, detail="先等上面的分析跑完，再生成简历顾问")
    d = Path(t["result"]["run_dir"])
    if not (d / "00_resume.txt").exists():
        raise HTTPException(
            status_code=409,
            detail="这次任务来自旧版本（没保存简历原文），请重新点一次「开始分析」再生成顾问",
        )
    resume_text = (d / "00_resume.txt").read_text(encoding="utf-8")
    profile = json.loads((d / "01_profile.json").read_text(encoding="utf-8"))
    jd = json.loads((d / "02_jd_analysis.json").read_text(encoding="utf-8"))
    advice = t["result"]["advice"]
    if creds:
        llm.set_request_credentials(api_key=creds["api_key"], base_url=creds.get("base_url"), model=creds.get("model"))
    try:
        adv = advisor.resume_advisor(
            resume_text, profile, jd, advice, position=advice.get("position", "") or ""
        )
    finally:
        if creds:
            llm.clear_request_credentials()
    if "error" in adv:
        raise HTTPException(status_code=502, detail="简历顾问没生成成功，请再点一次")
    (d / "06_resume_advisor.json").write_text(
        json.dumps(adv, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"advisor": adv, "markdown": advisor_guard.render_markdown(adv)}


@app.post("/api/task/{task_id}/export")
def task_export(task_id: str, format: str = "xlsx", token: str | None = Cookie(default=None, alias="pipeline_token"),
                device: str | None = Cookie(default=None, alias="pipeline_device"),
                response: Response = None, request: Request = None):
    _require_auth(token)
    device_id = _ensure_device(device, response)
    t = _load_owned_task(task_id, device_id, request)
    if t["status"] != "done" or not t["result"]:
        raise HTTPException(status_code=404, detail="任务还没跑完，稍等几秒再看")
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
    if format == "md":
        src = run_dir / "06_resume_advisor.json"
        if not src.exists():
            raise HTTPException(status_code=400, detail="还没生成简历顾问，先点「生成简历顾问」")
        adv = json.loads(src.read_text(encoding="utf-8"))
        out = run_dir / "简历顾问.md"
        out.write_text(advisor_guard.render_markdown(adv), encoding="utf-8")
        return FileResponse(out, media_type="text/markdown", filename="简历顾问.md")
    raise HTTPException(status_code=400, detail="format 只支持 xlsx | pdf | md")


@app.get("/api/auth")
def auth_probe(token: str | None = Cookie(default=None, alias="pipeline_token")):
    """真接口（替代前端原来那个 404 假探针）：告诉前端要不要登录、当前是否已登录、服务端有没有兜底 key。"""
    return {
        "password_required": bool(_PASSWORD),
        "logged_in": (not _PASSWORD) or (token in _SESSIONS),
        "has_key": llm.env_has_key(),
    }


@app.get("/api/runs")
def recent_runs(token: str | None = Cookie(default=None, alias="pipeline_token"),
                device: str | None = Cookie(default=None, alias="pipeline_device"),
                response: Response = None):
    """最近 10 次分析（来自 SQLite，重启后仍在，只返回当前设备自己的）。"""
    _require_auth(token)
    device_id = _ensure_device(device, response)
    return {"runs": store.list_tasks_for_device(device_id, 10)}


@app.get("/api/status")
def status(token: str | None = Cookie(default=None, alias="pipeline_token"),
           device: str | None = Cookie(default=None, alias="pipeline_device"),
           response: Response = None):
    _require_auth(token)
    device_id = _ensure_device(device, response)
    latest = store.list_tasks_for_device(device_id, 1)
    if latest:
        r = latest[0]
        return {
            "resume_name": r.get("resume_name", ""),
            "last_run": r.get("created_at", ""),
            "position": r.get("position", ""),
        }
    return {
        "resume_name": _state["resume_name"],
        "last_run": _state["last_run"],
    }


@app.get("/api/history")
def history(limit: int = 20, offset: int = 0, token: str | None = Cookie(default=None, alias="pipeline_token"),
            device: str | None = Cookie(default=None, alias="pipeline_device"),
            response: Response = None, request: Request = None):
    """历史列表：当前设备的分析记录；'local' 存量记录仅本机可见。"""
    _require_auth(token)
    device_id = _ensure_device(device, response)
    device_ids = [device_id]
    if _is_localhost(request):
        device_ids.append("local")
    sessions = store.list_sessions_multi(device_ids, limit=limit, offset=offset)
    return {
        "history": [
            {
                "session_id": s["session_id"],
                "position": s["position"],
                "score": s["score"],
                "verdict": s["verdict"],
                "conclusion": s["conclusion"],
                "venue": s["venue"],
                "run_dir": s["run_dir"],
                "created_at": s["created_at"],
            }
            for s in sessions
        ]
    }


@app.get("/api/history/{session_id}")
def history_detail(session_id: str, token: str | None = Cookie(default=None, alias="pipeline_token"),
                   device: str | None = Cookie(default=None, alias="pipeline_device"),
                   response: Response = None, request: Request = None):
    """打开某条历史：返回 advice + coverage（校验归属）。"""
    _require_auth(token)
    device_id = _ensure_device(device, response)
    s = store.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    if s["device_id"] != device_id and not (s["device_id"] == "local" and _is_localhost(request)):
        raise HTTPException(status_code=403, detail="这不是你的记录")
    d = Path(s["run_dir"])
    try:
        advice = json.loads((d / "04_apply_advice.json").read_text(encoding="utf-8"))
        coverage = json.loads((d / "05_coverage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="结果文件已被清理")
    return {"advice": advice, "coverage": coverage}


@app.post("/api/task/{task_id}/ask")
def task_ask(
    task_id: str,
    payload: dict = Body(...),
    token: str | None = Cookie(default=None, alias="pipeline_token"),
    x_llm_key: str | None = Header(default=None),
    x_llm_base_url: str | None = Header(default=None),
    x_llm_model: str | None = Header(default=None),
    device: str | None = Cookie(default=None, alias="pipeline_device"),
    response: Response = None,
    request: Request = None,
):
    """追问一次：注入 ≤300 字上次分析摘要，不塞整份 JSON / 简历原文。"""
    _require_auth(token)
    device_id = _ensure_device(device, response)
    creds = _resolve_creds(x_llm_key, x_llm_base_url, x_llm_model)
    t = _load_owned_task(task_id, device_id, request)
    if t["status"] != "done" or not t["result"]:
        raise HTTPException(status_code=409, detail="先等上面的分析跑完，再追问")
    question = (payload or {}).get("question", "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    advice = t["result"]["advice"]
    summary = _build_summary(advice)

    # 找到对应 session_id，用于把消息存进 messages（本批只存不读，多轮对话在批次 3）
    d = Path(t["result"]["run_dir"])
    try:
        resume_sha = hashlib.sha256((d / "00_resume.txt").read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]
        jd_sha = hashlib.sha256((d / "00_jd.txt").read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]
    except OSError:
        resume_sha = jd_sha = ""
    s = store.get_session_by_keys(device_id, resume_sha, jd_sha, advice.get("position", ""))
    session_id = s["session_id"] if s else ""

    if creds:
        llm.set_request_credentials(api_key=creds["api_key"], base_url=creds.get("base_url"), model=creds.get("model"))
    try:
        system = steps._fill(steps._load_prompt("07_ask.md"), SUMMARY=summary, QUESTION=question)
        data = llm.chat_json(system=system, user="", temperature=0.2, step="07_ask")
    finally:
        if creds:
            llm.clear_request_credentials()

    if isinstance(data, dict) and "error" in data:
        raise HTTPException(status_code=502, detail="追问生成失败，请再试一次")
    answer = (data or {}).get("answer", "").strip()
    if not answer:
        raise HTTPException(status_code=502, detail="追问生成失败（返回为空），请再试一次")

    store.add_message(device_id, session_id, "user", question)
    store.add_message(device_id, session_id, "assistant", answer)
    return {"answer": answer, "used_summary_chars": len(summary)}


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
