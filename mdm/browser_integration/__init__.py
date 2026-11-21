"""
Browser integration (Stage 6A): Chrome / Edge extension -> native messaging
host -> authenticated localhost IPC -> existing QueueManager.

``request_validator`` and ``ipc_protocol`` are pure standard-library modules
shared with the native messaging host. Qt-dependent modules (``ipc_server``,
``service``) are intentionally NOT imported here so the native host can import
this package without pulling in PySide6.
"""
import logging as _logging

# Library-style logging: the application attaches a rotating file handler in
# app.py; tokens and full URLs are never logged.
_logging.getLogger("mdm.browser_integration").addHandler(_logging.NullHandler())
