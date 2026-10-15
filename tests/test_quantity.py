"""Tests for K8s-style quantity parsing."""

import pytest
from jupyterhub_storage_usage.quantity import parse_quantity, format_bytes


def test_binary_suffixes():
    assert parse_quantity("1Ki") == 1024
    assert parse_quantity("1Mi") == 1024 ** 2
    assert parse_quantity("1Gi") == 1024 ** 3
    assert parse_quantity("1Ti") == 1024 ** 4
    assert parse_quantity("10Gi") == 10737418240
    assert parse_quantity("500Mi") == 524288000


def test_decimal_suffixes():
    assert parse_quantity("1k") == 1000
    assert parse_quantity("1M") == 1000000
    assert parse_quantity("1G") == 1000000000
    assert parse_quantity("20G") == 20000000000
    assert parse_quantity("1T") == 1000000000000


def test_plain_bytes():
    assert parse_quantity("1024") == 1024
    assert parse_quantity("0") == 0
    assert parse_quantity("21474836480") == 21474836480


def test_decimal_values():
    assert parse_quantity("0.5Gi") == 536870912
    assert parse_quantity("1.5Gi") == 1610612736


def test_milli():
    # milli rounds to 0 for storage (1m = 0.001 bytes)
    assert parse_quantity("1m") == 0
    assert parse_quantity("1000m") == 1


def test_invalid():
    with pytest.raises(ValueError):
        parse_quantity("abc")
    with pytest.raises(ValueError):
        parse_quantity("")
    with pytest.raises(ValueError):
        parse_quantity("10Xi")


def test_format_bytes():
    assert format_bytes(0) == "0"
    assert format_bytes(1024) == "1Ki"
    assert format_bytes(536870912) == "512Mi"
    assert format_bytes(10737418240) == "10Gi"
    assert format_bytes(16106127360) == "15Gi"
    assert format_bytes(524288) == "512Ki"
    assert format_bytes(500) == "500"
