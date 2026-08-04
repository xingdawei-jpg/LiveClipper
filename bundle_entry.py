"""Runtime V4 adapter for the existing LiveClipper FastAPI application."""

from __future__ import annotations


def create_application(context):
    from server import app, configure_host_services, emit_log

    configure_host_services(context)

    return {
        "asgi_app": app,
        "emit_log": emit_log,
        "context": context,
    }
