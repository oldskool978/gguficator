import os
import sys
import urllib.request
import urllib.error

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(PROJECT_ROOT, "static", "lib", "markdown")

# Augmented to include Source Maps for zero-error console logging during DevTools inspection
ASSETS = {
    "marked.min.js": "https://cdn.jsdelivr.net/npm/marked@12.0.1/marked.min.js",
    "purify.min.js": "https://cdn.jsdelivr.net/npm/dompurify@3.0.9/dist/purify.min.js",
    "purify.min.js.map": "https://cdn.jsdelivr.net/npm/dompurify@3.0.9/dist/purify.min.js.map"
}

def print_status(msg, status="INFO"):
    colors = {"INFO": "\033[94m", "SUCCESS": "\033[92m", "ERROR": "\033[91m", "RESET": "\033[0m"}
    print(f"{colors.get(status, '')}[{status}] {msg}{colors['RESET']}")

def verify_idempotency():
    for filename in ASSETS.keys():
        if not os.path.exists(os.path.join(TARGET_DIR, filename)):
            return False
    return True

def download_file(url: str, dest: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RLM-Hydrator"})
        with urllib.request.urlopen(req, timeout=15) as response, open(dest, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except urllib.error.URLError as e:
        print_status(f"Network error fetching {url}: {e}", "ERROR")
        return False
    except OSError as e:
        print_status(f"IO error writing to {dest}: {e}", "ERROR")
        return False

def main():
    print("="*60)
    print("       RLM PIPELINE MARKDOWN HYDRATION PROTOCOL       ")
    print("="*60)

    if verify_idempotency():
        print_status("Markdown subsystem already hydrated. Bypassing execution.", "SUCCESS")
        sys.exit(0)

    if not os.path.exists(TARGET_DIR):
        os.makedirs(TARGET_DIR)

    for filename, url in ASSETS.items():
        dest_path = os.path.join(TARGET_DIR, filename)
        print_status(f"Fetching {filename}...", "INFO")
        
        if download_file(url, dest_path):
            print_status(f"Localized: {filename}", "SUCCESS")
        else:
            print_status("Hydration failed. Aborting.", "ERROR")
            sys.exit(1)

    print_status("Dependencies successfully localized.", "SUCCESS")

if __name__ == "__main__":
    main()