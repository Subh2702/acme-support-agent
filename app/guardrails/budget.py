"""Global daily LLM budget (in paise), measured from the trace table.

The day starts at midnight IST. When spend reaches the cap, the public demo
answers with a friendly "demo limit reached" message instead of calling the LLM.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select

from app.db.models import TraceEvent
from app.db.session import Database
from app.timeutil import start_of_ist_day, utcnow

CACHE_TTL_S = 5.0


class BudgetGuard:
    def __init__(
        self,
        db: Database,
        daily_budget_paise: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.daily_budget_paise = daily_budget_paise
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, int] | None = None

    def spent_today_paise(self) -> int:
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < CACHE_TTL_S:
                return self._cached[1]
        since = start_of_ist_day(self.clock())
        with self.db.session() as session:
            spent = session.scalar(
                select(func.coalesce(func.sum(TraceEvent.cost_paise), 0)).where(
                    TraceEvent.created_at >= since
                )
            )
        value = int(spent or 0)
        with self._lock:
            self._cached = (time.monotonic(), value)
        return value

    def exhausted(self) -> bool:
        return self.spent_today_paise() >= self.daily_budget_paise

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None
