"""各数据源的内部编号（港交所 stockId、巨潮 orgId、SEC CIK、东方财富 secid），查一次后缓存在库里。"""

from __future__ import annotations

from ..config import Listing
from . import cninfo, eastmoney, hkex, sec


def hkex_stock_id(db, session, listing: Listing) -> int | None:
    key = f"hkex_stock_id:{listing.code}"
    if v := db.kv_get(key):
        return int(v)
    sid = hkex.lookup_stock_id(session, listing.code)
    if sid:
        db.kv_set(key, str(sid))
    return sid


def cninfo_org_id(db, session, listing: Listing) -> str | None:
    key = f"cninfo_org_id:{listing.code}"
    if v := db.kv_get(key):
        return v
    org = cninfo.lookup_org_id(session, listing.code)
    if org:
        db.kv_set(key, org)
    return org


def sec_cik(db, session, listing: Listing, user_agent: str) -> int | None:
    key = f"sec_cik:{listing.code}"
    if v := db.kv_get(key):
        return int(v)
    cik = sec.lookup_cik(session, listing.code, user_agent)
    if cik:
        db.kv_set(key, str(cik))
    return cik


def em_secid(db, session, listing: Listing) -> str:
    if listing.market != "US" or listing.exchange:
        return eastmoney.secid_for(listing)
    key = f"em_secid:{listing.code}"
    if v := db.kv_get(key):
        return v
    secid = eastmoney.secid_for(listing, session)
    db.kv_set(key, secid)
    return secid
