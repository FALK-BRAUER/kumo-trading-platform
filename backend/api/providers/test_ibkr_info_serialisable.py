"""An IB instrument whose contract details carry an `IneligibilityReason` must still serialise into the
durable cache (#929). Red before the wrapper exists.

MEASURED 2026-09-11 04:07–04:11Z on ibkr-paper (Nautilus 1.229.0, ibapi 10.45.1): IB attached
`ineligibilityReasonList=[IneligibilityReason(id_='i152', description='Instrument temporarily unavailable
pending configuration review.')]` to OKE (conId 921971937) across the day roll. The IB adapter copies
`contract_details.dict()` into `Instrument.info` (`parsing/instruments.py:1000 contract_details_to_dict`);
its `_serialize_for_json` converts Decimal and Enum only, so the ibapi object survives; the durable cache's
`MsgSpecSerializer` has no hook for it and raises inside `DataEngine._handle_instrument → cache.add_instrument`;
the live DataEngine terminates the node. Six restarts before the container was stopped. The 20:17Z boot of
the same image logged the field EMPTY on all 591 instruments and survived. Boot-blocking for any IBKR
instance the moment IB flags any instrument in its universe.

THE TEST DRIVES THE REAL OBJECTS: ibapi's `IneligibilityReason`, the adapter's `contract_details_to_dict`,
and Nautilus's serializer — not a stand-in that could pass while production fails.
"""

from __future__ import annotations

import pytest

ibapi = pytest.importorskip("ibapi")
from ibapi.contract import ContractDetails  # noqa: E402
from ibapi.ineligibility_reason import IneligibilityReason  # noqa: E402
from nautilus_trader.adapters.interactive_brokers.parsing import instruments as parsing  # noqa: E402
from nautilus_trader.serialization.serializer import MsgSpecSerializer  # noqa: E402


def _details_with_reason():
    """The REAL conversion the provider runs: ibapi ContractDetails → Nautilus IBContractDetails
    (`parsing/instruments.py:318 contract_details_to_ib_contract_details`), whose
    `ineligibilityReasonList: list = None` is untyped, so the ibapi objects ride through."""
    cd = ContractDetails()
    cd.contract.symbol, cd.contract.secType, cd.contract.exchange, cd.contract.currency = "OKE", "STK", "SMART", "USD"
    cd.contract.conId, cd.contract.primaryExchange = 921971937, "NYSE"
    cd.ineligibilityReasonList = [IneligibilityReason("i152", "Instrument temporarily unavailable pending configuration review.")]
    return parsing.contract_details_to_ib_contract_details(cd)


# -- fixture property first: the REAL objects reproduce the crash ---------------------------------------

def test_fixture_property_the_installed_adapter_leaves_the_ibapi_object_in_info_and_msgspec_refuses_it():
    """If this ever passes, Nautilus fixed it upstream and the wrapper below can go.

    Reads the adapter's ORIGINAL function (`__wrapped__` once a sibling test has installed the
    sanitiser in this process) — the property is about Nautilus, not about our wrapper."""
    original = getattr(parsing.contract_details_to_dict, "__wrapped__", parsing.contract_details_to_dict)
    info = original(_details_with_reason())
    reasons = info.get("ineligibilityReasonList")
    assert reasons and isinstance(reasons[0], IneligibilityReason), reasons
    import msgspec
    with pytest.raises(TypeError, match="IneligibilityReason"):
        MsgSpecSerializer(encoding=msgspec.msgpack).serialize(_equity_from(info))  # the cache's own serializer


def _equity_from(info):
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.objects import Currency, Price, Quantity
    return Equity(
        instrument_id=InstrumentId(Symbol("OKE"), Venue("XNYS")), raw_symbol=Symbol("OKE"),
        currency=Currency.from_str("USD"), price_precision=2, price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1), ts_event=0, ts_init=0, info=info)


# -- the wrapper: installed by the IB connector, resolved at call time, no fork ---------------------------

def test_with_the_wrapper_installed_the_reason_becomes_a_dict_and_the_instrument_serialises():
    from api.providers.ibkr import install_info_sanitiser
    install_info_sanitiser()
    info = parsing.contract_details_to_dict(_details_with_reason())
    assert info["ineligibilityReasonList"] == [{"id": "i152", "description": "Instrument temporarily unavailable pending configuration review."}]
    import msgspec
    raw = MsgSpecSerializer(encoding=msgspec.msgpack).serialize(_equity_from(info))
    assert isinstance(raw, (bytes, bytearray)) and len(raw) > 0


def test_the_wrapper_is_idempotent_and_leaves_an_empty_list_alone():
    from api.providers.ibkr import install_info_sanitiser
    install_info_sanitiser(); install_info_sanitiser()
    cd = ContractDetails(); cd.contract.symbol = "GLD"; cd.contract.conId = 51529211; cd.ineligibilityReasonList = []
    info = parsing.contract_details_to_dict(parsing.contract_details_to_ib_contract_details(cd))
    assert info["ineligibilityReasonList"] == []
    assert info["contract"]["conId"] == 51529211


def test_the_connector_installs_the_sanitiser_when_it_builds_the_ib_spec():
    """Seam: the wrapper is not a helper someone must remember — building the IBKR data spec installs it."""
    import api.providers.ibkr as ib
    installed = []
    orig = ib.install_info_sanitiser
    ib.install_info_sanitiser = lambda: installed.append(True) or orig()
    try:
        spec = ib.build_data({"ibg_client_id": 2})   # the REAL spec builder the node calls at boot
    finally:
        ib.install_info_sanitiser = orig
    assert installed == [True], "build_data did not install the sanitiser — a wrapper nobody calls"
    assert spec is not None
