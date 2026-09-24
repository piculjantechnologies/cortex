"""WSGI entry point (`gunicorn run:app`); `python run.py` starts the development server."""

import os

from app import create_app

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is a development dependency (requirements-dev.txt)
    pass
else:
    load_dotenv()

app = create_app()

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
