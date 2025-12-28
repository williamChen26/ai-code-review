from __future__ import annotations

import pytest

from app.review.models import RiskPlan


def test_risk_plan_schema_ok() -> None:
    plan = RiskPlan.model_validate(
        {"highRiskFiles": ["a.py"], "reviewFocus": ["security"], "reviewDepth": "deep"}
    )
    assert plan.reviewDepth == "deep"


def test_risk_plan_schema_rejects_invalid_depth() -> None:
    with pytest.raises(Exception):
        RiskPlan.model_validate(
            {"highRiskFiles": ["a.py"], "reviewFocus": ["security"], "reviewDepth": "very-deep"}
        )


