"""Public data models used by the desktop application.

The models deliberately contain only serialisable business data so that the
browser and GUI layers can exchange values without depending on Playwright or
openpyxl objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class MealInfo:
    """A meal extracted from an order line."""

    total_meals: Optional[int] = None
    grade: Optional[str] = None
    count: int = 1
    meal_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderInfo:
    """Normalised order data consumed by the Excel writer."""

    order_no: str
    name: str = ""
    phone: str = ""
    address: str = ""
    address_base_sheet: Optional[str] = None
    lunch: List[MealInfo] = field(default_factory=list)
    dinner: List[MealInfo] = field(default_factory=list)
    # Original delivery address and arbitrary metadata are useful for logs.
    delivery_address: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["lunch"] = [m.to_dict() for m in self.lunch]
        data["dinner"] = [m.to_dict() for m in self.dinner]
        return data

