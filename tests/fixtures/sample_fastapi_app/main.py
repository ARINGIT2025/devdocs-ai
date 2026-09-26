from typing import List, Optional
from fastapi import FastAPI, HTTPException, Path, Query, status
from .models import ItemCreate, ItemResponse
from .routers.users import router as users_router

app = FastAPI(
    title="Sample FastAPI Application",
    description="A sample API for testing automated documentation generator",
    version="1.0.0",
)

app.include_router(users_router, prefix="/api/v1")

@app.get("/health", tags=["system"], summary="Health Check")
def health_check():
    """Return system health status."""
    return {"status": "ok", "service": "sample-fastapi-app"}

@app.get("/api/v1/items", response_model=List[ItemResponse], tags=["items"], summary="List Items")
def list_items(
    skip: int = Query(0, description="Offset count"),
    limit: int = Query(20, description="Limit count"),
    q: Optional[str] = Query(None, description="Search keyword"),
):
    """Retrieve items with filtering and pagination."""
    return []

@app.post("/api/v1/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED, tags=["items"], summary="Create Item")
def create_item(item: ItemCreate):
    """Create a new item in the catalog."""
    return {"id": 100, "name": item.name, "description": item.description, "price": item.price, "is_active": item.is_active}

@app.get("/api/v1/items/{item_id}", response_model=ItemResponse, tags=["items"], summary="Get Item by ID")
def get_item(item_id: int = Path(..., description="ID of the item")):
    """Fetch item by its unique integer identifier."""
    return {"id": item_id, "name": "Sample Item", "price": 29.99, "is_active": True}

@app.put("/api/v1/items/{item_id}", response_model=ItemResponse, tags=["items"], summary="Update Item")
def update_item(item_id: int, item: ItemCreate):
    """Update an existing item."""
    return {"id": item_id, "name": item.name, "description": item.description, "price": item.price, "is_active": item.is_active}

@app.delete("/api/v1/items/{item_id}", status_code=204, tags=["items"], summary="Delete Item")
def delete_item(item_id: int):
    """Remove item from catalog."""
    return None
