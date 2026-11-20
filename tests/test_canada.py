import pandas as pd

from ogdata.sources import petrinex
from ogdata.sources.bc import _dms


def test_aer_uwi_to_petrinex_key():
    s = pd.Series(["00/08-09-051-25W4/0", "02/05-25-049-26W4/0", "F1/06-06-001-01W4/0", "garbage"])
    out = petrinex.aer_to_petrinex_uwi(s).tolist()
    assert out[0] == "100080905125W400"
    assert out[1] == "102052504926W400"
    assert out[2] == "1F1060600101W400"
    assert pd.isna(out[3])


def test_norm_uwi_strips_separators():
    assert petrinex.norm_uwi(pd.Series(["191/05-22-004-08W2/00"])).iloc[0] == "191052200408W200"


def test_bc_packed_dms():
    lat = _dms(pd.Series(["49085314"]), 2).iloc[0]
    lon = _dms(pd.Series(["123065320"]), 3).iloc[0]
    assert abs(lat - (49 + 8 / 60 + 53.14 / 3600)) < 1e-6
    assert abs(lon - (123 + 6 / 60 + 53.20 / 3600)) < 1e-6


def test_metric_conversions():
    assert abs(petrinex.M3_TO_BBL - 6.28981) < 1e-5
    assert abs(petrinex.E3M3_TO_MCF - 35.3147) < 1e-4
