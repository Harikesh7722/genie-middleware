from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import time

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GENIE_SPACE_ID = "01f1bc030cfd187b962995a95ebc456d"

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/ask")
async def ask(request: Request):
    try:
        # Initialize client inside the request — avoids startup crash
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()

        body     = await request.json()
        question = body.get("question", "")
        mode     = body.get("mode", "code")

        # Start conversation
        convo = w.genie.start_conversation(
            space_id=GENIE_SPACE_ID,
            content=question
        )

        # Poll until complete (max 60 seconds)
        for _ in range(60):
            status = w.genie.get_message(
                space_id=GENIE_SPACE_ID,
                conversation_id=convo.conversation_id,
                message_id=convo.message_id
            )
            if status.status == "COMPLETED":
                break
            time.sleep(1)

        return {
            "mode":   mode,
            "sql":    getattr(status, "sql_query", None),
            "result": getattr(status, "result", None),
            "chart":  getattr(status, "attachments", None)
        }

    except Exception as e:
        return {"error": str(e)}
