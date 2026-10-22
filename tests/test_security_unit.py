import pytest
from fastapi import HTTPException

from app.security import sign, verify


def test_hmac_rfc4231_test_case_1():
    # Published HMAC-SHA256 test vector, independent of this implementation.
    expected = "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    secret = "\x0b" * 20
    assert sign(b"Hi There", secret) == expected
    verify(b"Hi There", expected, secret)
    with pytest.raises(HTTPException) as exc:
        verify(b"Hi There\n", expected, secret)
    assert exc.value.status_code == 401
