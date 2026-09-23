"""Opt-in API connectivity check; never logs key or error response bodies."""
import argparse
import json
from pathlib import Path
import sys
import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", choices=["openai", "nvidia"], default="openai")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    env = dotenv_values(root / ".env")
    prefix = args.provider.upper()
    key = env.get(prefix + "_API_KEY", "")
    if not key or not key.strip():
        print(prefix + "_API_KEY is not configured")
        return 2
    default_base = "https://integrate.api.nvidia.com/v1" if args.provider == "nvidia" else "https://api.openai.com/v1"
    base = (env.get(prefix + "_BASE_URL") or default_base).rstrip("/")
    try:
        with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {key}"}) as client:
            if args.model:
                body = {
                    "model": args.model, "messages": [{"role": "user", "content": "Reply exactly: OK"}],
                }
                body["max_tokens" if args.provider == "nvidia" else "max_completion_tokens"] = 128
                response = client.post(base + "/chat/completions", json=body)
                print("Probe HTTP", response.status_code)
                if response.is_success:
                    data = response.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    print(json.dumps({"model": data.get("model"), "nonempty_response": bool(content),
                                      "usage": data.get("usage")}, ensure_ascii=False))
                return 0 if response.is_success else 1
            response = client.get(base + "/models")
            print("Models HTTP", response.status_code)
            if response.is_success:
                models = sorted(item["id"] for item in response.json().get("data", [])
                                if args.provider == "nvidia" or item["id"].startswith(("gpt-", "o3", "o4")))
                print(json.dumps({"available_text_models": models}, ensure_ascii=False))
            return 0 if response.is_success else 1
    except httpx.RequestError as exc:
        print("Network error:", type(exc).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main())
