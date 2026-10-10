from pathlib import Path
import requests

BASE = Path.home() / "iov-lab"
PORTS = {"TA": 17401, "RSU1": 17403, "RSU2": 17405}

TA_ADDRESS = "1KvMog5Q5FuEyYvRuC6yAz6iP1DN2YDLJdgPdq"

RSU_ADDRESSES = {
    "RSU1": "1B9hsdERZpkBwkGGYf4DCCG6aFQRz8ifbKUWmk",
    "RSU2": "1Y14TDFJdDzjdaL5hTRVmdWvBMkzHdHXXSXXV4",
}

VEHICLES = {
    "A": "1M4iAgVgvieWFdpRuPYFpatcbGBAVY4Q9qY1cu",
    "B": "1MrgswowcXXrCj6Mp8NYvm3ovW1apaxUMYs3ce",
    "C": "1LSsi3RYS7HcXoyPhXdzWE4y5B3AZM5pbuUZ2Z",
    "D": "1N9bGBoB6vdL2LteeVAhvVxCwWWt7cA78XQjxY",
    "E": "18UGApYRVxvobLbkPvMFAc6krrdLriwDBmpBZU",
}

VEHICLE_NODES = {
    "A": "RSU1", "B": "RSU1", "C": "RSU1",
    "D": "RSU2", "E": "RSU2",
}

STREAMS = [
    "registry", "reports", "decisions",
    "requests", "claims", "telemetry",
]

SESSIONS = {}


def get_session(node):
    if node not in SESSIONS:
        config_path = BASE / node / "iovlab" / "multichain.conf"
        config = {}

        for line in config_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()

        if "rpcuser" not in config or "rpcpassword" not in config:
            raise RuntimeError(f"{node}: RPC credentials missing in config")

        session = requests.Session()
        session.trust_env = False
        session.auth = (config["rpcuser"], config["rpcpassword"])
        SESSIONS[node] = session

    return SESSIONS[node]


def rpc(node, method, *params):
    response = get_session(node).post(
        f"http://127.0.0.1:{PORTS[node]}",
        json={
            "jsonrpc": "1.0",
            "id": "iov-lab",
            "method": method,
            "params": list(params),
        },
        timeout=30,
    )

    if response.status_code == 401:
        raise RuntimeError(f"{node}: RPC authentication failed")

    result = response.json()

    if result.get("error"):
        error = result["error"]
        raise RuntimeError(
            f"{node}/{method}: {error['code']} - {error['message']}"
        )

    response.raise_for_status()
    return result["result"]


def publish(node, address, stream, key, record):
    return rpc(
        node, "publishfrom",
        address, stream, key, {"json": record}
    )


def read_items(node, stream):
    return rpc(node, "liststreamitems", stream, False, 10000)


def check_setup():
    print("CHECKING THREE NODES")
    for node in PORTS:
        info = rpc(node, "getinfo")
        assert info["chainname"] == "iovlab", f"{node}: wrong chain"

        # A Python list becomes the JSON array subscribe requires.
        rpc(node, "subscribe", STREAMS)
        rpc(node, "subscribe", "IOVREWARD")

        records = read_items(node, "registry")
        registered = {
            item["keys"][0]: item["data"]["json"]
            for item in records
        }

        for vehicle, address in VEHICLES.items():
            assert registered[vehicle]["address"] == address, (
                f"{node}: registration mismatch for {vehicle}"
            )

        print(
            f"{node}: connected | blocks={info['blocks']} "
            f"| vehicles A-E verified"
        )

    print("\nTA REWARD BALANCE")
    print(rpc("TA", "getaddressbalances", TA_ADDRESS))
    print("\nSETUP CHECK PASSED")


if __name__ == "__main__":
    check_setup()