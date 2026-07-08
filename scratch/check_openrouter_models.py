# check_openrouter_models.py
import urllib.request
import json
import os

api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key:
    # Try reading from .env
    if os.path.exists(".env"):
        for line in open(".env"):
            if line.startswith("OPENROUTER_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

if not api_key:
    print("No OpenRouter API key found!")
    exit(1)

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/models",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
)

try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        models = data.get("data", [])
        print(f"Total models returned: {len(models)}")
        
        # Filter for qwen models
        qwen_models = [m["id"] for m in models if "qwen" in m["id"]]
        print("\nAvailable Qwen models:")
        for m in sorted(qwen_models):
            print(f"- {m}")
            
except Exception as e:
    print(f"Error fetching models: {e}")
