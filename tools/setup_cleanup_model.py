"""
One-time download of Qwen2.5-0.5B cleanup model (~397 MB).
Run: .\.venv\Scripts\python.exe tools\setup_cleanup_model.py
After this runs, Voxen cleanup works fully offline.
"""
import os
from pathlib import Path

MODEL_DIR  = Path(os.environ["LOCALAPPDATA"]) / "Voxen" / "models" / "qwen"
MODEL_FILE = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
HF_REPO    = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"

def main():
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    target = MODEL_DIR / MODEL_FILE
    if target.exists():
        print(f"Already present: {target}")
        return
    print(f"Downloading {MODEL_FILE} (~397 MB)…")
    from huggingface_hub import hf_hub_download
    hf_hub_download(
        repo_id=HF_REPO,
        filename=MODEL_FILE,
        local_dir=str(MODEL_DIR),
    )
    print("Done. Voxen cleanup is now active.")

if __name__ == "__main__":
    main()
