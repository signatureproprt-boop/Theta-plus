import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List
import uuid
from datetime import datetime


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
from lib.db import client, db, ensure_indexes


# Startup runs before the yield, shutdown after it. Add your own setup/teardown here.
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.index_task = asyncio.create_task(ensure_indexes())  # background: a big index build must not block boot
    from pipeline import get_pipeline  # local import: pipeline imports routers' siblings, not server

    pipeline = get_pipeline()
    pipeline.start()  # fails safe: tick/flush loops contain their own errors
    try:
        yield
    finally:
        await pipeline.stop()
        client.close()


# Create the main app without a prefix
app = FastAPI(lifespan=lifespan)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    _ = await db.status_checks.insert_one(status_obj.model_dump())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.status_checks.find().to_list(1000)
    return [StatusCheck(**status_check) for status_check in status_checks]

from routers.config_router import router as config_router
from routers.dashboard_router import router as dashboard_router
from routers.replay_router import router as replay_router
from routers.signal_router import router as signal_router

api_router.include_router(signal_router)   # Phase C: read-only signal research harness
api_router.include_router(config_router)
api_router.include_router(dashboard_router)  # Phase D: control-room payload + logs
api_router.include_router(replay_router)     # Phase E: offline replay + backtest metrics
from routers.chart_router import router as chart_router  # noqa: E402

api_router.include_router(chart_router)      # Phase F: chart markers (visualization only)
from routers.paper_router import router as paper_router  # noqa: E402

api_router.include_router(paper_router)      # Phase G: alerts log + PAPER trading (read-only)
from routers.execution_router import router as execution_router  # noqa: E402

api_router.include_router(execution_router)  # Phase H: execution status/log (read-only; no arming API)
from routers.research_router import router as research_router  # noqa: E402

api_router.include_router(research_router)   # Phase I: real-data validation + readiness (read-only)

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
