import os
import sys
import stat
import asyncio
import uuid
import shutil
import re
import json
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from huggingface_hub import HfApi, snapshot_download, hf_hub_download
from contextlib import asynccontextmanager

PROJECT_ROOT = Path(__file__).parent.resolve()
CONVERTER_ENGINE_DIR = PROJECT_ROOT / "converter_engine"
STAGING_DIR = PROJECT_ROOT / "staging"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOCAL_HF_CACHE = PROJECT_ROOT / ".hf_cache"

os.environ["HF_HOME"] = str(LOCAL_HF_CACHE)
os.environ["HF_HUB_CACHE"] = str(LOCAL_HF_CACHE)

active_processes = set()
active_pipelines = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for process in list(active_processes):
        try:
            if process.returncode is None:
                process.kill()
        except Exception:
            pass

app = FastAPI(lifespan=lifespan)

class TaskManager:
    def __init__(self):
        self.queues = {}

    def create_task(self) -> str:
        task_id = str(uuid.uuid4())
        self.queues[task_id] = asyncio.Queue(maxsize=1000)
        return task_id

    async def log(self, task_id: str, message: str):
        if task_id in self.queues:
            try:
                await asyncio.wait_for(self.queues[task_id].put(message), timeout=1.0)
            except asyncio.TimeoutError:
                pass 

    async def get_log(self, task_id: str):
        if task_id in self.queues:
            return await self.queues[task_id].get()
        return None

    def remove_task(self, task_id: str):
        self.queues.pop(task_id, None)

task_manager = TaskManager()

class TokenPayload(BaseModel):
    token: str

class PipelinePayload(BaseModel):
    token: str
    repo_id: str
    quant_profile: str

def sanitize_path_segment(segment: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\.-]', '_', segment)

@app.post("/api/auth/token")
async def verify_token(payload: TokenPayload):
    try:
        api = HfApi(token=payload.token)
        user_info = api.whoami()
        return {"status": "success", "username": user_info.get("name")}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

