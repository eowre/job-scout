"""
test_location_filter.py — Unit tests for the NAM location filter.
"""
import pytest
from services.scraper_service import _location_filter


# include → NAM or unknown
@pytest.mark.parametrize("loc", [
    "",
    "San Francisco, CA",
    "New York, NY",
    "Austin, TX",
    "United States",
    "US",
    "USA",
    "Toronto, Canada",
    "Mexico City, Mexico",
    "North America",
    "Remote, US",
    "Remote – United States",
])
def test_include(loc):
    assert _location_filter(loc) == "include", f"Expected include for {loc!r}"


# remote → no parse
@pytest.mark.parametrize("loc", [
    "Remote",
    "remote",
    "REMOTE",
    "Distributed",
    "Work From Anywhere",
    "Anywhere",
    "Worldwide",
    "Global",
])
def test_remote(loc):
    assert _location_filter(loc) == "remote", f"Expected remote for {loc!r}"


# skip → non-NAM, discard
@pytest.mark.parametrize("loc", [
    "London, UK",
    "Berlin, Germany",
    "Paris, France",
    "Amsterdam, Netherlands",
    "Dublin, Ireland",
    "Sydney, Australia",
    "Singapore",
    "Tokyo, Japan",
    "Bangalore, India",
    "Remote – UK",
    "EMEA",
    "Europe",
    "APAC",
])
def test_skip(loc):
    assert _location_filter(loc) == "skip", f"Expected skip for {loc!r}"
