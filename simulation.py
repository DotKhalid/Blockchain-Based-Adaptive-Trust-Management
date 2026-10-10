import csv
import json
import math
import shutil
import subprocess
import time
import uuid

import traci

from connection import (
    BASE, rpc, VEHICLES, VEHICLE_NODES,
    TA_ADDRESS, RSU_ADDRESSES, read_items,
)
from workflow import write_once, wait_confirmed

RUN = "sumo-" + uuid.uuid4().hex[:10]
FOLDER = BASE / "simulation"
FOLDER.mkdir(exist_ok=True)

TRUST = {}
CREDITS = {}
BLOCKED_UNTIL = {v: 0 for v in VEHICLES}
EVENTS = []
ROWS = []


def load_state():
    # Continue from the records already on this blockchain.
    for vehicle in VEHICLES:
        items = rpc(
            "TA", "liststreamkeyitems",
            "registry", vehicle, False, 1000,
        )
        if len(items) != 1:
            raise RuntimeError("Expected one registration per vehicle")
        item = items[0]
        if item["publishers"] != [TA_ADDRESS]:
            raise RuntimeError("Unexpected registration publisher")
        record = item["data"]["json"]
        TRUST[vehicle] = record["initial_trust"]
        CREDITS[vehicle] = record["initial_credits"]

    seen = set()
    for item in read_items("TA", "decisions"):
        if not item.get("confirmations", 0):
            raise RuntimeError("Wait for pending decisions to confirm")
        if len(item["publishers"]) != 1:
            continue
        if item["publishers"][0] not in RSU_ADDRESSES.values():
            continue

        data = item["data"]["json"]
        session = data["session"]
        if session in seen:
            raise RuntimeError("Duplicate decision session")
        seen.add(session)

        if data.get("accepted"):
            TRUST[data["suspect"]] += data["trust_delta"]

    seen = set()
    for item in read_items("TA", "claims"):
        if not item.get("confirmations", 0):
            raise RuntimeError("Wait for pending rewards to confirm")
        if item["publishers"] != [TA_ADDRESS]:
            continue

        data = item["data"]["json"]
        key = (data["session"], data["vehicle"])
        if key in seen:
            raise RuntimeError("Duplicate reward receipt")
        seen.add(key)

        TRUST[data["vehicle"]] += data["trust_delta"]
        CREDITS[data["vehicle"]] += data["credits_delta"]


def eligible(vehicle, sim_time):
    return (
        TRUST[vehicle] >= 0
        and sim_time >= BLOCKED_UNTIL[vehicle]
    )


def make_scenario():
    netconvert = shutil.which("netconvert")
    gui = shutil.which("sumo-gui")
    if not netconvert or not gui:
        raise RuntimeError("netconvert or sumo-gui missing from PATH")

    (FOLDER / "road.nod.xml").write_text(
        '<nodes>'
        '<node id="start" x="0" y="0"/>'
        '<node id="finish" x="3000" y="0"/>'
        '</nodes>', encoding="utf-8",
    )
    (FOLDER / "road.edg.xml").write_text(
        '<edges>'
        '<edge id="road" from="start" to="finish" '
        'numLanes="2" speed="20"/>'
        '</edges>', encoding="utf-8",
    )

    subprocess.run([
        netconvert,
        "--node-files", str(FOLDER / "road.nod.xml"),
        "--edge-files", str(FOLDER / "road.edg.xml"),
        "--output-file", str(FOLDER / "road.net.xml"),
    ], check=True)

    vehicles_xml = []
    for index, vehicle in enumerate(VEHICLES):
        position = 200 - 25 * index
        vehicles_xml.append(
            f'<vehicle id="{vehicle}" type="car" route="route0" '
            f'depart="0" departPos="{position}" '
            f'departSpeed="15" departLane="0"/>'
        )

    (FOLDER / "cars.rou.xml").write_text(
        '<routes>'
        '<vType id="car" accel="2.6" decel="4.5" '
        'sigma="0" length="5" minGap="2.5" '
        'maxSpeed="15" speedFactor="1" speedDev="0"/>'
        '<route id="route0" edges="road"/>'
        + "".join(vehicles_xml) +
        '</routes>', encoding="utf-8",
    )

    (FOLDER / "demo.sumocfg").write_text(
        '<configuration>'
        '<input>'
        '<net-file value="road.net.xml"/>'
        '<route-files value="cars.rou.xml"/>'
        '</input>'
        '<time><begin value="0"/><end value="100"/>'
        '<step-length value="1"/></time>'
        '</configuration>', encoding="utf-8",
    )
    return gui


