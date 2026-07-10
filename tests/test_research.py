from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.errors import GenerationError
from inky_bird_frame.research import ResearchBudget


class ResearchBudgetTests(unittest.TestCase):
    def test_enforces_daily_and_per_species_limits(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            budget = ResearchBudget(Path(temporary) / "budget.json", daily_limit=3, species_limit=2)
            budget.consume(1, now)
            budget.consume(1, now)
            with self.assertRaisesRegex(GenerationError, "research limit"):
                budget.consume(1, now)
            budget.consume(2, now)
            with self.assertRaisesRegex(GenerationError, "Daily research limit"):
                budget.consume(3, now)

    def test_new_utc_day_resets_budget(self) -> None:
        now = datetime(2026, 7, 10, 23, 59, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            budget = ResearchBudget(Path(temporary) / "budget.json", daily_limit=1, species_limit=1)
            budget.consume(1, now)
            budget.consume(1, now + timedelta(minutes=2))


if __name__ == "__main__":
    unittest.main()
