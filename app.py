import os
import requests
from fastapi import FastAPI, Request, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Allow Power BI (any origin) to call this app with credentials
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# In-memory store for agent mode: conversationId -> messageId
_agent_messages: dict = {}

# ─── Databricks config from environment variables ─────────────────────────────
DATABRICKS_HOST  = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")


# ─── helpers ──────────────────────────────────────────────────────────────────

def genie_api(method: str, path: str, body: dict = None, params: dict = None):
    """Call Databricks REST API using PAT from environment variables."""
    headers = {
        "Authorization": f"Bearer {DATABRICKS_TOKEN}",
        "Content-Type":  "application/json",
    }
    url = DATABRICKS_HOST + path
    if method.upper() == "POST":
        r = requests.post(url, headers=headers, json=body or {}, timeout=60)
    else:
        r = requests.get(url, headers=headers, params=params or {}, timeout=60)

    # Surface the actual Databricks error instead of a cryptic JSON parse error
    if not r.ok:
        raise Exception(f"Databricks returned HTTP {r.status_code}: {r.text[:500]}")

    if not r.text.strip():
        raise Exception(f"Databricks returned empty body for {method} {path}")

    try:
        return r.json()
    except Exception:
        raise Exception(f"Databricks returned non-JSON ({r.status_code}): {r.text[:200]}")


def fetch_query_result(space_id, conversation_id, message_id):
    """Return (columns, column_types, rows) from a completed Genie query."""
    try:
        qr = genie_api(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}"
            f"/messages/{message_id}/query-result",
        )
        stmt   = qr.get("statement_response", {})
        schema = stmt.get("manifest", {}).get("schema", {})
        cols   = [c.get("name", "") for c in schema.get("columns", [])]
        types  = [c.get("type_name", "STRING") for c in schema.get("columns", [])]
        rows   = stmt.get("result", {}).get("data_array", [])
        return cols, types, rows
    except Exception:
        return [], [], []


def parse_attachments(attachments: list):
    """Extract text, sql, description from Genie message attachments."""
    text = sql = description = None
    for att in attachments:
        if "text" in att:
            text = att["text"].get("content")
        if "query" in att:
            sql         = att["query"].get("query")
            description = att["query"].get("description")
    return text, sql, description


# ─── health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    configured = bool(DATABRICKS_HOST and DATABRICKS_TOKEN)
    return {
        "status": "ok",
        "databricks_host_set": bool(DATABRICKS_HOST),
        "databricks_token_set": bool(DATABRICKS_TOKEN),
        "configured": configured,
    }


# ─── debug (temporary) ────────────────────────────────────────────────────────

@app.get("/debug")
def debug():
    """Temporary: verify PAT works by calling a simple Databricks endpoint."""
    try:
        r = requests.get(
            f"{DATABRICKS_HOST}/api/2.0/token/list",
            headers={"Authorization": f"Bearer {DATABRICKS_TOKEN}"},
            timeout=10
        )
        return {
            "status_code": r.status_code,
            "host": DATABRICKS_HOST,
            "token_first_5": DATABRICKS_TOKEN[:5] if DATABRICKS_TOKEN else "EMPTY",
            "token_length": len(DATABRICKS_TOKEN),
            "response_preview": r.text[:200]
        }
    except Exception as e:
        return {"error": str(e)}


# ─── chat endpoints ───────────────────────────────────────────────────────────

@app.post("/api/spm/chat/ask")
async def chat_ask(request: Request):
    try:
        body            = await request.json()
        question        = body.get("question", "")
        conversation_id = body.get("conversationId")
        space_id        = body.get("spaceId", "")

        if conversation_id:
            result = genie_api(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages",
                body={"content": question},
            )
            return {"conversationId": conversation_id, "messageId": result.get("id")}
        else:
            result = genie_api(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/start-conversation",
                body={"content": question},
            )
            return {
                "conversationId": result.get("conversation_id"),
                "messageId":      result.get("message_id"),
            }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/spm/chat/poll")
async def chat_poll(
    conversationId: str = Query(...),
    messageId:      str = Query(...),
    spaceId:        str = Query(...),
):
    try:
        result = genie_api(
            "GET",
            f"/api/2.0/genie/spaces/{spaceId}/conversations/{conversationId}/messages/{messageId}",
        )
        status = result.get("status", "SUBMITTED")

        if status != "COMPLETED":
            return {"status": status}

        text, sql, description = parse_attachments(result.get("attachments", []))

        columns = column_types = rows = []
        if sql:
            columns, column_types, rows = fetch_query_result(spaceId, conversationId, messageId)

        return {
            "status":      "COMPLETED",
            "text":        text,
            "description": description,
            "sql":         sql,
            "columns":     columns,
            "columnTypes": column_types,
            "rows":        rows,
            "rowCount":    len(rows),
            "truncated":   len(rows) >= 1000,
        }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}


# ─── agent / research endpoints ───────────────────────────────────────────────

@app.post("/api/spm/agent/ask")
async def agent_ask(request: Request):
    try:
        body            = await request.json()
        question        = body.get("question", "")
        conversation_id = body.get("conversationId")
        space_id        = body.get("spaceId", "")

        if conversation_id:
            result = genie_api(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages",
                body={"content": question},
            )
            message_id = result.get("id")
        else:
            result = genie_api(
                "POST",
                f"/api/2.0/genie/spaces/{space_id}/start-conversation",
                body={"content": question},
            )
            conversation_id = result.get("conversation_id")
            message_id      = result.get("message_id")

        _agent_messages[conversation_id] = message_id
        return {"conversationId": conversation_id, "messageId": message_id}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/spm/agent/poll")
async def agent_poll(
    conversationId: str = Query(...),
    spaceId:        str = Query(...),
):
    try:
        message_id = _agent_messages.get(conversationId)
        if not message_id:
            return {"status": "FAILED", "error": "Unknown conversation. Re-ask your question."}

        result = genie_api(
            "GET",
            f"/api/2.0/genie/spaces/{spaceId}/conversations/{conversationId}/messages/{message_id}",
        )
        status = result.get("status", "SUBMITTED")

        if status != "COMPLETED":
            return {
                "status":     status,
                "reasoning":  "Genie is analysing your question…",
                "queriesRun": 0,
            }

        text, sql, description = parse_attachments(result.get("attachments", []))

        columns = column_types = rows = []
        if sql:
            columns, column_types, rows = fetch_query_result(spaceId, conversationId, message_id)

        report = []
        if columns:
            report.append({
                "type":        "table",
                "columns":     columns,
                "columnTypes": column_types,
                "rows":        rows,
                "rowCount":    len(rows),
                "sql":         sql,
            })
        if text:
            report.append({"type": "text", "text": text})
        elif description:
            report.append({"type": "text", "text": description})

        return {
            "status":     "COMPLETED",
            "queriesRun": 1 if sql else 0,
            "report":     report,
            "text":       text,
        }
    except Exception as e:
        return {"status": "FAILED", "error": str(e)}


# ─── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
