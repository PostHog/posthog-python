import pytest

def validate_distinct_id(distinct_id: str) -> str:
    if distinct_id is None:
        raise ValueError("distinct_id cannot be None")
    s = str(distinct_id).strip()
    if not s:
        raise ValueError("distinct_id cannot be empty")
    return s

def test_valid_distinct_id():
    assert validate_distinct_id("user_12345") == "user_12345"
    assert validate_distinct_id("  auth0|9876  ") == "auth0|9876"

def test_invalid_distinct_id():
    with pytest.raises(ValueError, match="distinct_id"):
        validate_distinct_id("")
    with pytest.raises(ValueError, match="distinct_id"):
        validate_distinct_id("   ")
