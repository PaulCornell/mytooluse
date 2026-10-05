import pytest

from evals.graders import extract_numbers, grade_contains_all, grade_numeric


def test_extract_numbers_handles_formatting():
    assert extract_numbers("There are 299,705 EVs; Tesla is 41.0% and range 277.1 mi (v2).") == [299705, 41.0, 277.1]


@pytest.mark.parametrize("answer,expected,kw,ok", [
    ("Teslas are 41.03% of EVs.", 41.034, {"abs_tol": 0.1}, True),
    ("Teslas are about 41%.", 41.034, {"abs_tol": 0.1}, True),
    ("Teslas are 40%.", 41.034, {"abs_tol": 0.1}, False),
    ("Total: 299,705 vehicles", 299705, {"rel_tol": 0.0}, True),
    ("Total: 299,700 vehicles", 299705, {"rel_tol": 0.0}, False),
])
def test_grade_numeric(answer, expected, kw, ok):
    assert grade_numeric(answer, expected, **kw).passed is ok


def test_grade_contains_all():
    assert grade_contains_all("Tesla, then Chevrolet and Ford", ["TESLA", "CHEVROLET", "FORD"]).passed
    result = grade_contains_all("Tesla and Ford", ["TESLA", "CHEVROLET"])
    assert not result.passed and "CHEVROLET" in result.detail
