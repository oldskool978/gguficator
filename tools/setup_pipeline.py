import os
import sys
import stat
import json
import shutil
import subprocess
import urllib.request
import urllib.error
import zipfile
import hashlib
import re
import time

REPO_OWNER = "ggml-org"
REPO_NAME = "llama.cpp"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(PROJECT_ROOT, "converter_engine")
STAGING_DIR = os.path.join(PROJECT_ROOT, "staging_engine")
API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
TIMEOUT = 15

SYSTEM_EXPECTED_BIN = "llama-quantize.exe" 
POSSIBLE_BIN_NAMES = ["llama-quantize.exe", "quantize.exe"]

CRITICAL_DLLS = {
    "cuda": ["cudart64", "cublas64"],
    "hip": ["hip", "rocblas"], 
    "vulkan": [] 
}

def print_status(msg, status="INFO"):
    colors = {"INFO": "\033[94m", "SUCCESS": "\033[92m", "WARN": "\033[93m", "ERROR": "\033[91m", "RESET": "\033[0m"}
    print(f"{colors.get(status, '')}[{status}] {msg}{colors['RESET']}")

def resilient_fs_op(func, *args, retries=5, delay=0.5, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                pass
            time.sleep(delay)

def resilient_purge(path):
    if not os.path.exists(path): return

    def remove_readonly(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    if os.path.isdir(path):
        resilient_fs_op(shutil.rmtree, path, onerror=remove_readonly)
    else:
        for attempt in range(5):
            try:
                os.remove(path)
                return
            except PermissionError:
                try:
                    os.chmod(path, stat.S_IWRITE)
                    os.remove(path)
                    return
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(0.5)

def safe_subprocess(cmd):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode('utf-8', errors='ignore').strip().lower()
    except Exception:
        return ""

def detect_hardware_profile():
    if os.name != 'nt':
        print_status("OS Mismatch: Windows Required.", "ERROR")
        sys.exit(1)

    print_status("Probing Hardware capabilities...", "INFO")

    nv_out = safe_subprocess(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    if nv_out:
        try:
            major_ver = float(nv_out.split('.')[0])
            if major_ver >= 520: 
                print_status("NVIDIA CUDA Detected (v12)", "SUCCESS")
                return {"type": "cuda", "ver": "12"}
            if major_ver >= 450: 
                print_status("NVIDIA CUDA Detected (v11)", "SUCCESS")
                return {"type": "cuda", "ver": "11"}
        except ValueError: pass

    wmic_out = safe_subprocess(["wmic", "path", "win32_VideoController", "get", "Name"])
    if not wmic_out:
        wmic_out = safe_subprocess(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"])
        
    if "amd" in wmic_out or "radeon" in wmic_out:
        print_status("AMD Radeon GPU Detected (HIP/ROCm)", "SUCCESS")
        return {"type": "hip", "ver": None}

    print_status("No Compute GPU Detected. Defaulting to Universal (Vulkan/CPU).", "WARN")
    return {"type": "vulkan", "ver": None}

def fetch_json(url):
    headers = {"User-Agent": "RLM-Hydrator"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                print_status("GitHub API Rate Limit Exceeded. Set GITHUB_TOKEN environment variable.", "ERROR")
                sys.exit(1)
            time.sleep(2)
        except Exception as e:
            if attempt == 2:
                print_status(f"API Failure: {e}", "ERROR")
                sys.exit(1)
            time.sleep(2)

def download_file(url, path, expected_hash=None):
    tmp_path = path + ".tmp"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "RLM-Hydrator"})
            with urllib.request.urlopen(req, timeout=300) as response, open(tmp_path, 'wb') as out_file:
                shutil.copyfileobj(response, out_file)
            
            if expected_hash:
                sha256 = hashlib.sha256()
                with open(tmp_path, 'rb') as f:
                    while chunk := f.read(65536): sha256.update(chunk)
                if sha256.hexdigest().lower() != expected_hash.lower():
                    raise ValueError("Checksum Mismatch")
            
            resilient_fs_op(os.replace, tmp_path, path)
            return True
        except Exception:
            resilient_purge(tmp_path)
            time.sleep(2)
    return False

def extract_source_topology(tag_name):
    print_status("Acquiring and mapping source topology...", "INFO")
    source_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/tags/{tag_name}.zip"
    zip_path = os.path.join(STAGING_DIR, "source.zip")
    
    if not download_file(source_url, zip_path):
        return False

    extract_dir = os.path.join(STAGING_DIR, "source_extracted")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)
    
    extracted_items = os.listdir(extract_dir)
    if not extracted_items: return False
    
    root_folder = os.path.join(extract_dir, extracted_items[0])
    for item in os.listdir(root_folder):
        shutil.move(os.path.join(root_folder, item), os.path.join(TARGET_DIR, item))
        
    return True

def extract_binary_topology(asset_url, expected_hash):
    print_status("Acquiring binary execution payload...", "INFO")
    zip_path = os.path.join(STAGING_DIR, "binaries.zip")
    
    if not download_file(asset_url, zip_path, expected_hash):
        return False

    extract_dir = os.path.join(STAGING_DIR, "bin_extracted")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_dir)

    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.lower().endswith(('.exe', '.dll')):
                src = os.path.join(root, f)
                dst = os.path.join(TARGET_DIR, f)
                if not os.path.exists(dst):
                    shutil.move(src, dst)
    return True

def hydrate_python_environment(profile):
    print_status("Hydrating deterministic Python environment...", "INFO")
    req_path = os.path.join(PROJECT_ROOT, "requirements.txt")
    
    try:
        if profile['type'] == 'hip':
            subprocess.run([sys.executable, "-m", "pip", "install", "--index-url", "https://rocm.nightlies.amd.com/v2-staging/gfx120X-all/", "-U", "rocm[libraries,devel]"], check=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "--index-url", "https://rocm.nightlies.amd.com/v2-staging/gfx120X-all/", "--pre", "-U", "torch", "torchaudio", "torchvision"], check=True)
        elif profile['type'] == 'cuda':
            cu_ver = "cu121" if profile.get('ver') == "12" else "cu118"
            subprocess.run([sys.executable, "-m", "pip", "install", "--index-url", f"https://download.pytorch.org/whl/{cu_ver}", "--pre", "-U", "torch", "torchvision", "torchaudio"], check=True)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "--index-url", "https://download.pytorch.org/whl/cpu", "--pre", "-U", "torch", "torchvision", "torchaudio"], check=True)

        if os.path.exists(req_path):
            subprocess.run([sys.executable, "-m", "pip", "install", "--pre", "-U", "-r", req_path], check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Base python environment hydration failed: {e}")

def synchronize_gguf_library():
    print_status("Synchronizing hermetic 'gguf' python dependency...", "INFO")
    local_gguf_path = os.path.join(TARGET_DIR, "gguf-py")
    
    if not os.path.exists(local_gguf_path):
        return False

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-cache-dir", "--quiet", local_gguf_path]
    
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def converge_structure():
    print_status("Converging and sanitizing directory structure...", "INFO")
    
    for candidate in POSSIBLE_BIN_NAMES:
        current_loc = os.path.join(TARGET_DIR, candidate)
        expected_loc = os.path.join(TARGET_DIR, SYSTEM_EXPECTED_BIN)
        
        if os.path.exists(current_loc) and current_loc != expected_loc:
            resilient_purge(expected_loc)
            os.rename(current_loc, expected_loc)
            break

def verify_python_env():
    try:
        subprocess.run(
            [sys.executable, "-c", "import torch, transformers, tiktoken, einops, fastapi, uvicorn, huggingface_hub"], 
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except subprocess.CalledProcessError:
        return False

def verify_ensemble(profile):
    if not os.path.exists(os.path.join(TARGET_DIR, SYSTEM_EXPECTED_BIN)):
        return False, f"Binary '{SYSTEM_EXPECTED_BIN}' Missing"
    
    if not os.path.exists(os.path.join(TARGET_DIR, "convert_hf_to_gguf.py")):
        return False, "Base conversion architecture missing"
    
    try:
        subprocess.run([sys.executable, "-c", "import gguf"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return False, "Python library 'gguf' is missing from the localized environment"
    
    required_stubs = CRITICAL_DLLS.get(profile['type'], [])
    if required_stubs:
        found_dlls = [f.lower() for f in os.listdir(TARGET_DIR) if f.endswith(".dll")]
        has_dependency = any(any(stub in dll for stub in required_stubs) for dll in found_dlls)
        if not has_dependency:
            return False, f"Missing Runtime DLLs for {profile['type']}"
            
    return True, "Ready"

def main():
    profile = detect_hardware_profile()

    if not verify_python_env():
        hydrate_python_environment(profile)

    is_valid, _ = verify_ensemble(profile)
    if is_valid:
        print_status("Execution Engine verified. Bypassing hydration.", "SUCCESS")
        sys.exit(0)

    resilient_purge(TARGET_DIR)
    resilient_purge(STAGING_DIR)
    os.makedirs(TARGET_DIR, exist_ok=True)
    os.makedirs(STAGING_DIR, exist_ok=True)

    try:
        release = fetch_json(API_URL)
        print_status(f"Targeting Release: {release['tag_name']}", "INFO")

        assets_to_fetch = []
        def add_asset(keywords):
            for asset in release['assets']:
                name = asset['name'].lower()
                if all(k in name for k in keywords):
                    assets_to_fetch.append(asset)
                    return True
            return False

        if profile['type'] == 'cuda':
            add_asset(["llama-", "bin-win", f"cuda-{profile['ver']}"])
            add_asset(["cudart-", "bin-win", f"cuda-{profile['ver']}"])
        elif profile['type'] == 'hip':
            add_asset(["llama-", "bin-win", "hip"])
        else:
            if not add_asset(["bin-win", "vulkan-x64"]):
                add_asset(["bin-win", "cpu-x64"])

        if not assets_to_fetch:
            raise RuntimeError("No matching hardware assets found in upstream release.")

        if not extract_source_topology(release['tag_name']):
            raise RuntimeError("Source topology acquisition failed.")

        for asset in assets_to_fetch:
            pattern = re.escape(asset['name']) + r".*?sha256:\s*([a-fA-F0-9]{64})"
            match = re.search(pattern, release.get('body', ''), re.DOTALL | re.IGNORECASE)
            expected_hash = match.group(1).lower() if match else None
            
            if not extract_binary_topology(asset['browser_download_url'], expected_hash):
                raise RuntimeError(f"Binary asset acquisition failed: {asset['name']}")

        if not synchronize_gguf_library():
            raise RuntimeError("Local Python dependency synchronization failed.")

        converge_structure()
        
        is_valid, msg = verify_ensemble(profile)
        if is_valid:
            print_status(f"Hydration Complete. Status: {msg}", "SUCCESS")
        else:
            raise RuntimeError(f"Post-hydration verification failed: {msg}")

    except Exception as e:
        print_status(str(e), "ERROR")
        sys.exit(1)
    finally:
        resilient_purge(STAGING_DIR)

if __name__ == "__main__":
    main()