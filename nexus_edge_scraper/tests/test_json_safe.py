import pandas as pd
from fastapi.responses import JSONResponse

from local_api import app
from local_api.json_safe import NaNSafeJSONResponse, sanitize_non_finite


def test_sanitize_replaces_non_finite_recursively() -> None:
    content = {
        "a": float("nan"),
        "b": [1.5, float("inf"), {"c": float("-inf")}],
        "d": (2.0, float("nan")),
        "e": "x",
        "f": None,
        "g": 3,
    }
    assert sanitize_non_finite(content) == {
        "a": None,
        "b": [1.5, None, {"c": None}],
        "d": [2.0, None],
        "e": "x",
        "f": None,
        "g": 3,
    }


def test_sanitize_handles_numpy_float64() -> None:
    # DataFrame.to_dict 產出的是 numpy.float64（float 子類別）
    records = pd.DataFrame({"v": [float("nan"), 1.5]}).to_dict(orient="records")
    assert sanitize_non_finite(records) == [{"v": None}, {"v": 1.5}]


def test_render_nan_becomes_null() -> None:
    assert NaNSafeJSONResponse({"a": float("nan"), "b": 1.0}).body == (
        b'{"a":null,"b":1.0}'
    )


def test_render_finite_content_identical_to_starlette() -> None:
    content = {"s": "中文", "n": [1, 2.5, None], "nested": {"k": True}}
    assert NaNSafeJSONResponse(content).body == JSONResponse(content).body


def test_app_uses_nan_safe_default_response_class() -> None:
    cls = app.router.default_response_class
    assert getattr(cls, "value", cls) is NaNSafeJSONResponse
