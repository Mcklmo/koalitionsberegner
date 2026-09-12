"""The backend package.

Importing it loads ``.env`` into the environment (see :mod:`app.env`). That
belongs here rather than in ``main.py`` because ``main`` is not the only way in:
``uvicorn app.main:app``, ``pytest`` and any one-off script that imports
:mod:`app.config` must all see the same configuration, and a file read at one
entry point only is a file that works until somebody uses the other one.
"""

from .env import load_env_file

#: The file that was loaded and the names it set, reported by the startup log.
ENV_FILE_LOADED, ENV_NAMES_LOADED = load_env_file()
