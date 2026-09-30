from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from databricks.sdk import WorkspaceClient   # pre-installed in Databricks Apps
import time

app = FastAPI()

# Allow Power BI to call this app cross-origin
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GENIE_SPACE_ID = "01f1bc030cfd187b962995a95ebc456d"   # from Genie URL

# WorkspaceClient auto-authenticates using the app's built-in identity
w = WorkspaceClient()

@app.post("/ask")
def ask(payload: dict):
    question = payload.get("question")
    mode     = payload.get("mode", "code")   # "code" or "image"

    # Start a conversation with Genie
    convo = w.genie.start_conversation(space_id=GENIE_SPACE_ID, content=question)

    # Genie is async — poll until complete
    while True:
        status = w.genie.get_message(
            space_id=GENIE_SPACE_ID,
            conversation_id=convo.conversation_id,
            message_id=convo.message_id
        )
        if status.status == "COMPLETED":
            break
        time.sleep(1)

    # Return result based on mode
    return {
        "mode":     mode,
        "sql":      status.sql_query,          # if mode == "code"
        "result":   status.result,             # tabular data
        "chart":    status.attachments         # if mode == "image"
    }