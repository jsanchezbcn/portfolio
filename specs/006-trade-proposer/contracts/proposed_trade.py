from typing import List, Optional, Dict
from datetime import datetime
from sqlmodel import SQLModel, Field, Column, JSON

class ProposedTrade(SQLModel, table=True):
    __tablename__ = "proposed_trades"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: str = Field(index=True)
    strategy_name: str
    
    # Store legs as a JSON list of dicts: [{"conId": 123, "action": "BUY", "qty": 1}, ...]
    legs_json: List[Dict] = Field(default_factory=list, sa_column=Column(JSON))
    
    net_premium: float = 0.0
    margin_impact: float = 0.0
    efficiency_score: float = 0.0
    
    delta_reduction: float = 0.0
    vega_reduction: float = 0.0
    
    status: str = Field(default="Pending")  # Pending, Approved, Rejected, Superseded
    justification: str
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
