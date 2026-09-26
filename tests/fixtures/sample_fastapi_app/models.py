from typing import Optional
from pydantic import BaseModel, Field

class ItemCreate(BaseModel):
    name: str = Field(..., description="Name of the item")
    description: Optional[str] = Field(None, description="Detailed description")
    price: float = Field(..., description="Price in USD")
    is_active: bool = Field(True, description="Whether the item is active")

class ItemResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    price: float
    is_active: bool

class UserCreate(BaseModel):
    username: str
    email: str
    full_name: Optional[str] = None

class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    full_name: Optional[str] = None
