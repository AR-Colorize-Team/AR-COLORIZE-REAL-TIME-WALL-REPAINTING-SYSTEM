"""Pure-logic unit tests (no DB, no network)."""
import pytest

from app.core.rbac import Perm, Role, has_permission
from app.utils.color import delta_e_2000, hex_to_lab, normalize_hex

# Reference pairs from Sharma, Wu & Dalal (2005), "The CIEDE2000 Color-Difference Formula".
SHARMA = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
]


@pytest.mark.parametrize("lab1,lab2,expected", SHARMA)
def test_ciede2000_reference_values(lab1, lab2, expected):
    assert delta_e_2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)
    assert delta_e_2000(lab2, lab1) == pytest.approx(expected, abs=1e-4)   # symmetric


def test_hex_to_lab_known_points():
    assert hex_to_lab("#FFFFFF") == pytest.approx((100.0, 0.0, 0.0), abs=0.01)
    assert hex_to_lab("#000000") == pytest.approx((0.0, 0.0, 0.0), abs=0.01)
    assert hex_to_lab("#FF0000") == pytest.approx((53.24, 80.09, 67.20), abs=0.05)


@pytest.mark.parametrize("raw,expected", [("abc123", "#ABC123"), ("#abc123", "#ABC123"), (" #FFFFFF ", "#FFFFFF")])
def test_normalize_hex(raw, expected):
    assert normalize_hex(raw) == expected


@pytest.mark.parametrize("bad", ["#FFF", "GGGGGG", "#1234567", "", "rgb(0,0,0)"])
def test_normalize_hex_rejects(bad):
    with pytest.raises(ValueError):
        normalize_hex(bad)


def test_rbac_matrix():
    assert has_permission(Role.USER, Perm.PROJECTS_RW_OWN)
    assert has_permission(Role.USER, Perm.BILLING_MANAGE_OWN)
    assert not has_permission(Role.USER, Perm.USERS_MANAGE)
    assert not has_permission(Role.ORG_MEMBER, Perm.BILLING_MANAGE_OWN)   # billed via org
    assert has_permission(Role.ORG_ADMIN, Perm.CATALOG_MANAGE_ORG)
    assert not has_permission(Role.ORG_ADMIN, Perm.CATALOG_MANAGE_GLOBAL)
    assert has_permission(Role.SUPPORT, Perm.PROJECTS_READ_ANY)
    assert not has_permission(Role.SUPPORT, Perm.PROJECTS_RW_OWN)          # support is read-only
    assert all(has_permission(Role.ADMIN, p) for p in Perm)
    assert not has_permission("unknown-role", Perm.AI_USE)                  # fail closed
