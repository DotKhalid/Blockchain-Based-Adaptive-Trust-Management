import time

from connection import (
    rpc, publish, VEHICLES, VEHICLE_NODES,
    TA_ADDRESS, RSU_ADDRESSES,
)

SESSION = "manual-001"
REPORTERS = ["B", "C", "D", "E"]


def get_record(node, stream, key):
    items = rpc(node, "liststreamkeyitems", stream, key, False, 1000)
    if len(items) > 1:
        raise RuntimeError(f"Duplicate records: {stream}/{key}")
    return items[0] if items else None


def wait_confirmed(stream, key):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        records = [
            get_record(node, stream, key)
            for node in ("TA", "RSU1", "RSU2")
        ]
        if all(r and r.get("confirmations", 0) >= 1 for r in records):
            if len({r["txid"] for r in records}) != 1:
                raise RuntimeError("Nodes disagree on transaction")
            return records[0]
        time.sleep(0.5)
    raise RuntimeError(f"Confirmation timeout: {stream}/{key}")


def check_record(item, address, data):
    if item["publishers"] != [address]:
        raise RuntimeError("Unexpected publisher")
    if item["data"].get("json") != data:
        raise RuntimeError("Existing record does not match")


def write_once(node, address, stream, key, data):
    existing = get_record("TA", stream, key)
    if existing:
        check_record(existing, address, data)
        print(f"Already recorded: {stream}/{key}")
    else:
        publish(node, address, stream, key, data)
        print(f"Published: {stream}/{key}")

    item = wait_confirmed(stream, key)
    check_record(item, address, data)
    return item


def pay_once(vehicle, amount):
    key = f"{SESSION}:{vehicle}"
    receipt = {
        "session": SESSION,
        "mode": "manual_test",
        "vehicle": vehicle,
        "asset": "IOVREWARD",
        "amount": amount,
        "trust_delta": 1,
        "credits_delta": amount,
    }

    existing = get_record("TA", "claims", key)
    if existing:
        check_record(existing, TA_ADDRESS, receipt)
        print(f"Reward already paid: {vehicle}")
    else:
        # Token payment and receipt are in ONE transaction.
        txid = rpc(
            "TA", "createrawsendfrom",
            TA_ADDRESS,
            {VEHICLES[vehicle]: {"IOVREWARD": amount}},
            [{
                "for": "claims",
                "key": key,
                "data": {"json": receipt},
            }],
            "send",
        )
        print(f"Paid {vehicle}: {amount} tokens | {txid}")

    item = wait_confirmed("claims", key)
    check_record(item, TA_ADDRESS, receipt)


def main():
    print("\n1. PUBLISHING FOUR SIGNED TEST REPORTS")

    for vehicle in REPORTERS:
        write_once(
            VEHICLE_NODES[vehicle],
            VEHICLES[vehicle],
            "reports",
            f"{SESSION}:{vehicle}",
            {
                "session": SESSION,
                "mode": "manual_test",
                "reporter": vehicle,
                "suspect": "A",
                "vote": True,
                "reason": "Manually supplied test allegation",
            },
        )

    print("\n2. RSU1 VALIDATES REPORTERS AND COUNTS VOTES")

    reports = []
    for vehicle in REPORTERS:
        item = get_record("RSU1", "reports", f"{SESSION}:{vehicle}")
        data = item["data"]["json"]

        if item["publishers"] != [VEHICLES[vehicle]]:
            raise RuntimeError("Reporter signature mismatch")
        if (
            data["reporter"] != vehicle
            or data["suspect"] != "A"
            or data["session"] != SESSION
            or type(data["vote"]) is not bool
        ):
            raise RuntimeError("Invalid report")

        reports.append(data)

    unique_reporters = {r["reporter"] for r in reports}
    yes_votes = sum(r["vote"] for r in reports)

    accepted = (
        len(unique_reporters) >= 4
        and yes_votes > len(reports) / 2
    )
    if not accepted:
        raise RuntimeError("Quorum or strict majority not reached")

    decision = {
        "session": SESSION,
        "mode": "manual_test",
        "suspect": "A",
        "reporter_count": len(unique_reporters),
        "yes_votes": yes_votes,
        "accepted": accepted,
        "trust_delta": -1,
        "soft_block_seconds": 60,
        "clock": "manual_simulation_seconds",
        "decision_time": 10,
        "blocked_until": 70,
        "report_txids": [
            get_record("RSU1", "reports", f"{SESSION}:{v}")["txid"]
            for v in REPORTERS
        ],
    }

    write_once(
        "RSU1", RSU_ADDRESSES["RSU1"],
        "decisions", SESSION, decision,
    )
    print(f"Accepted: {yes_votes}/{len(reports)} votes")

    print("\n3. VEHICLES SUBMIT REWARD REQUESTS")

    for vehicle in REPORTERS:
        write_once(
            VEHICLE_NODES[vehicle],
            VEHICLES[vehicle],
            "requests",
            f"{SESSION}:{vehicle}",
            {
                "session": SESSION,
                "mode": "manual_test",
                "vehicle": vehicle,
                "decision_key": SESSION,
            },
        )

        # B published first: base reward 5 + first-reporter bonus 2.
        amount = 7 if vehicle == REPORTERS[0] else 5
        pay_once(vehicle, amount)

    print("\n4. MANUAL SESSION RESULTS")
    print("Vehicle | Trust | Credits | Actual token balance")

    for vehicle in VEHICLES:
        registration = get_record("TA", "registry", vehicle)
        initial = registration["data"]["json"]

        if vehicle == "A":
            trust = initial["initial_trust"] + decision["trust_delta"]
            credits = initial["initial_credits"]
        else:
            receipt = get_record(
                "TA", "claims", f"{SESSION}:{vehicle}"
            )["data"]["json"]
            trust = initial["initial_trust"] + receipt["trust_delta"]
            credits = initial["initial_credits"] + receipt["credits_delta"]

        balances = rpc(
            VEHICLE_NODES[vehicle],
            "getaddressbalances", VEHICLES[vehicle],
        )
        tokens = sum(
            b["qty"] for b in balances
            if b.get("name") == "IOVREWARD"
        )
        print(f"{vehicle:7} | {trust:5} | {credits:7} | {tokens}")

    print("\nMANUAL WORKFLOW COMPLETE")
    print("All session records confirmed on TA, RSU1 and RSU2.")


if __name__ == "__main__":
    main()