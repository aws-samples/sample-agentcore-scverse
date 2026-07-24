"""
Thin OpenAI-compatible proxy for AgentCore Runtime.

Translates POST /v1/chat/completions into invoke_agent_runtime calls
so Open WebUI can talk to the scverse agent without modification.
"""

import json
import os
import time
import uuid

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

client = boto3.client("bedrock-agentcore", region_name=REGION)


@app.get("/v1/models")
async def list_models():
    """Return a fake model list so Open WebUI discovers the agent."""
    return {
        "object": "list",
        "data": [
            {
                "id": "scverse-research-agent",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "agentcore",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Translate OpenAI chat completions to AgentCore invoke."""
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # Extract the last user message as the prompt
    prompt = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            prompt = msg.get("content", "")
            break

    if not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "No user message found"}},
        )

    # Build conversation history for context
    history = []
    for msg in messages[:-1]:  # exclude the last message (already in prompt)
        history.append({"role": msg["role"], "content": msg.get("content", "")})

    payload = {"prompt": prompt}
    if history:
        payload["history"] = history

    session_id = f"openwebui-{uuid.uuid4().hex[:16]}-{uuid.uuid4().hex[:16]}"

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            payload=json.dumps(payload),
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
        )

        # AgentCore returns SSE event stream in resp['response']
        stream_body = response.get("response")
        if stream_body and hasattr(stream_body, "read"):
            raw = stream_body.read().decode("utf-8", errors="replace")
        else:
            raw = ""

        # Parse SSE: each line is 'data: "chunk"' or 'data: chunk'
        chunks = []
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                content = line[6:]  # remove 'data: ' prefix
                # Remove surrounding quotes if present
                if content.startswith('"') and content.endswith('"'):
                    content = json.loads(content)  # handles escape sequences
                chunks.append(content)

        response_text = "".join(chunks)

        if not response_text.strip():
            response_text = "The agent processed your request but returned an empty response. Please try again."

    except Exception as e:
        response_text = f"Error invoking agent: {str(e)}"

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if stream:
        return StreamingResponse(
            _stream_response(completion_id, response_text),
            media_type="text/event-stream",
        )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "scverse-research-agent",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(response_text.split()),
            "total_tokens": len(prompt.split()) + len(response_text.split()),
        },
    }


async def _stream_response(completion_id: str, text: str):
    """Yield SSE chunks in OpenAI streaming format."""
    # Send content in a single chunk (AgentCore doesn't stream to us)
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "scverse-research-agent",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk)}\n\n"

    # Send stop chunk
    stop_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "scverse-research-agent",
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ],
    }
    yield f"data: {json.dumps(stop_chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health():
    return {"status": "ok"}
