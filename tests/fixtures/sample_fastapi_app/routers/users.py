from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from ..models import UserCreate, UserResponse

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/", response_model=List[UserResponse], summary="List all users")
def list_users(
    skip: int = Query(0, description="Offset for pagination"),
    limit: int = Query(10, description="Number of users to return"),
    role: Optional[str] = Query(None, description="Filter by user role"),
):
    """Retrieve a paginated list of registered users."""
    return []

@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED, summary="Create a new user")
def create_user(user: UserCreate):
    """Register a new user account."""
    return {"id": 1, "username": user.username, "email": user.email, "full_name": user.full_name}

@router.get("/{user_id}", response_model=UserResponse, summary="Get user by ID")
def get_user(user_id: int):
    """Fetch details of a specific user by their numerical ID."""
    return {"id": user_id, "username": "alice", "email": "alice@example.com"}

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete user")
def delete_user(user_id: int):
    """Delete a user account."""
    return None
