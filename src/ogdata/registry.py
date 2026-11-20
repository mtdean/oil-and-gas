"""All known sources. Add a module under sources/ and list its class here."""
from __future__ import annotations

from .sources.base import Source
from .sources import nd, tx, nm, co, wy, ut, mt, pa, oh, wv, ks, ok, la, ca, ak, bsee, fracfocus
from .sources import ab, sk, bc, camacro, sd
from .sources import eia_bulk, eia, fred, cftc, bakerhughes, onrr, dallasfed

_CLASSES: list[type[Source]] = [
    # well-level / entity-level production + locations (ordered roughly by production volume)
    tx.Texas, nm.NewMexico, nd.NorthDakota, co.Colorado, ok.Oklahoma, wy.Wyoming, pa.Pennsylvania, la.Louisiana,
    ut.Utah, oh.Ohio, wv.WestVirginia, mt.Montana, ks.Kansas, sd.SouthDakota, ca.California, ak.Alaska, bsee.BSEE, fracfocus.FracFocus,
    ab.Alberta, sk.Saskatchewan, bc.BritishColumbia,
    # macro
    eia_bulk.EIABulk, eia.EIA, fred.FRED, cftc.CFTC, bakerhughes.BakerHughes, onrr.ONRR, dallasfed.DallasFed, camacro.CanadaMacro,
]

_instances: dict[str, Source] | None = None


def all_sources() -> list[Source]:
    global _instances
    if _instances is None:
        _instances = {c.name: c() for c in _CLASSES}
    return list(_instances.values())


def get(name: str) -> Source:
    for s in all_sources():
        if s.name == name:
            return s
    raise SystemExit(f"unknown source '{name}'. known: {', '.join(s.name for s in all_sources())}")