def reward(vehicle, amount, session):
    request = {
        "session": session,
        "run": RUN,
        "vehicle": vehicle,
        "decision_key": session,
    }
    key = f"{session}:{vehicle}"

    write_once(
        VEHICLE_NODES[vehicle], VEHICLES[vehicle],
        "requests", key, request,
    )

    receipt = {
        "session": session,
        "run": RUN,
        "vehicle": vehicle,
        "asset": "IOVREWARD",
        "amount": amount,
        "trust_delta": 1,
        "credits_delta": amount,
    }

    # Asset transfer and receipt are committed together.
    rpc(
        "TA", "createrawsendfrom", TA_ADDRESS,
        {VEHICLES[vehicle]: {"IOVREWARD": amount}},
        [{
            "for": "claims",
            "key": key,
            "data": {"json": receipt},
        }],
        "send",
    )
    wait_confirmed("claims", key)
    TRUST[vehicle] += 1
    CREDITS[vehicle] += amount


def handle_detection(sim_time, positions, evidence):
    suspect = "A"
    rsu = "RSU1" if positions[suspect][0] < 1000 else "RSU2"

    reporters = [
        vehicle for vehicle in VEHICLES
        if vehicle != suspect
        and eligible(vehicle, sim_time)
        and math.dist(positions[vehicle], positions[suspect]) <= 300
    ]
    if len(reporters) < 4:
        return

    session = f"{RUN}:{int(sim_time)}"
    evidence = dict(
        evidence, run=RUN, session=session, suspect=suspect,
        sensor_model="ideal SUMO position observation",
    )

    evidence_item = write_once(
        rsu, RSU_ADDRESSES[rsu],
        "telemetry", session, evidence,
    )

    for vehicle in reporters:
        write_once(
            VEHICLE_NODES[vehicle], VEHICLES[vehicle],
            "reports", f"{session}:{vehicle}",
            {
                "session": session,
                "run": RUN,
                "reporter": vehicle,
                "suspect": suspect,
                "vote": True,
                "evidence_txid": evidence_item["txid"],
                "sim_time": sim_time,
            },
        )

    # Read confirmed reports back from the deciding RSU.
    validated = {}
    for vehicle in reporters:
        items = rpc(
            rsu, "liststreamkeyitems",
            "reports", f"{session}:{vehicle}", False, 1000,
        )
        if len(items) != 1:
            raise RuntimeError("Missing or duplicate report")
        item = items[0]
        report = item["data"]["json"]

        if (
            item["publishers"] != [VEHICLES[vehicle]]
            or report["reporter"] != vehicle
            or report["session"] != session
            or report["suspect"] != suspect
            or report["evidence_txid"] != evidence_item["txid"]
            or type(report["vote"]) is not bool
        ):
            raise RuntimeError("Invalid report")
        validated[vehicle] = item

    yes = sum(i["data"]["json"]["vote"] for i in validated.values())
    if len(validated) < 4 or yes <= len(validated) / 2:
        return

    before = TRUST[suspect]
    after = before - 1
    status = "hard_revoked" if after < 0 else "soft_blocked"

    decision = {
        "session": session,
        "run": RUN,
        "suspect": suspect,
        "accepted": True,
        "reporter_count": len(validated),
        "yes_votes": yes,
        "trust_before": before,
        "trust_after": after,
        "trust_delta": -1,
        "status": status,
        "clock": "simulation_seconds",
        "decision_time": sim_time,
        "blocked_until": sim_time + 60,
        "report_txids": [i["txid"] for i in validated.values()],
    }

    write_once(
        rsu, RSU_ADDRESSES[rsu],
        "decisions", session, decision,
    )
    TRUST[suspect] = after
    BLOCKED_UNTIL[suspect] = sim_time + 60

    # Reports were submitted sequentially and confirmed in this order.
    for index, vehicle in enumerate(reporters):
        reward(vehicle, 7 if index == 0 else 5, session)

    EVENTS.append(dict(decision, rsu=rsu))
    print(
        f"\nDETECTED t={sim_time:.0f} | {rsu} | "
        f"A trust {before}->{after} | {status}\n"
    )


