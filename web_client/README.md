# LiveClipper Web Client

This is the clean Web-first client track for LiveClipper.

The old `live_cutter_web` folder is treated as a visual reference only. This
client keeps a clean split:

- `frontend/`: static HTML/CSS/JS app shell for the desktop/web UI.
- `server.py`: local FastAPI bridge to the existing Python processing modules.

## Run

```powershell
python -m pip install -r web_client\requirements.txt
python web_client\server.py
```

Then open:

```text
http://127.0.0.1:8765
```

Desktop shell:

```powershell
python web_client\desktop.py
```

## Product Direction

The desktop package can run this UI against a local FastAPI process. A later
online edition can keep most of the frontend structure and point selected APIs
at a cloud backend, while local-only workflows such as disk scanning, live
recording, and ffmpeg processing remain in the desktop client.
