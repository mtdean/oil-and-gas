import pandas as pd

from ogdata.schema import api10_from, api14_from, conform


def test_api_helpers():
    s = pd.Series(["33-053-03901-00-00", "3305303901", None, "42001-12345"])
    assert api14_from(s).tolist()[0] == "33053039010000"
    assert api10_from(s).tolist()[1] == "3305303901"
    assert pd.isna(api10_from(s).tolist()[2])
    assert api10_from(s).tolist()[3] == "4200112345"


def test_conform_adds_missing_and_casts():
    df = pd.DataFrame({"entity_id": ["a"], "period": ["2024-01-01"], "oil_bbl": ["12.5"], "spud": ["x"]})
    out = conform(df, "production_monthly")
    assert out["oil_bbl"].iloc[0] == 12.5
    assert str(out["period"].dtype).startswith("datetime64")
    assert "gas_mcf" in out.columns and pd.isna(out["gas_mcf"].iloc[0])
    assert "spud" not in out.columns


def test_conform_drops_sentinel_dates():
    df = pd.DataFrame({"well_id": ["w"], "spud_date": ["1083-01-03"], "completion_date": ["9999-12-31"]})
    out = conform(df, "wells")
    assert pd.isna(out["spud_date"].iloc[0]) and pd.isna(out["completion_date"].iloc[0])
