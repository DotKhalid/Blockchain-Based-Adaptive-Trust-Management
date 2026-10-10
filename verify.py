from connection import rpc, read_items, STREAMS, TA_ADDRESS

expected = {
    "registry": 5,
    "reports": 12,
    "decisions": 3,
    "requests": 12,
    "claims": 12,
    "telemetry": 2,
}

print("STREAM       TA  RSU1  RSU2  MATCH  CONFIRMED")

for stream in STREAMS:
    records = [
        read_items(node, stream)
        for node in ("TA", "RSU1", "RSU2")
    ]
    counts = [len(items) for items in records]
    transactions = [
        {item["txid"] for item in items}
        for items in records
    ]
    matched = transactions[0] == transactions[1] == transactions[2]
    confirmed = all(
        item.get("confirmations", 0) >= 1
        for items in records for item in items
    )

    print(
        f"{stream:12} {counts[0]:2}  {counts[1]:4}  "
        f"{counts[2]:4}  {str(matched):5}  {confirmed}"
    )
    assert counts == [expected[stream]] * 3, "Unexpected record count"
    assert matched and confirmed, "Records not confirmed or synchronized"

balance = sum(
    item["qty"]
    for item in rpc("TA", "getaddressbalances", TA_ADDRESS)
    if item.get("name") == "IOVREWARD"
)
print("\nTA remaining tokens:", balance)
assert balance == 9934, "Unexpected treasury balance"

print("\nPASS: All three nodes agree; all records confirmed.")