@app.get("/api/models/search")
async def search_models(q: str, sort: str = "downloads", token: str = Header(None)):
    api = HfApi(token=token)
    try:
        models = api.list_models(search=q, limit=50, sort=sort)
        data = []
        for m in models:
            data.append({
                "repo_id": m.id, 
                "downloads": getattr(m, 'downloads', 0) or 0,
                "likes": getattr(m, 'likes', 0) or 0
            })
        return {"status": "success", "data": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/artifacts")
async def list_artifacts():
    try:
        if not OUTPUT_DIR.exists():
            return {"status": "success", "data": []}
        
        artifacts = []
        artifact_paths = set(OUTPUT_DIR.glob("*.gguf")) | set(OUTPUT_DIR.glob("*.processing"))
        
        for file in artifact_paths:
            stat_info = file.stat()
            artifacts.append({
                "filename": file.name,
                "size_bytes": stat_info.st_size,
                "created_at": stat_info.st_mtime
            })
        artifacts.sort(key=lambda x: x["created_at"], reverse=True)
        return {"status": "success", "data": artifacts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/artifacts/{filename}")
async def delete_artifact(filename: str):
    safe_filename = sanitize_path_segment(filename)
    if not safe_filename.endswith(".gguf") and not safe_filename.endswith(".processing"):
        safe_filename += ".gguf"
        
    target_path = OUTPUT_DIR / safe_filename
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    try:
        target_path.unlink()
        return {"status": "success", "message": f"Deleted {safe_filename}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/artifacts/download/{filename}")
async def download_artifact(filename: str):
    safe_filename = sanitize_path_segment(filename)
    if not safe_filename.endswith(".gguf"):
        safe_filename += ".gguf"
        
    target_path = OUTPUT_DIR / safe_filename
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    
    if safe_filename.endswith(".processing"):
        raise HTTPException(status_code=423, detail="Artifact is currently locked for encoding")
        
    return FileResponse(path=target_path, filename=safe_filename, media_type='application/octet-stream')

@app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
async def pacify_chrome_devtools():
    return {}

async def execute_subprocess(task_id: str, command: list, cwd: Path, timeout: int = 1200):
    env = os.environ.copy()
    env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    active_processes.add(process)
    
    try:
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                raise RuntimeError("Subprocess execution halted: Watchdog timeout exceeded (Zombie Process).")
                
            if not line:
                break
            await task_manager.log(task_id, line.decode('utf-8', errors='replace').strip())
        
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"Subprocess terminated with code {process.returncode}")
    finally:
        active_processes.discard(process)

async def pipeline_worker(task_id: str, payload: PipelinePayload):
    safe_repo_name = sanitize_path_segment(payload.repo_id)
    model_staging_dir = STAGING_DIR / safe_repo_name
    
    safe_basename = sanitize_path_segment(payload.repo_id.split('/')[-1])
    
    # Mathematically preserve native tensor topology (e.g., BFloat16) for intermediate artifacts
    is_unquantized = payload.quant_profile.upper() in ["F16", "F32", "BF16", "AUTO"]
    base_out_type = payload.quant_profile.lower() if is_unquantized else "auto"
    
    out_base = OUTPUT_DIR / f"{safe_basename}-{base_out_type.upper()}.gguf"
    out_base_processing = out_base.with_suffix(".gguf.processing")
    
    out_quant = OUTPUT_DIR / f"{safe_basename}-{payload.quant_profile}.gguf"
    out_quant_processing = out_quant.with_suffix(".gguf.processing")

    async def gc_reaper(target_path: Path, desc: str):
        if not target_path.exists():
            return

        def remove_readonly(func, path, exc_info):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        for attempt in range(5):
            try:
                if target_path.is_dir():
                    shutil.rmtree(target_path, onerror=remove_readonly)
                else:
                    try:
                        target_path.unlink(missing_ok=True)
                    except PermissionError:
                        os.chmod(target_path, stat.S_IWRITE)
                        target_path.unlink(missing_ok=True)
                
                if not target_path.exists():
                    await task_manager.log(task_id, f"  -> Purged {desc}")
                    return
            except Exception:
                pass
                
            if attempt < 4:
                try:
                    await asyncio.sleep(0.5 * (2 ** attempt)) 
                except asyncio.CancelledError:
                    break 

        await task_manager.log(task_id, f"  -> [WARN] OS-level lock retained on {desc}. GC aborted for this sector.")

    try:
        LOCAL_HF_CACHE.mkdir(exist_ok=True)
        STAGING_DIR.mkdir(exist_ok=True)
        OUTPUT_DIR.mkdir(exist_ok=True)

        await task_manager.log(task_id, f"[PHASE 1] Initializing Pre-Flight Topology Inspection for {payload.repo_id}")
        loop = asyncio.get_running_loop()
        api = HfApi(token=payload.token)
        
        def probe_repo():
            return api.model_info(repo_id=payload.repo_id)
            
        repo_info = await loop.run_in_executor(None, probe_repo)
        repo_files = [f.rfilename for f in repo_info.siblings]

        is_moe = False
        requires_jinja = False

        if "config.json" in repo_files:
            try:
                config_path = await loop.run_in_executor(None, lambda: hf_hub_download(
                    repo_id=payload.repo_id, filename="config.json", token=payload.token, cache_dir=LOCAL_HF_CACHE))
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    archs = config_data.get("architectures", [])
                    if any("MoE" in arch or "Gemma" in arch for arch in archs):
                        is_moe = True
                        await task_manager.log(task_id, f"  -> Architecture Detected: High-Density/MoE Topology.")
            except Exception:
                pass

        if "tokenizer_config.json" in repo_files:
            try:
                tok_path = await loop.run_in_executor(None, lambda: hf_hub_download(
                    repo_id=payload.repo_id, filename="tokenizer_config.json", token=payload.token, cache_dir=LOCAL_HF_CACHE))
                with open(tok_path, 'r', encoding='utf-8') as f:
                    tok_data = json.load(f)
                    if "chat_template" in tok_data:
                        requires_jinja = True
            except Exception:
                pass

        repo_size_bytes = sum(f.size for f in repo_info.siblings if f.size is not None)
        multiplier = 3.8 if is_moe else 2.5
        required_space = repo_size_bytes * multiplier 
        free_space = shutil.disk_usage(PROJECT_ROOT).free
        
        if required_space > free_space:
            req_gb = required_space / (1024**3)
            free_gb = free_space / (1024**3)
            raise RuntimeError(f"Hardware Exhaustion Risk: Pipeline requires ~{req_gb:.2f}GB of contiguous free space for tensor mapping. {free_gb:.2f}GB available. Aborting.")
            
        await task_manager.log(task_id, f"  -> Storage verified: {free_space / (1024**3):.2f}GB available. Safe to proceed.")

        has_safetensors = any(f.endswith(".safetensors") for f in repo_files)
        dynamic_ignore_patterns = ["*.msgpack", "*.h5", "*.ot"]
        if has_safetensors:
            dynamic_ignore_patterns.append("*.bin") 
            await task_manager.log(task_id, f"  -> Safetensors verified. Excluding legacy .bin blobs.")
        else:
            await task_manager.log(task_id, f"  -> [WARN] Safetensors absent. Falling back to .bin ingestion.")
        
        await task_manager.log(task_id, f"[PHASE 2] Acquiring weights for {payload.repo_id}")
        def download_sync():
            return snapshot_download(
                repo_id=payload.repo_id,
                local_dir=model_staging_dir,
                cache_dir=LOCAL_HF_CACHE,
                token=payload.token,
                ignore_patterns=dynamic_ignore_patterns
            )
            
        model_path = await loop.run_in_executor(None, download_sync)
        
        await task_manager.log(task_id, f"[PHASE 3] Executing base {base_out_type.upper()} conversion")
        out_base_processing.touch(exist_ok=True)
        
        cmd_convert = [
            sys.executable,
            str(CONVERTER_ENGINE_DIR / "convert_hf_to_gguf.py"),
            str(model_path),
            "--outfile",
            str(out_base_processing),
            "--outtype",
            base_out_type
        ]
        
        if requires_jinja:
            # We preserve the exact string to trigger the UI Telemetry Badge, but we 
            # no longer inject the broken flag, as llama.cpp natively auto-resolves this now.
            await task_manager.log(task_id, "  -> Injecting --jinja template heuristics (Native Auto-Resolve).")

        await execute_subprocess(task_id, cmd_convert, PROJECT_ROOT)
        out_base_processing.replace(out_base) 

        if not is_unquantized:
            await task_manager.log(task_id, f"[PHASE 4] Quantizing to {payload.quant_profile}")
            out_quant_processing.touch(exist_ok=True)
            cmd_quant = [
                str(CONVERTER_ENGINE_DIR / "llama-quantize.exe"),
                str(out_base),
                str(out_quant_processing),
                payload.quant_profile
            ]
            await execute_subprocess(task_id, cmd_quant, PROJECT_ROOT)
            out_quant_processing.replace(out_quant) 

        await task_manager.log(task_id, "[PHASE 5] Executing Memory & Storage Garbage Collection")
        await gc_reaper(model_staging_dir, "Staging Directory")
        if not is_unquantized:
            await gc_reaper(out_base, "Intermediate Base GGUF")
        await gc_reaper(LOCAL_HF_CACHE, "Isolated HF Blob Cache")

        await task_manager.log(task_id, "[SUCCESS] Pipeline execution and GC complete.")
        await task_manager.log(task_id, "EOF")

    except (Exception, asyncio.CancelledError) as e:
        err_msg = str(e) if str(e) else "Task Interrupted"
        await task_manager.log(task_id, f"[ERROR] Pipeline aborted: {err_msg}")
        await gc_reaper(out_base_processing, "Orphaned Base Processing Lock")
        await gc_reaper(out_quant_processing, "Orphaned Quant Processing Lock")
        await gc_reaper(model_staging_dir, "Staging Directory (Emergency Cleanup)")
        await gc_reaper(LOCAL_HF_CACHE, "Isolated HF Blob Cache (Emergency Cleanup)")
        await task_manager.log(task_id, "EOF")

    finally:
        active_pipelines.discard(payload.repo_id)
        await asyncio.sleep(10)
        task_manager.remove_task(task_id)

@app.post("/api/pipeline/start")
async def start_pipeline(payload: PipelinePayload):
    if payload.repo_id in active_pipelines:
        raise HTTPException(status_code=409, detail=f"A pipeline is already actively encoding {payload.repo_id}.")
        
    active_pipelines.add(payload.repo_id)
    task_id = task_manager.create_task()
    asyncio.create_task(pipeline_worker(task_id, payload))
    return {"status": "success", "task_id": task_id}

@app.websocket("/ws/pipeline/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await websocket.accept()
    try:
        while True:
            log_line = await task_manager.get_log(task_id)
            if log_line == "EOF":
                await websocket.send_text("EOF")
                break
            if log_line:
                await websocket.send_text(log_line)
    except WebSocketDisconnect:
        pass
    finally:
        task_manager.remove_task(task_id)
        try:
            await websocket.close()
        except RuntimeError:
            pass

if (PROJECT_ROOT / "static").exists():
    app.mount("/", StaticFiles(directory="static", html=True), name="static")