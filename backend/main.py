from fastapi import FastAPI
import database
from router import router as cleaning_router

app = FastAPI(
    title="Quarc Cleaning API",
    description="Household chore tracker — persons, sections, logs, analytics.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Initialise database tables on startup
database.init()

app.include_router(cleaning_router)


@app.get("/", tags=["status"])
def root():
    return {"status": "Quarc Cleaning is online"}
