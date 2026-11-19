from datetime import datetime


def _dt(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps require an offset")
    return result


def review(data):
    lots = {x["id"]: x for x in data["lots"]}
    offers = data["offers"]
    if len(lots) != len(data["lots"]) or len({x["id"] for x in offers}) != len(offers):
        raise ValueError("lot and offer IDs must be unique")
    grouped = {key: [] for key in lots}
    for offer in offers:
        lot_id = offer["lot_id"]
        if lot_id not in lots:
            raise ValueError(f"unknown lot {lot_id}")
        price = int(offer["price_cents"])
        if price < 0:
            raise ValueError("offer price cannot be negative")
        lot = lots[lot_id]
        reasons = []
        if _dt(offer["submitted"]) > _dt(lot["deadline"]):
            reasons.append("late")
        missing = sorted(set(lot["required_documents"]) - set(offer["documents"]))
        if missing:
            reasons.append("missing_documents:" + ",".join(missing))
        if price > int(lot["budget_cents"]):
            reasons.append("over_budget")
        row = {"offer_id": offer["id"], "vendor_id": offer["vendor_id"], "price_cents": price,
               "eligible": not reasons, "reasons": reasons}
        grouped[lot_id].append(row)
    result = []
    for lot_id in sorted(lots):
        rows = sorted(grouped[lot_id], key=lambda x: (x["price_cents"], x["vendor_id"], x["offer_id"]))
        winner = next((x for x in rows if x["eligible"]), None)
        result.append({"lot_id": lot_id, "offers": rows, "award_offer_id": winner["offer_id"] if winner else None,
                       "award_vendor_id": winner["vendor_id"] if winner else None})
    return {"lots": result, "awarded": sum(x["award_offer_id"] is not None for x in result)}
