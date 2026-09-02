import os
import re
import websocket

url = os.environ["ESPN_DRAFT_WS_URL"]

def printable(data):
    if isinstance(data, bytes):
        return data.decode("latin-1", errors="ignore")
    return data

def on_open(ws):
    print("CONNECTED — listening for picks")

def on_message(ws, message):
    text = printable(message)

    # The useful live events are plain text embedded in frames.
    if "SELECTED" in text or "SELECTING" in text or "CLOCK" in text or "AUTOSUGGEST" in text:
        clean = re.sub(r"[^\x20-\x7E\r\n\t]", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        print(clean[-500:])

def on_error(ws, error):
    print("ERROR:", error)

def on_close(ws, code, msg):
    print("CLOSED:", code, msg)

ws = websocket.WebSocketApp(
    url,
    header=[
        "Origin: https://fantasy.espn.com",
        "User-Agent: Mozilla/5.0",
    ],
    on_open=on_open,
    on_message=on_message,
    on_error=on_error,
    on_close=on_close,
)

ws.run_forever(ping_interval=20, ping_timeout=10)
