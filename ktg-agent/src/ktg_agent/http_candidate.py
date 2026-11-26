"""Single Uvicorn startup implementation used by the canonical server module."""
import uvicorn
from ktg_agent.http_app import create_app
from ktg_agent.server import Settings


def run(settings: Settings | None = None) -> None:
    configuration = settings if settings is not None else Settings.from_env()
    uvicorn.run(create_app(configuration), host=configuration.host,
                port=configuration.port, workers=1, reload=False, lifespan="on", access_log=False)