def main():
    load_state()
    gui = make_scenario()
    print("RUN:", RUN)
    print("Starting trust:", TRUST)

    previous = None
    streak = 0
    block_demo_printed = False

    traci.start([
        gui, "-c", str(FOLDER / "demo.sumocfg"),
        "--start", "--quit-on-end",
        "--seed", "42", "--no-step-log", "true",
    ])

    try:
        for _ in range(100):
            traci.simulationStep()
            sim_time = traci.simulation.getTime()
            ids = set(traci.vehicle.getIDList())
            if not set(VEHICLES).issubset(ids):
                continue

            positions = {
                v: traci.vehicle.getPosition(v) for v in VEHICLES
            }
            actual = traci.vehicle.getSpeed("A")

            # Attack changes the message, not A's actual driving speed.
            attack = (15 <= sim_time <= 22) or (80 <= sim_time <= 87)
            claimed = actual + 30 if attack else actual

            for vehicle in VEHICLES:
                color = (0, 200, 0, 255)
                if vehicle == "A":
                    color = (
                        (255, 0, 0, 255) if attack
                        else (0, 120, 255, 255)
                    )
                traci.vehicle.setColor(vehicle, color)

            if previous is None:
                traci.gui.trackVehicle("View #0", "A")
                traci.gui.setZoom("View #0", 800)
            else:
                old_time, old_position = previous
                estimated = (
                    math.dist(positions["A"], old_position)
                    / (sim_time - old_time)
                )
                mismatch = abs(claimed - estimated) > 8
                streak = streak + 1 if mismatch else 0
                detected = streak >= 2

                ROWS.append({
                    "time": sim_time,
                    "x": positions["A"][0],
                    "actual_speed": actual,
                    "claimed_speed": claimed,
                    "estimated_speed": estimated,
                    "attack": int(attack),
                    "detected": int(detected),
                    "A_eligible": int(eligible("A", sim_time)),
                })

                if detected and eligible("A", sim_time):
                    handle_detection(
                        sim_time, positions,
                        {
                            "previous_time": old_time,
                            "time": sim_time,
                            "previous_position": list(old_position),
                            "position": list(positions["A"]),
                            "claimed_speed": claimed,
                            "estimated_speed": estimated,
                            "threshold_mps": 8,
                            "consecutive_mismatches": streak,
                        },
                    )

                # Exercise the Python participation gate.
                if sim_time >= 20 and not block_demo_printed:
                    if not eligible("A", sim_time):
                        print(
                            "BLOCK TEST: A's attempted report rejected "
                            "by Python; no transaction submitted."
                        )
                        block_demo_printed = True

            previous = (sim_time, positions["A"])
            time.sleep(0.12)

        print("\nSimulation finished. Take a GUI screenshot now.")
        input("Press Enter here to close SUMO and save results...")
    finally:
        traci.close()

    output = FOLDER / f"{RUN}.csv"
    with output.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(ROWS[0]))
        writer.writeheader()
        writer.writerows(ROWS)

    metrics = {
        "TP": sum(r["attack"] and r["detected"] for r in ROWS),
        "FP": sum(not r["attack"] and r["detected"] for r in ROWS),
        "TN": sum(not r["attack"] and not r["detected"] for r in ROWS),
        "FN": sum(r["attack"] and not r["detected"] for r in ROWS),
    }

    balances = {}
    print("\nVehicle | Trust | Credits | Actual reward tokens")
    for vehicle in VEHICLES:
        assets = rpc(
            VEHICLE_NODES[vehicle],
            "getaddressbalances", VEHICLES[vehicle],
        )
        balances[vehicle] = sum(
            a["qty"] for a in assets if a.get("name") == "IOVREWARD"
        )
        print(
            f"{vehicle:7} | {TRUST[vehicle]:5} | "
            f"{CREDITS[vehicle]:7} | {balances[vehicle]}"
        )

    summary = {
        "run": RUN,
        "events": EVENTS,
        "trust": TRUST,
        "credits": CREDITS,
        "token_balances": balances,
        "detector_metrics": metrics,
    }
    summary_path = FOLDER / f"{RUN}.json"
    summary_path.write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print("\nDetector metrics:", metrics)
    print("Decision sessions:", len(EVENTS))
    print("Saved:", output)
    print("Saved:", summary_path)


if __name__ == "__main__":
    main()