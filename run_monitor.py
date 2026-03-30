#run_monitor.py
import uvicorn
import os
import sys

if __name__ == "__main__":
    # Only set working directory in development
    if not getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    from frontend.web_app import app
    uvicorn.run(app, host="0.0.0.0", port=8000)