"""HTTP route modules.

`app.py` stays as the assembly point (app object, lifespan, middleware,
startup); each module here owns one area of the API surface and exports a
router that `app.py` includes. Splitting it this way keeps the request
handlers readable and makes it obvious where to add the next endpoint.
"""
from __future__ import annotations

from .chat import router as chat_router
from .library import router as library_router
from .models import router as models_router
from .pages import router as pages_router
from .settings import router as settings_router
from .synthesis import router as synthesis_router
from .voices import router as voices_router

ALL_ROUTERS = (
    pages_router, voices_router, synthesis_router,
    library_router, models_router, settings_router, chat_router,
)

__all__ = ["ALL_ROUTERS"]
