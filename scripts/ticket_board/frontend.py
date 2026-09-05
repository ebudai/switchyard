"""Tenant-aware frontend for the ticket-board browser UI."""

from __future__ import annotations

import html
import json

from .frontend_markup import MARKUP
from .frontend_script_app import SCRIPT_APP
from .frontend_script_core import SCRIPT_CORE
from .frontend_script_detail import SCRIPT_DETAIL
from .frontend_style import STYLE


def _inline_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html(*, project: str = "pgu", project_name: str = "PGU", ticket_prefix: str = "PGU") -> str:
    normalized_project = str(project or "pgu").strip().lower() or "pgu"
    normalized_name = str(project_name or normalized_project).strip() or normalized_project
    normalized_prefix = str(ticket_prefix or normalized_project).strip().upper() or "PGU"
    board_title = f"{normalized_name} Ticket Board"
    markup = MARKUP.replace("__TICKET_BOARD_HEADING__", html.escape(board_title), 1)
    identity_script = _inline_json(
        {"project": normalized_project, "projectName": normalized_name, "ticketPrefix": normalized_prefix}
    )
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        f"  <title>{html.escape(board_title)}</title>\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <style>\n"
        + STYLE
        + "  </style>\n"
        + "</head>\n"
        + "<body>\n"
        + markup
        + "  <script>\n"
        + f"    const BOARD_IDENTITY = Object.freeze({identity_script});\n"
        + SCRIPT_CORE
        + SCRIPT_DETAIL
        + SCRIPT_APP
        + "  </script>\n"
        + "</body>\n"
        + "</html>\n"
    )


HTML = render_html()
