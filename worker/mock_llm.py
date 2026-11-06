"""Development-only fake LLM to test task orchestration without GPU/model downloads."""
import os
import time

from fastapi import FastAPI
from pydantic import BaseModel

# Optional artificial latency so tests can interrupt a running job.
DELAY = float(os.environ.get("CAG_MOCK_DELAY_SECONDS", "0"))
if not 0 <= DELAY <= 30:
    raise RuntimeError("CAG_MOCK_DELAY_SECONDS must be between 0 and 30")

app = FastAPI(title="MOCK MODEL — NEVER USE FOR REAL INFERENCE")

class DemoRequest(BaseModel):
    model: str
    messages: list[dict]
    max_tokens: int = 128

@app.post("/v1/chat/completions")
def completions(req: DemoRequest):
    time.sleep(DELAY)
    last = next((m.get("content", "") for m in reversed(req.messages) if m.get("role") == "user"), "")
    return {"id": "mock", "object": "chat.completion", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "[MOCK – no AI calculation] Echo: " + str(last)[:300]},
            "finish_reason": "stop"}], "usage": {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}}
