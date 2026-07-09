"""Static frontend for the ticket-board browser UI."""

from .frontend_markup import MARKUP
from .frontend_script_app import SCRIPT_APP
from .frontend_script_core import SCRIPT_CORE
from .frontend_script_detail import SCRIPT_DETAIL
from .frontend_style import STYLE

HTML = (
    "<!doctype html>\n"
    "<html lang=\"en\">\n"
    "<head>\n"
    "  <meta charset=\"utf-8\">\n"
    "  <title>PGU Ticket Board</title>\n"
    "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "  <style>\n"
    + STYLE
    + "  </style>\n"
    + "</head>\n"
    + "<body>\n"
    + MARKUP
    + "  <script>\n"
    + SCRIPT_CORE
    + SCRIPT_DETAIL
    + SCRIPT_APP
    + "  </script>\n"
    + "</body>\n"
    + "</html>\n"
)
