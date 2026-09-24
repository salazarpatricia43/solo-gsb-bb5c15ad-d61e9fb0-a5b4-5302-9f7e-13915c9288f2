"""Smoke test: exercises the solver + API end to end via TestClient.

The A/B concentrate numbers are constructed so that the exact target mix
for 10 L is A = 1500 mL + B = 125 mL + 8375 mL water (16.25% concentrate):

    N: 1500 mL * 1200 mg/L / 1000 = 1800 mg -> 180 mg/L
    P:  125 mL * 4000 mg/L / 1000 =  500 mg ->  50 mg/L
    K: 1500 * 400/1000 + 125 * 2000/1000
                                 =  850 mg ->  85 mg/L
"""
import json
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def base_request() -> dict:
    return {
        "target_volume_ml": 10000,
        "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
        "concentrates": [
            {"name": "A液", "available_volume": 2000,
             "concentrations": {"N": 1200, "K": 400}},
            {"name": "B液", "available_volume": 2000,
             "concentrations": {"P": 4000, "K": 2000}},
        ],
        "max_concentrate_fraction": 0.20,
        "min_water_ml": 7000,
        "tolerance": 0.05,
    }


with TestClient(app) as client:
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    # 1) feasible trial --------------------------------------------------- #
    req = base_request()
    r = client.post("/api/solve", json=req)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    assert data["water_volume_ml"] >= 7000
    assert data["concentrate_total_ml"] <= 2000 + 1e-6
    assert abs(
        sum(a["volume_ml"] for a in data["allocations"])
        + data["water_volume_ml"] - 10000
    ) < 1e-4
    for el in data["elements"]:
        assert el["status"] == "ok", el
    alloc = {a["concentrate"]: a["volume_ml"] for a in data["allocations"]}
    assert abs(alloc["A液"] - 1500) < 1e-4, alloc
    assert abs(alloc["B液"] - 125) < 1e-4, alloc
    assert abs(data["water_volume_ml"] - 8375) < 1e-4
    print("1) feasible allocations:", alloc,
          "water:", data["water_volume_ml"])

    # 2) infeasible: A bottle nearly empty -> N shortage ------------------ #
    bad = base_request()
    bad["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/solve", json=bad)
    data = r.json()
    assert data["feasible"] is False, data
    nv = [v for v in data["violations"] if v["element"] == "N"]
    assert nv and nv[0]["code"] == "ELEMENT_SHORTAGE", data["violations"]
    # 50 mL * 1.2 mg/mL = 60 mg max vs target 1800 mg
    assert abs(nv[0]["actual"] - 60.0) < 1e-6, nv
    print("2) shortage:", nv[0]["message"])

    # 3) hard volume conflict: dilution-water floor above total volume --- #
    bad2 = base_request()
    bad2["min_water_ml"] = 10500
    bad2["max_concentrate_fraction"] = 0.05
    data = client.post("/api/solve", json=bad2).json()
    assert data["feasible"] is False
    codes = [v["code"] for v in data["violations"]]
    assert codes[0] == "WATER_MIN_EXCEEDS_VOLUME"
    assert "CONCENTRATE_FRACTION_CONFLICT" in codes, codes
    print("3) conflict:", data["violations"][0]["message"])

    # 3b) a high water floor that stays within the total volume is NOT a
    #     fraction conflict: min 9600 mL water + 5% cap leaves 400 mL of
    #     headroom for concentrate (cap allows 500 mL), so the hard
    #     polytope is feasible — chemistry simply cannot be met there ----- #
    okfloor = base_request()
    okfloor["min_water_ml"] = 9600
    okfloor["max_concentrate_fraction"] = 0.05
    data = client.post("/api/solve", json=okfloor).json()
    assert data["feasible"] is False
    assert all(v["code"] != "CONCENTRATE_FRACTION_CONFLICT"
               for v in data["violations"]), data["violations"]
    assert all(v["code"] != "WATER_MIN_EXCEEDS_VOLUME"
               for v in data["violations"]), data["violations"]
    print("3b) water floor within volume is not a hard conflict")

    # 4) water min exceeds total ------------------------------------------ #
    bad3 = base_request()
    bad3["min_water_ml"] = 12000
    data = client.post("/api/solve", json=bad3).json()
    assert data["violations"][0]["code"] == "WATER_MIN_EXCEEDS_VOLUME"
    print("4) water > total:", data["violations"][0]["message"])

    # 5) per-bottle max_volume binds -> N shortage ------------------------ #
    bad4 = base_request()
    bad4["concentrates"][0]["max_volume"] = 5
    data = client.post("/api/solve", json=bad4).json()
    assert data["feasible"] is False
    assert any(v["code"] == "ELEMENT_SHORTAGE" and v["element"] == "N"
               for v in data["violations"]), data["violations"]
    print("5) max_volume cap binds")

    # 6) misspelled inner key is rejected, not silently ignored ----------- #
    bad5 = base_request()
    bad5["concentrates"][1]["concentrates"] = bad5["concentrates"][1].pop(
        "concentrations")
    r = client.post("/api/solve", json=bad5)
    assert r.status_code == 422, r.text
    print("6) typo rejected with 422")

    # 7) unknown element rejected ----------------------------------------- #
    bad6 = base_request()
    bad6["targets"]["concentrations"]["Xx"] = 1
    assert client.post("/api/solve", json=bad6).status_code == 422

    # 7b) element keys are case-insensitive (ca/CA/fe all accepted) ------- #
    ci = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"ca": 50, "FE": 2.5}},
        "concentrates": [
            {"name": "C", "available_volume": 100,
             "concentrations": {"CA": 25000}},
            {"name": "E", "available_volume": 100,
             "concentrations": {"fe": 1250}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    r = client.post("/api/solve", json=ci)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    got = {e["element"]: e["status"] for e in data["elements"]}
    assert got == {"Ca": "ok", "Fe": "ok"}, got

    # 8) duplicate concentrate names rejected ----------------------------- #
    bad7 = base_request()
    bad7["concentrates"][1]["name"] = "A液"
    assert client.post("/api/solve", json=bad7).status_code == 422

    # 9) save feasible recipe --------------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "description": "test", "note": "v1",
        "request": base_request()})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1 and v1["result"]["feasible"]

    # 10) second save -> version 2 ---------------------------------------- #
    r = client.post("/api/recipes", json={
        "name": "叶菜通用配方", "note": "v2", "request": base_request()})
    assert r.status_code == 201 and r.json()["version"] == 2

    # 11) infeasible save rejected, then forced --------------------------- #
    r = client.post("/api/recipes", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 422
    r = client.post("/api/recipes?force=true", json={
        "name": "bad", "request": bad4})
    assert r.status_code == 201 and r.json()["result"]["feasible"] is False

    # 12) list / get / versions / export ---------------------------------- #
    names = [x["name"] for x in client.get("/api/recipes").json()]
    assert "叶菜通用配方" in names and len(names) == 2
    rid = v1["recipe_id"]
    versions = client.get(f"/api/recipes/{rid}/versions").json()
    assert versions[0]["version"] == 2
    assert client.get(f"/api/recipes/{rid}/versions/1").json()["version"] == 1
    r = client.get(f"/api/recipes/{rid}/versions/1/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    payload = r.json()
    assert payload["recipe"]["name"] == "叶菜通用配方"
    assert payload["version"]["result"]["feasible"] is True

    assert client.get("/api/recipes/999").status_code == 404
    assert client.get(f"/api/recipes/{rid}/versions/99").status_code == 404

    # 13) element surplus: N and P coupled 1:1, P target far too low ------ #
    surp_req = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 200, "P": 1}},
        "concentrates": [
            {"name": "BNP", "available_volume": 100,
             "concentrations": {"N": 100000, "P": 100000}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    data = client.post("/api/solve", json=surp_req).json()
    assert data["feasible"] is False, data
    assert any(v["code"] == "ELEMENT_SURPLUS" and v["element"] == "P"
               for v in data["violations"]), data["violations"]
    print("13) surplus:",
          [v["message"] for v in data["violations"]
           if v["code"] == "ELEMENT_SURPLUS"][0])

    # 14) micronutrients: coupled Fe/N ratio exactly matches target ------- #
    # 1 L, N=100 mg/L (100 mg), Fe=2 mg/L (2 mg). Use 2 mL of a mother
    # liquor with N=50000 mg/L and Fe=1000 mg/L -> N 100 mg, Fe 2 mg.
    micro = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/solve", json=micro)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    fe = [e for e in data["elements"] if e["element"] == "Fe"][0]
    assert fe["status"] == "ok"
    assert abs(data["allocations"][0]["volume_ml"] - 2.0) < 1e-4
    print("14) micro mix ok:", [(e["element"], e["status"])
                                for e in data["elements"]])

    # 15) element present in a concentrate but not targeted is reported as
    #     "unconstrained" (actual concentration still shown) -------------- #
    unc = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "Fe": 2}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             # B comes along for the ride but has no target
             "concentrations": {"N": 50000, "Fe": 1000, "B": 500}},
        ],
        "max_concentrate_fraction": 0.5, "min_water_ml": 0, "tolerance": 0.05}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    # B is untargeted: no row for it, because targets drive element rows;
    # it only matters if the user lists it. Confirm targeted rows are ok
    # and B does not block feasibility.
    assert {e["element"] for e in data["elements"]} == {"N", "Fe"}
    # zero target element -> unconstrained row
    unc["targets"] = {"concentrations": {"N": 100, "Fe": 2, "B": 0}}
    data = client.post("/api/solve", json=unc).json()
    assert data["feasible"] is True, data
    b_row = [e for e in data["elements"] if e["element"] == "B"][0]
    assert b_row["status"] == "unconstrained"
    assert b_row["relative_deviation"] is None
    print("15) untargeted/zero-target handling ok")

    # =================================================================== #
    # Raw-water mineral baseline (source_water)
    # =================================================================== #
    # 10 L; target Ca=100, Mg=40 mg/L; raw water already carries
    # Ca=40, Mg=30 mg/L; concentrate C has Ca=640, Mg=130 mg/L.
    # Exact solution: C 1000 mL + water 9000 mL:
    #   Ca: 1000*640/1000 + 9000*40/1000 = 640 + 360 = 1000 mg
    #   Mg: 1000*130/1000 + 9000*30/1000 = 130 + 270 =  400 mg
    def raw_request() -> dict:
        return {
            "target_volume_ml": 10000,
            "targets": {"concentrations": {"Ca": 100, "Mg": 40}},
            "concentrates": [
                {"name": "C液", "available_volume": 2000,
                 "concentrations": {"Ca": 640, "Mg": 130}}
            ],
            "source_water": {"concentrations": {"ca": 40, "mg": 30}},
            "max_concentrate_fraction": 0.20,
            "min_water_ml": 0,
            "tolerance": 0.05,
        }

    # 28) raw-water baseline: contributions split per element ------------- #
    r = client.post("/api/solve", json=raw_request())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True, data
    alloc = {a["concentrate"]: a["volume_ml"] for a in data["allocations"]}
    assert abs(alloc["C液"] - 1000) < 1e-4, alloc
    assert abs(data["water_volume_ml"] - 9000) < 1e-4
    rows = {e["element"]: e for e in data["elements"]}
    assert abs(rows["Ca"]["concentrate_amount_mg"] - 640) < 1e-3
    assert abs(rows["Ca"]["source_water_amount_mg"] - 360) < 1e-3
    assert abs(rows["Ca"]["actual_amount_mg"] - 1000) < 1e-3
    assert abs(rows["Mg"]["concentrate_amount_mg"] - 130) < 1e-3
    assert abs(rows["Mg"]["source_water_amount_mg"] - 270) < 1e-3
    assert abs(rows["Mg"]["actual_amount_mg"] - 400) < 1e-3
    assert all(e["status"] == "ok" for e in data["elements"])
    print("28) raw-water baseline split:",
          {e: (rows[e]["concentrate_amount_mg"],
               rows[e]["source_water_amount_mg"]) for e in rows})

    # 29) the baseline scales with the SOLVED water volume, not a fixed
    #     s*V pre-deduction: exact case yields 360 mg (40 mg/L * 9000 mL),
    #     not 400 mg (40 mg/L * 10000 mL) ------------------------------- #
    data = client.post("/api/solve", json=raw_request()).json()
    w0 = data["water_volume_ml"]
    ca0 = [e for e in data["elements"] if e["element"] == "Ca"][0]
    assert abs(ca0["source_water_amount_mg"] - 0.04 * w0) < 1e-6
    assert abs(ca0["source_water_amount_mg"] - 360) < 1e-3
    # binding bottle cap forces a different compromise water share; the
    # raw-water contribution must follow that solved water (x=800 -> w=9200)
    capped = raw_request()
    capped["concentrates"][0]["available_volume"] = 800
    data = client.post("/api/solve", json=capped).json()
    assert data["feasible"] is False, data  # Ca shortfall beyond tolerance
    w = data["water_volume_ml"]
    assert abs(data["concentrate_total_ml"] - 800) < 1e-4
    assert abs(w - 9200) < 1e-4, w
    for el in data["elements"]:
        assert abs(
            el["source_water_amount_mg"]
            - {  # source mg = source mg/L * solved water L
                "Ca": 0.04, "Mg": 0.03}[el["element"]] * w
        ) < 1e-6
        assert abs(
            el["actual_amount_mg"]
            - el["concentrate_amount_mg"] - el["source_water_amount_mg"]
        ) < 1e-6
    print("29) baseline tracks solved water (360 mg @9000 mL,",
          "368 mg @9200 mL), never a fixed s*V deduction")

    # 30) raw-water-forced surplus: target tiny but baseline unavoidable --- #
    surplus = raw_request()
    surplus["targets"] = {"concentrations": {"Ca": 10}}
    data = client.post("/api/solve", json=surplus).json()
    assert data["feasible"] is False, data
    v = [x for x in data["violations"]
         if x["code"] == "ELEMENT_SURPLUS" and x["element"] == "Ca"]
    assert v, data["violations"]
    # even with zero concentrate, ~100% water * 40 mg/L = ~400 mg baseline
    assert abs(v[0]["actual"] - 400) < 1.0, v[0]
    print("30) raw-water-forced surplus:", v[0]["message"])

    # 31) shortage bound includes the baseline contribution --------------- #
    short = raw_request()
    short["concentrates"][0]["available_volume"] = 10
    short["concentrates"][0]["concentrations"] = {"Ca": 1200, "Mg": 130}
    data = client.post("/api/solve", json=short).json()
    assert data["feasible"] is False
    v = [x for x in data["violations"]
         if x["code"] == "ELEMENT_SHORTAGE" and x["element"] == "Ca"]
    assert v, data["violations"]
    # max = 10 mL*1.2 mg/mL + 9990 mL water*0.04 mg/mL = 12 + 399.6
    assert abs(v[0]["actual"] - 411.6) < 1e-3, v[0]
    print("31) shortage reachable bound includes baseline:", v[0]["actual"])

    # 32) source_water validation: unknown element / negative / typo -> 422 #
    bad = raw_request()
    bad["source_water"]["concentrations"]["Xx"] = 1
    assert client.post("/api/solve", json=bad).status_code == 422
    bad = raw_request()
    bad["source_water"]["concentrations"]["Ca"] = -1
    assert client.post("/api/solve", json=bad).status_code == 422
    bad = raw_request()
    bad["source_water"] = {"bogus": {"Ca": 1}}
    assert client.post("/api/solve", json=bad).status_code == 422
    print("32) source_water validation 422 ok")

    # 33) saving preserves source_water; export echoes it back ------------- #
    r = client.post("/api/recipes", json={
        "name": "原水基线配方", "note": "v1", "request": raw_request()})
    assert r.status_code == 201, r.text
    sw_rid = r.json()["recipe_id"]
    saved = client.get(
        f"/api/recipes/{sw_rid}/versions/1").json()
    assert saved["request"]["source_water"] == {
        "concentrations": {"Ca": 40.0, "Mg": 30.0}}, saved["request"]
    export = client.get(
        f"/api/recipes/{sw_rid}/versions/1/export").json()
    assert export["version"]["request"]["source_water"]["concentrations"] == {
        "Ca": 40.0, "Mg": 30.0}
    # result rows keep the split too
    erows = {e["element"]: e
             for e in export["version"]["result"]["elements"]}
    assert abs(erows["Ca"]["source_water_amount_mg"] - 360) < 1e-3
    print("33) save + export preserve source_water")

    # 34) old requests without source_water behave exactly as before ------ #
    legacy = client.post("/api/solve", json=base_request()).json()
    assert legacy["feasible"] is True
    assert all(e["source_water_amount_mg"] == 0.0
               and e["concentrate_amount_mg"] == e["actual_amount_mg"]
               for e in legacy["elements"])
    print("34) legacy requests: zero baseline, unchanged results")

    # =================================================================== #
    # Irrigation plan (POST /api/irrigation/plan)
    # =================================================================== #
    def irr_body(**overrides) -> dict:
        body = {
            "timezone": "Asia/Shanghai",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "sessions": [
                {"start_time": "08:00", "duration_minutes": 10,
                 "water_ml": 500},
                {"start_time": "08:10:30", "duration_minutes": 5,
                 "water_ml": 200},  # gap 30s -> merges with 08:00
                {"start_time": "23:55", "duration_minutes": 10,
                 "water_ml": 300},  # crosses midnight
            ],
            "daily_capacity_ml": 1100,
        }
        body.update(overrides)
        return body

    # 16) basic plan: merge close batches, cross midnight, UTC+local times #
    r = client.post("/api/irrigation/plan", json=irr_body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["timezone"] == "Asia/Shanghai" and d["days"] == 3
    # 3 sessions * 3 days = 9 raw; 08:00+08:10:30 merge per day -> 6
    assert d["batch_count_before_merge"] == 9
    assert d["batch_count_after_merge"] == 6
    assert d["merge_gap_seconds"] == 60
    assert d["capacity_ok"] and d["planned_water_ml"] == 3000
    day1 = d["day_results"][0]
    assert day1["batch_count_before_merge"] == 3
    assert day1["batch_count_after_merge"] == 2
    assert day1["planned_water_ml"] == 1000 and day1["within_capacity"]
    b0 = d["batches"][0]
    assert b0["merged"] and b0["water_ml"] == 700
    assert b0["batch_count_before_merge"] == 2
    assert b0["duration_seconds"] == 930  # 08:00:00 .. 08:15:30
    assert b0["start_utc"] == "2026-05-01T00:00:00Z"
    assert b0["local_start"] == "2026-05-01T08:00:00+0800"
    # every day's 23:55 session crosses midnight (last one ends on 05-04)
    xmid = [b for b in d["batches"] if b["crosses_midnight"]]
    assert len(xmid) == 3
    assert xmid[0]["local_end"] == "2026-05-02T00:05:00+0800"
    assert xmid[0]["end_utc"] == "2026-05-01T16:05:00Z"
    print("16) irrigation merge + cross-midnight:",
          d["batch_count_before_merge"], "->",
          d["batch_count_after_merge"], "batches")

    # 17) merge boundary: gap <= 60s merges, 61s does not ---------------- #
    near = {
        "timezone": "UTC", "start_date": "2024-01-01", "end_date": "2024-01-01",
        "daily_capacity_ml": 1000,
        "sessions": [
            {"start_time": "08:00", "duration_minutes": 10, "water_ml": 10},
            {"start_time": "08:11", "duration_minutes": 10, "water_ml": 10},
        ],
    }
    d = client.post("/api/irrigation/plan", json=near).json()
    assert d["batch_count_after_merge"] == 1 and d["batches"][0]["water_ml"] == 20
    near["sessions"][1]["start_time"] = "08:11:01"
    d = client.post("/api/irrigation/plan", json=near).json()
    assert d["batch_count_after_merge"] == 2
    print("17) merge boundary 60s/61s ok")

    # 18) merge across midnight between two days -------------------------- #
    cross = {
        "timezone": "UTC", "start_date": "2024-01-01", "end_date": "2024-01-02",
        "daily_capacity_ml": 10000,
        "sessions": [
            {"start_time": "23:59", "duration_minutes": 2, "water_ml": 100},
            {"start_time": "00:01:30", "duration_minutes": 1, "water_ml": 50},
        ],
    }
    d = client.post("/api/irrigation/plan", json=cross).json()
    assert d["batch_count_before_merge"] == 4
    assert d["batch_count_after_merge"] == 3
    merged_xmid = [
        b for b in d["batches"] if b["merged"] and b["crosses_midnight"]
    ]
    assert len(merged_xmid) == 1
    b = merged_xmid[0]
    assert b["start_utc"] == "2024-01-01T23:59:00Z"
    assert b["end_utc"] == "2024-01-02T00:02:30Z"
    assert b["water_ml"] == 150
    assert {s["day"] for s in b["sources"]} == {"2024-01-01", "2024-01-02"}
    days = {x["date"]: x for x in d["day_results"]}
    assert days["2024-01-01"]["batch_count_after_merge"] == 2
    assert days["2024-01-02"]["batch_count_after_merge"] == 2
    print("18) cross-midnight merge ok")

    # 19) DST gap (spring forward) and duplicate (fall back) ------------- #
    spring = {
        "timezone": "America/New_York",
        "start_date": "2024-03-09", "end_date": "2024-03-11",
        "sessions": [{"start_time": "02:30", "duration_minutes": 30,
                      "water_ml": 100}],
        "daily_capacity_ml": 1000,
    }
    d = client.post("/api/irrigation/plan", json=spring).json()
    events = {x["date"]: x["dst_events"] for x in d["day_results"]}
    assert events["2024-03-09"] == [] and events["2024-03-10"] == ["gap"]
    gb = [b for b in d["batches"] if b["dst_event"] == "gap"][0]
    # 02:30 EST does not exist; fold=0 maps to 07:30Z == 03:30 EDT
    assert gb["start_utc"] == "2024-03-10T07:30:00Z"
    assert gb["end_utc"] == "2024-03-10T08:00:00Z"
    assert gb["local_start"] == "2024-03-10T03:30:00-0400"
    assert gb["sources"][0]["requested_time"] == "02:30:00"
    print("19a) DST gap shifted:", gb["dst_note"])

    fall = {
        "timezone": "America/New_York",
        "start_date": "2024-11-03", "end_date": "2024-11-03",
        "sessions": [{"start_time": "01:30", "duration_minutes": 20,
                      "water_ml": 100}],
        "daily_capacity_ml": 1000,
    }
    b = client.post("/api/irrigation/plan", json=fall).json()["batches"][0]
    assert b["dst_event"] == "duplicate"
    assert b["start_utc"] == "2024-11-03T05:30:00Z"  # first occurrence, EDT
    assert b["local_start"] == "2024-11-03T01:30:00-0400"
    print("19b) DST duplicate first occurrence:", b["dst_note"])

    # elapsed duration is physical time even when a session spans a DST jump
    span = dict(spring, start_date="2024-03-10", end_date="2024-03-10")
    span["sessions"] = [{"start_time": "01:30", "duration_minutes": 120,
                         "water_ml": 1}]
    b = client.post("/api/irrigation/plan", json=span).json()["batches"][0]
    assert b["duration_seconds"] == 7200  # 2 real hours ...
    assert b["local_end"] == "2024-03-10T04:30:00-0400"  # ... clock shows 3h
    print("19c) DST-spanning physical duration ok")

    # 20) daily capacity shortfall: explicit gap, never truncated --------- #
    d = client.post(
        "/api/irrigation/plan", json=irr_body(daily_capacity_ml=900)
    ).json()
    assert d["capacity_ok"] is False
    assert len(d["capacity_violations"]) == 3
    v = d["capacity_violations"][0]
    assert v["planned_water_ml"] == 1000
    assert v["daily_capacity_ml"] == 900
    assert v["shortfall_ml"] == 100
    assert "缺口 100" in v["message"] and "未截断" in v["message"]
    assert all(x["within_capacity"] is False for x in d["day_results"])
    # batches still carry the full planned water (3 days x 1000 = 3000)
    assert sum(b["water_ml"] for b in d["batches"]) == 3000
    print("20) capacity shortfall reported without truncation")
    # zero capacity is a legal (if useless) input, not a 422
    r = client.post(
        "/api/irrigation/plan", json=irr_body(daily_capacity_ml=0)
    )
    assert r.status_code == 200 and r.json()["capacity_ok"] is False

    # 21) invalid input -> 422 ------------------------------------------- #
    def irr_422(overrides, label):
        r = client.post("/api/irrigation/plan", json=irr_body(**overrides))
        assert r.status_code == 422, (label, r.status_code, r.text[:300])

    irr_422({"timezone": "Mars/Olympus"}, "unknown timezone")
    irr_422({"timezone": "+08:00"}, "fixed-offset string")
    irr_422({"timezone": "   "}, "blank timezone")
    irr_422({"end_date": "2026-04-30"}, "end before start")
    irr_422({"sessions": [
        {"start_time": "08:00", "duration_minutes": 10, "water_ml": 1},
        {"start_time": "08:00", "duration_minutes": 1, "water_ml": 1}]},
        "duplicate start")
    irr_422({"sessions": []}, "empty sessions")
    irr_422({"sessions": [{"start_time": "25:00", "duration_minutes": 1,
                           "water_ml": 1}]}, "invalid time")
    irr_422({"sessions": [{"start_time": "08:00", "duration_minutes": 0,
                           "water_ml": 1}]}, "zero duration")
    irr_422({"sessions": [{"start_time": "08:00", "duration_minutes": 1,
                           "water_ml": -5}]}, "negative water")
    irr_422({"daily_capacity_ml": -1}, "negative capacity")
    irr_422({"start_date": "nope"}, "bad date")
    irr_422({"start_date": "2024-01-01", "end_date": "2025-01-05"},
            "range over 370 days")
    bad = irr_body()
    bad["bogus"] = 1
    r = client.post("/api/irrigation/plan", json=bad)
    assert r.status_code == 422
    print("21) all invalid irrigation inputs rejected with 422")

    # 21b) local suspended days ------------------------------------------ #
    sd = client.post(
        "/api/irrigation/plan",
        json=irr_body(suspended_dates=["2026-05-02"]),
    ).json()
    # day 2 schedules nothing: 9 - 3 raw, 6 - 2 merged batches, 2000 mL
    assert sd["suspended_dates"] == ["2026-05-02"]
    assert sd["batch_count_before_merge"] == 6
    assert sd["batch_count_after_merge"] == 4
    assert sd["planned_water_ml"] == 2000
    days_sd = {x["date"]: x for x in sd["day_results"]}
    sday = days_sd["2026-05-02"]
    assert sday["suspended"] is True
    assert sday["batch_count_before_merge"] == 0
    assert sday["planned_water_ml"] == 0 and sday["within_capacity"]
    assert days_sd["2026-05-01"]["suspended"] is False
    assert days_sd["2026-05-03"]["suspended"] is False
    # cross-midnight batch started on day 1 (23:55 -> 05-02 00:05) survives
    survive = [b for b in sd["batches"]
               if b["start_utc"] == "2026-05-01T15:55:00Z"]
    assert survive and survive[0]["crosses_midnight"]
    # no batch starts on the suspended day
    assert not any(b["local_start"].startswith("2026-05-02")
                   for b in sd["batches"])
    # unordered input is echoed sorted; capacity-0 never flags suspended day
    sd2 = client.post("/api/irrigation/plan", json=irr_body(
        daily_capacity_ml=0,
        suspended_dates=["2026-05-03", "2026-05-01"])).json()
    assert sd2["suspended_dates"] == ["2026-05-01", "2026-05-03"]
    assert {v["date"] for v in sd2["capacity_violations"]} == {"2026-05-02"}
    # day 2's 23:55 batch crosses INTO suspended 05-03 and is not cancelled
    assert any(b["start_utc"] == "2026-05-02T15:55:00Z" and b["crosses_midnight"]
               for b in sd2["batches"])
    irr_422({"suspended_dates": ["2026-05-02", "2026-05-02"]},
            "duplicate suspended date")
    irr_422({"suspended_dates": ["2026-04-30"]}, "suspended before range")
    irr_422({"suspended_dates": ["2026-05-04"]}, "suspended after range")
    print("21b) suspended local days: no batches, cross-midnight kept ok")

    # =================================================================== #
    # Consumption forecast
    # (POST /api/recipes/{id}/versions/{v}/consumption-forecast)
    # =================================================================== #
    # Saved recipe: 10 L batch uses A 1500 mL + B 125 mL -> 0.15 mL A and
    # 0.0125 mL B per litre of irrigation water.
    r = client.post("/api/recipes", json={
        "name": "预测配方", "note": "v1", "request": base_request()})
    assert r.status_code == 201, r.text
    fc_rid = r.json()["recipe_id"]
    fc_url = f"/api/recipes/{fc_rid}/versions/1/consumption-forecast"

    def fc_body(**overrides) -> dict:
        body = {
            "timezone": "Asia/Shanghai",
            "start_date": "2026-05-01",
            "end_date": "2026-05-03",
            "sessions": [
                {"start_time": "08:00", "duration_minutes": 10,
                 "water_ml": 500},
                {"start_time": "08:10:30", "duration_minutes": 5,
                 "water_ml": 200},  # merges with 08:00 -> 700 mL
                {"start_time": "23:55", "duration_minutes": 10,
                 "water_ml": 300},   # crosses midnight
            ],
            "stock": [
                {"concentrate": "A液", "stock_ml": 100},
                {"concentrate": "B液", "stock_ml": 10},
            ],
        }
        body.update(overrides)
        return body

    # 22) proportional draw-down, totals and shortage -------------------- #
    r = client.post(fc_url, json=fc_body())
    assert r.status_code == 200, r.text
    d = r.json()
    # 3 days x 1000 mL = 3000 mL -> A 450 mL, B 37.5 mL required
    assert d["planned_water_ml"] == 3000
    assert d["batch_count_before_merge"] == 9
    assert d["batch_count_after_merge"] == 6
    assert d["sufficient"] is False
    assert d["first_shortfall_date"] == "2026-05-01"
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert abs(liq["A液"]["mix_ml_per_l"] - 150.0) < 1e-9
    assert abs(liq["B液"]["mix_ml_per_l"] - 12.5) < 1e-9
    assert abs(liq["A液"]["total_required_ml"] - 450) < 1e-6
    assert abs(liq["B液"]["total_required_ml"] - 37.5) < 1e-6
    # clamped consumption, explicit gap, never negative
    assert liq["A液"]["total_consumed_ml"] == 100
    assert liq["B液"]["total_consumed_ml"] == 10
    assert abs(liq["A液"]["total_shortfall_ml"] - 350) < 1e-6
    assert abs(liq["B液"]["total_shortfall_ml"] - 27.5) < 1e-6
    assert liq["A液"]["remaining_ml"] == 0
    assert liq["B液"]["remaining_ml"] == 0

    # full projection kept after depletion; remaining never negative
    for day in d["day_results"]:
        for l in day["liquids"]:
            assert l["remaining_ml"] >= 0, l
            assert l["consumed_ml"] <= l["required_ml"] + 1e-9
    last = {x["concentrate"]: x for x in d["day_results"][-1]["liquids"]}
    assert abs(last["A液"]["required_ml"] - 150) < 1e-6
    assert last["A液"]["consumed_ml"] == 0

    # 23) merged / cross-midnight batches counted exactly once ----------- #
    days = {x["date"]: x for x in d["day_results"]}
    assert days["2026-05-01"]["batches"] == 2
    assert days["2026-05-01"]["water_ml"] == 1000
    assert days["2026-05-03"]["batches"] == 2
    xb = [b for b in d["batches"] if b["crosses_midnight"]]
    assert len(xb) == 3
    assert xb[0]["attributed_date"] == "2026-05-01"
    assert xb[2]["attributed_date"] == "2026-05-03"
    mb = d["batches"][0]  # merged 500+200=700 mL batch
    assert mb["merged"] and mb["batch_count_before_merge"] == 2
    mla = {x["concentrate"]: x for x in mb["liquids"]}
    assert abs(mla["A液"]["required_ml"] - 105) < 1e-9  # 0.15 * 700
    assert mla["A液"]["consumed_ml"] == 100
    assert mla["A液"]["shortfall_ml"] == 5

    # 24) sufficient stock and exact end-of-horizon exhaustion ----------- #
    ok = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 100000}])).json()
    assert ok["sufficient"] is True and ok["first_shortfall_date"] is None
    assert all(b["sufficient"] for b in ok["batches"])
    exact = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 37.5}])).json()
    assert exact["sufficient"] is True
    assert exact["liquids"][1]["remaining_ml"] == 0
    # shortage first appearing on day 3
    late = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 100000},
        {"concentrate": "B液", "stock_ml": 32.5}])).json()
    assert late["sufficient"] is False
    assert late["first_shortfall_date"] == "2026-05-03"
    assert late["liquids"][1]["first_shortfall_date"] == "2026-05-03"
    assert late["liquids"][0]["first_shortfall_date"] is None

    # 25) forecast 4xx: missing/unknown/negative/duplicate stock, bad plan #
    assert client.post(
        f"/api/recipes/999/versions/1/consumption-forecast",
        json=fc_body()).status_code == 404
    assert client.post(
        f"/api/recipes/{fc_rid}/versions/99/consumption-forecast",
        json=fc_body()).status_code == 404

    def fc_422(overrides, label):
        rr = client.post(fc_url, json=fc_body(**overrides))
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])

    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100},
                      {"concentrate": "C液", "stock_ml": 10}]},
           "unknown concentrate")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100}]},
           "missing concentrate")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": 100},
                      {"concentrate": "A液", "stock_ml": 10},
                      {"concentrate": "B液", "stock_ml": 10}]},
           "duplicate stock")
    fc_422({"stock": [{"concentrate": "A液", "stock_ml": -1},
                      {"concentrate": "B液", "stock_ml": 10}]},
           "negative stock")
    fc_422({"stock": []}, "empty stock")
    fc_422({"timezone": "Mars/Olympus"}, "bad tz")
    fc_422({"end_date": "2026-04-30"}, "bad range")
    fc_422({"sessions": []}, "empty sessions")
    bad_fc = fc_body()
    bad_fc["bogus"] = 1
    assert client.post(fc_url, json=bad_fc).status_code == 422
    rr = client.post(fc_url, json=fc_body(stock=[
        {"concentrate": "A液", "stock_ml": 1},
        {"concentrate": "C液", "stock_ml": 1}]))
    assert "未知浓缩液" in rr.json()["detail"]["message"]
    print("22-25) consumption forecast: proportional deduction, "
          "single-count batches, clamping, 4xx all ok")

    # 26) infeasible force-saved version: warning but forecast computed -- #
    bad_req = base_request()
    bad_req["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/recipes?force=true",
                    json={"name": "bad-forecast", "request": bad_req})
    assert r.status_code == 201
    bad_id = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{bad_id}/versions/1/consumption-forecast",
        json=fc_body(stock=[
            {"concentrate": "A液", "stock_ml": 50},
            {"concentrate": "B液", "stock_ml": 50}])).json()
    assert d["recipe_feasible"] is False
    assert d["warning"] and "不可行" in d["warning"]
    print("26) infeasible version forecast carries warning")

    # 27) suspended days schedule no batches and consume no stock -------- #
    d = client.post(fc_url, json=fc_body(
        suspended_dates=["2026-05-02"],
        stock=[{"concentrate": "A液", "stock_ml": 100000},
               {"concentrate": "B液", "stock_ml": 100000}])).json()
    assert d["suspended_dates"] == ["2026-05-02"]
    assert d["planned_water_ml"] == 2000  # day 1 + day 3 only
    assert d["batch_count_before_merge"] == 6
    assert d["batch_count_after_merge"] == 4
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert abs(liq["A液"]["total_required_ml"] - 300) < 1e-6
    assert abs(liq["B液"]["total_required_ml"] - 25) < 1e-6
    fdays = {x["date"]: x for x in d["day_results"]}
    sday = fdays["2026-05-02"]
    assert sday["suspended"] is True
    assert sday["batches"] == 0 and sday["water_ml"] == 0
    assert sday["sufficient"] is True
    # end-of-day stock unchanged across the suspended day
    rem1 = {x["concentrate"]: x["remaining_ml"]
            for x in fdays["2026-05-01"]["liquids"]}
    rem2 = {x["concentrate"]: x["remaining_ml"]
            for x in sday["liquids"]}
    assert rem1 == rem2
    # cross-midnight batch begun day 1 still deducted once on day 1
    attrs = sorted(b["attributed_date"] for b in d["batches"])
    assert attrs == ["2026-05-01", "2026-05-01",
                     "2026-05-03", "2026-05-03"]
    # shortage that would land on day 2 is skipped to the next active day
    late = client.post(fc_url, json=fc_body(
        suspended_dates=["2026-05-02"],
        stock=[{"concentrate": "A液", "stock_ml": 100000},
               {"concentrate": "B液", "stock_ml": 20}])).json()
    assert late["first_shortfall_date"] == "2026-05-03"
    fc_422({"suspended_dates": ["2026-05-02", "2026-05-02"]},
           "duplicate suspended date")
    fc_422({"suspended_dates": ["2026-05-04"]}, "suspended after range")
    print("27) forecast suspended days: no deduction, cross-midnight kept")

    # =================================================================== #
    # Integer pump-stroke dispense plan
    # (POST /api/recipes/{id}/versions/{v}/dispense-plan)
    # =================================================================== #
    # Same A/B chemistry as base_request(): continuous optimum 1500/125.
    r = client.post("/api/recipes", json={
        "name": "泵送配方", "note": "integer strokes", "request": base_request()})
    assert r.status_code == 201, r.text
    dp_rid = r.json()["recipe_id"]
    dp_url = f"/api/recipes/{dp_rid}/versions/1/dispense-plan"

    def dp_body(qa, qb):
        return {"pumps": [
            {"concentrate": "A液", "pump_step_ml": qa},
            {"concentrate": "B液", "pump_step_ml": qb}]}

    def brute_ab(qa, qb):
        """Independent oracle: enumerate every legal integer stroke pair.

        Mirrors the hard constraints (cap=2000 each, water >= 7000,
        concentrate <= 2000) and the minimax objective; ties are broken by
        the total number of strokes, exactly like the API.
        """
        V = 10000.0
        best = None
        for ka in range(int(2000 // qa) + 1):
            for kb in range(int(2000 // qb) + 1):
                xa, xb = ka * qa, kb * qb
                x = xa + xb
                w = V - x
                if x > 2000 + 1e-9 or w < 7000 - 1e-9:
                    continue
                n = xa * 1200 / 1000
                p = xb * 4000 / 1000
                k = (xa * 400 + xb * 2000) / 1000
                t = max(abs(n - 1800) / 1800, abs(p - 500) / 500,
                        abs(k - 850) / 850)
                cand = (t, ka + kb, ka, kb, x, w)
                if best is None or cand[:2] < best[:2]:
                    best = cand
        assert best is not None
        return best

    def assert_matches_oracle(qa, qb):
        t_exp, pumps_exp, ka, kb, x_exp, w_exp = brute_ab(qa, qb)
        d = client.post(dp_url, json=dp_body(qa, qb)).json()
        assert d["feasible"] is True and d["optimal"] is True, d
        got = {p["concentrate"]: p for p in d["pumps"]}
        assert got["A液"]["pump_count"] == ka, (qa, qb, d)
        assert got["B液"]["pump_count"] == kb, (qa, qb, d)
        assert got["A液"]["volume_ml"] == ka * qa
        assert got["B液"]["volume_ml"] == kb * qb
        assert abs(d["max_relative_deviation"] - t_exp) < 1e-6, d
        assert d["total_pump_count"] == pumps_exp
        assert abs(d["concentrate_total_ml"] - x_exp) < 1e-6
        assert abs(d["water_volume_ml"] - w_exp) < 1e-6
        # exact volume closure: dilution water fills the remainder
        assert abs(
            d["water_volume_ml"] + d["concentrate_total_ml"] - 10000
        ) < 1e-6
        # element report is consistent with the actual dosages + water
        for e in d["elements"]:
            assert abs(
                e["actual_amount_mg"]
                - e["concentrate_amount_mg"] - e["source_water_amount_mg"]
            ) < 1e-6
        return d

    # 35) exact integer grid -> zero deviation --------------------------- #
    d = assert_matches_oracle(125, 125)
    assert d["within_tolerance"] is True
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert got["A液"]["pump_count"] == 12 and got["B液"]["pump_count"] == 1
    assert d["water_volume_ml"] == 8375.0
    assert all(e["status"] == "ok" for e in d["elements"])
    print("35) dispense exact grid: 12x125 + 1x125, water 8375, dev 0")

    # 36) MILP matches brute force on a range of step sizes --------------- #
    for qa, qb in [(125, 130), (200, 200), (300, 125), (375, 90),
                   (125, 300), (600, 600), (500, 300), (1000, 1000)]:
        d = assert_matches_oracle(qa, qb)
        t_exp = brute_ab(qa, qb)[0]
        assert d["within_tolerance"] == (t_exp <= 0.05 + 1e-7), (qa, qb, d)
    print("36) dispense MILP matches brute-force oracle across step sizes")

    # 37) best executable plan still returned when tolerance unreachable - #
    # A 200 mL/pump, B 125 mL/pump: oracle optimum k=(7,1) -> 1400+125 mL:
    # N 1680 (-6.67%), P 500 (exact), K 810 (-4.71%); feasible but beyond
    # the 5% tolerance, with an ELEMENT_DEVIATION on N -------------------- #
    d = client.post(dp_url, json=dp_body(200, 125)).json()
    assert d["feasible"] is True and d["within_tolerance"] is False
    assert d["optimal"] is True
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert (got["A液"]["pump_count"], got["B液"]["pump_count"]) == (7, 1)
    assert abs(d["max_relative_deviation"] - 0.066666667) < 1e-4
    assert d["water_volume_ml"] == 8475.0
    bad_elems = {v["element"]: v for v in d["violations"]
                 if v["code"] == "ELEMENT_DEVIATION"}
    assert set(bad_elems) == {"N"}, d["violations"]
    assert {e["element"]: e["status"] for e in d["elements"]} == {
        "N": "under", "P": "ok", "K": "ok"}
    assert "整数泵送步长" in d["message"]
    print("37) out-of-tolerance but executable:",
          d["max_relative_deviation"], list(bad_elems))

    # 37b) coarse B-only grid: every choice under-feeds N/P by 100%, so the
    # optimum is the zero-stroke plan (t=1), still returned as executable - #
    d = client.post(dp_url, json=dp_body(125, 300)).json()
    assert d["feasible"] is True and d["within_tolerance"] is False
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert (got["A液"]["pump_count"], got["B液"]["pump_count"]) == (0, 0)
    assert abs(d["max_relative_deviation"] - 1.0) < 1e-9
    assert d["water_volume_ml"] == 10000.0
    assert {v["element"] for v in d["violations"]} == {"N", "P", "K"}
    print("37b) zero-stroke optimum returned when all plans miss badly")

    # 38) step larger than bottle cap: that pump cannot fire even once ---- #
    d = client.post(dp_url, json=dp_body(125, 2001)).json()
    assert d["feasible"] is True
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert got["B液"]["max_pumps"] == 0 and got["B液"]["pump_count"] == 0
    assert got["B液"]["note"] and "超过" in got["B液"]["note"]
    assert got["A液"]["note"] is None
    print("38) oversized step forces 0 pumps with a note:",
          got["B液"]["note"][:50])

    # 39) raw-water baseline: source contribution tracks ACTUAL water ----- #
    raw_disp = {
        "target_volume_ml": 10000,
        "targets": {"concentrations": {"Ca": 100, "Mg": 40}},
        "concentrates": [
            {"name": "C液", "available_volume": 2000,
             "concentrations": {"Ca": 640, "Mg": 130}}
        ],
        "source_water": {"concentrations": {"ca": 40, "mg": 30}},
        "max_concentrate_fraction": 0.20,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/recipes",
                    json={"name": "原水泵送配方", "request": raw_disp})
    sw_rid = r.json()["recipe_id"]
    sw_url = f"/api/recipes/{sw_rid}/versions/1/dispense-plan"
    d = client.post(sw_url, json={
        "pumps": [{"concentrate": "C液", "pump_step_ml": 250}]}).json()
    assert d["feasible"] is True and d["within_tolerance"] is True
    p = d["pumps"][0]
    assert (p["pump_count"], p["volume_ml"]) == (4, 1000.0)
    assert d["water_volume_ml"] == 9000.0
    rows = {e["element"]: e for e in d["elements"]}
    assert abs(rows["Ca"]["concentrate_amount_mg"] - 640) < 1e-6
    assert abs(rows["Ca"]["source_water_amount_mg"] - 360) < 1e-6  # 40*9
    assert abs(rows["Mg"]["source_water_amount_mg"] - 270) < 1e-6  # 30*9
    # coarse 300 mL step -> oracle optimum k=3 (Ca/Mg both +/− 6%)
    d300 = client.post(sw_url, json={
        "pumps": [{"concentrate": "C液", "pump_step_ml": 300}]}).json()
    assert d300["feasible"] is True
    assert d300["pumps"][0]["pump_count"] == 3
    assert abs(d300["water_volume_ml"] - 9100) < 1e-6
    assert abs(d300["max_relative_deviation"] - 0.06) < 1e-6
    print("39) dispense raw-water baseline split on actual water")

    # 40) per-bottle max_volume tightens max pump count ------------------- #
    capped = base_request()
    capped["concentrates"][0]["max_volume"] = 500
    r = client.post("/api/recipes?force=true",
                    json={"name": "泵送封顶", "request": capped})
    cap_rid = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{cap_rid}/versions/1/dispense-plan",
        json=dp_body(125, 125)).json()
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert got["A液"]["cap_ml"] == 500.0
    assert got["A液"]["max_pumps"] == 4
    assert got["A液"]["pump_count"] <= 4
    # oracle under the 500 mL cap: (4, 1), minimax dev 66.7%
    assert (got["A液"]["pump_count"], got["B液"]["pump_count"]) == (4, 1)
    assert abs(d["max_relative_deviation"] - 2 / 3) < 1e-6
    assert d["within_tolerance"] is False
    print("40) max_volume caps pump counts (4x125 max), oracle matches")

    # 41) binding minimum dilution water keeps the plan inside the limit -- #
    # Continuous optimum uses 8375 mL water; demanding >= 8400 mL forbids
    # the exact 1625 mL dosage: oracle picks (11 A, 1 B) x 125 = 1500 mL,
    # 8500 mL water (N -8.33% at most) — the plan never violates the floor  #
    mw = base_request()
    mw["min_water_ml"] = 8400
    r = client.post("/api/recipes?force=true",
                    json={"name": "泵送少浓缩", "request": mw})
    mw_rid = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{mw_rid}/versions/1/dispense-plan",
        json=dp_body(125, 125)).json()
    assert d["feasible"] is True, d
    assert d["water_volume_ml"] >= 8400 - 1e-6
    got = {p["concentrate"]: p for p in d["pumps"]}
    assert (got["A液"]["pump_count"], got["B液"]["pump_count"]) == (11, 1)
    assert d["water_volume_ml"] == 8500.0
    assert d["concentrate_total_ml"] == 1500.0
    assert abs(d["max_relative_deviation"] - 0.083333333) < 1e-4
    print("41) min_water floor binds:",
          d["water_volume_ml"], d["concentrate_total_ml"],
          d["max_relative_deviation"])

    # 42) volume hard constraints unsatisfiable -> feasible=false, reason - #
    hard = base_request()
    hard["min_water_ml"] = 12000
    r = client.post("/api/recipes?force=true",
                    json={"name": "泵送硬冲突", "request": hard})
    hard_rid = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{hard_rid}/versions/1/dispense-plan",
        json=dp_body(100, 100)).json()
    assert d["feasible"] is False and d["within_tolerance"] is False
    assert d["optimal"] is False
    codes = [v["code"] for v in d["violations"]]
    assert "WATER_MIN_EXCEEDS_VOLUME" in codes, codes
    assert all(p["pump_count"] == 0 for p in d["pumps"])
    print("42) volume hard-infeasible version:", codes)

    # 43) 4xx: missing recipe/version and illegal pump configurations ----- #
    assert client.post(
        "/api/recipes/999/versions/1/dispense-plan",
        json=dp_body(1, 1)).status_code == 404
    assert client.post(
        f"/api/recipes/{dp_rid}/versions/99/dispense-plan",
        json=dp_body(1, 1)).status_code == 404

    def dp_422(body, label):
        rr = client.post(dp_url, json=body)
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])

    dp_422({"pumps": [{"concentrate": "A液", "pump_step_ml": 125}]},
           "missing B")
    dp_422({"pumps": [
        {"concentrate": "A液", "pump_step_ml": 125},
        {"concentrate": "B液", "pump_step_ml": 125},
        {"concentrate": "C液", "pump_step_ml": 10}]}, "unknown C")
    dp_422({"pumps": [
        {"concentrate": "A液", "pump_step_ml": 125},
        {"concentrate": "A液", "pump_step_ml": 100}]}, "duplicate name")
    dp_422({"pumps": [
        {"concentrate": "A液", "pump_step_ml": 0},
        {"concentrate": "B液", "pump_step_ml": 125}]}, "zero step")
    dp_422({"pumps": [
        {"concentrate": "A液", "pump_step_ml": -5},
        {"concentrate": "B液", "pump_step_ml": 125}]}, "negative step")
    # NaN / Infinity cannot be encoded as standard JSON by httpx; send the
    # raw (non-standard) token and let the server-side validator reject it.
    for token in ("NaN", "Infinity"):
        rr = client.post(
            dp_url,
            content=(
                '{"pumps": [{"concentrate": "A液", "pump_step_ml": '
                + token + '}, '
                '{"concentrate": "B液", "pump_step_ml": 125}]}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert rr.status_code == 422, (token, rr.status_code, rr.text[:200])
    dp_422({"pumps": []}, "empty pumps")
    bad_dp = dp_body(125, 125)
    bad_dp["bogus"] = 1
    dp_422(bad_dp, "extra top-level field")
    dp_422({"pumps": [
        {"concentrate": "A液", "pump_step_ml": "125"},
        {"concentrate": "B液", "pump_step_ml": 125}]}, "string step")
    # domain-level mismatch message (unknown/missing concentrate)
    rr = client.post(dp_url, json={
        "pumps": [{"concentrate": "A液", "pump_step_ml": 125},
                  {"concentrate": "C液", "pump_step_ml": 10}]})
    assert "未使用" in rr.json()["detail"]["message"] and "C液" in \
        rr.json()["detail"]["message"]
    rr = client.post(dp_url, json={
        "pumps": [{"concentrate": "A液", "pump_step_ml": 125}]})
    assert "缺少" in rr.json()["detail"]["message"]
    print("43) dispense-plan 404/422 cases all ok")

    # =================================================================== #
    # Whole-package replenishment procurement
    # (POST /api/recipes/{id}/versions/{v}/procurement-plan)
    # ===================================================================
    # One full 10 L batch mixes A 1500 mL + B 125 mL (base_request recipe).
    r = client.post("/api/recipes", json={
        "name": "采购配方", "note": "procurement", "request": base_request()})
    assert r.status_code == 201, r.text
    pc_rid = r.json()["recipe_id"]
    pc_url = f"/api/recipes/{pc_rid}/versions/1/procurement-plan"

    def pc_body(**overrides) -> dict:
        body = {
            "timezone": "Asia/Shanghai",
            "start_date": "2026-05-01",
            "end_date": "2026-05-01",
            "sessions": [{"start_time": "08:00", "duration_minutes": 10,
                          "water_ml": 10000}],
            "stock": [
                {"concentrate": "A液", "stock_ml": 0},
                {"concentrate": "B液", "stock_ml": 0},
            ],
            "quotes": [],
        }
        body.update(overrides)
        return body

    same_day_quotes = [
        {"concentrate": "A液", "package_ml": 1000, "price": 100,
         "arrival_date": "2026-05-01", "max_packages": 2},
        {"concentrate": "A液", "package_ml": 1500, "price": 100,
         "arrival_date": "2026-05-01", "max_packages": None},
        {"concentrate": "B液", "package_ml": 125, "price": 50,
         "arrival_date": "2026-05-01", "max_packages": None},
    ]

    # 44) minimum cost, tie-break on total packages ---------------------- #
    d = client.post(pc_url, json=pc_body(quotes=same_day_quotes)).json()
    assert d["feasible"] is True
    # A: 1x1500 @100 beats 2x1000 @200 and ties 1x1000 (only 1000 < 1500);
    # cheapest feasible is exactly one 1500 mL package (100) — fewest packs.
    assert d["total_cost"] == 150.0
    assert d["total_packages"] == 2
    got = {q["index"]: q["packages"] for q in d["quotes"]}
    assert got == {1: 0, 2: 1, 3: 1}, got
    b = d["batches"][0]
    inbound = {x["quote_index"]: (x["concentrate"], x["packages"])
               for x in b["inbound"]}
    assert inbound == {2: ("A液", 1), 3: ("B液", 1)}
    rem = {x["concentrate"]: x["remaining_ml"] for x in b["liquids"]}
    assert rem == {"A液": 0.0, "B液": 0.0}
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert abs(liq["A液"]["total_required_ml"] - 1500) < 1e-9
    assert abs(liq["B液"]["mix_ml_per_l"] - 12.5) < 1e-9
    print("44) procurement min cost + min packs tie-break:",
          d["total_cost"], d["total_packages"], "packs")

    # 44b) same total price -> strictly fewer packages preferred --------- #
    d = client.post(pc_url, json=pc_body(quotes=[
        {"concentrate": "A液", "package_ml": 750, "price": 100,
         "arrival_date": "2026-05-01"},
        {"concentrate": "A液", "package_ml": 1500, "price": 100,
         "arrival_date": "2026-05-01"},
        {"concentrate": "B液", "package_ml": 125, "price": 50,
         "arrival_date": "2026-05-01"},
    ])).json()
    assert d["feasible"]
    got = {q["index"]: q["packages"] for q in d["quotes"]}
    assert got == {1: 0, 2: 1, 3: 1}, got  # 1x1500 not 2x750
    assert d["total_cost"] == 150 and d["total_packages"] == 2
    print("44b) equal price -> fewer packages")

    # 44c) cheaper-but-more-packages quote wins on price ----------------- #
    d = client.post(pc_url, json=pc_body(quotes=[
        {"concentrate": "A液", "package_ml": 500, "price": 30,
         "arrival_date": "2026-05-01"},
        {"concentrate": "A液", "package_ml": 1500, "price": 100,
         "arrival_date": "2026-05-01"},
        {"concentrate": "B液", "package_ml": 125, "price": 50,
         "arrival_date": "2026-05-01"},
    ])).json()
    got = {q["index"]: q["packages"] for q in d["quotes"]}
    # 3x500 @30 = 90 < 100 even though it is 3 packs
    assert got == {1: 3, 2: 0, 3: 1}, got
    assert d["total_cost"] == 140
    print("44c) price dominates package count")

    # 45) stock alone covers the schedule -> zero packages --------------- #
    d = client.post(pc_url, json=pc_body(stock=[
        {"concentrate": "A液", "stock_ml": 1500},
        {"concentrate": "B液", "stock_ml": 125}])).json()
    assert d["feasible"] and d["total_cost"] == 0 and d["total_packages"] == 0
    assert d["quotes"] == [] and d["first_shortfall"] is None
    assert d["batches"][0]["inbound"] == []
    print("45) sufficient stock -> empty order, cost 0")

    # 46) arrival after the first batch -> infeasible, no fabricated order #
    d = client.post(pc_url, json=pc_body(
        end_date="2026-05-02",
        sessions=[{"start_time": "08:00", "duration_minutes": 10,
                   "water_ml": 10000}],
        quotes=[
            {"concentrate": "A液", "package_ml": 1500, "price": 100,
             "arrival_date": "2026-05-02"},
            {"concentrate": "B液", "package_ml": 125, "price": 50,
             "arrival_date": "2026-05-02"},
        ])).json()
    assert d["feasible"] is False
    assert d["total_cost"] is None and d["total_packages"] is None
    assert all(q["packages"] is None and q["cost"] is None
               and q["volume_ml"] is None for q in d["quotes"])
    fs = d["first_shortfall"]
    assert fs["batch_index"] == 1
    assert fs["attributed_date"] == "2026-05-01"
    assert {x["concentrate"] for x in fs["liquids"]} == {"A液", "B液"}
    assert all(abs(x["available_ml"]) < 1e-9 for x in fs["liquids"])
    assert "未生成" in fs["message"]
    # diagnostic trajectory keeps full batches and non-negative stock
    assert len(d["batches"]) == 2
    assert d["batches"][0]["sufficient"] is False
    assert all(l["remaining_ml"] >= 0 for b in d["batches"] for l in b["liquids"])
    print("46) late quotes infeasible, earliest gap batch 1, no fake order")

    # 46b) supply cap too small -> shortfall with available/shortfall mL -- #
    d = client.post(pc_url, json=pc_body(quotes=[
        {"concentrate": "A液", "package_ml": 1000, "price": 100,
         "arrival_date": "2026-05-01", "max_packages": 1},
        {"concentrate": "B液", "package_ml": 125, "price": 50,
         "arrival_date": "2026-05-01"},
    ])).json()
    assert d["feasible"] is False
    fs = d["first_shortfall"]
    assert [x["concentrate"] for x in fs["liquids"]] == ["A液"]
    assert abs(fs["liquids"][0]["available_ml"] - 1000) < 1e-9
    assert abs(fs["liquids"][0]["shortfall_ml"] - 500) < 1e-9
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert liq["A液"]["capped_supply_ml"] == 1000.0
    assert liq["A液"]["purchased_ml"] is None
    print("46b) capped supply reports exact gap (1000 vs 1500)")

    # 46c) early batches covered by stock, gap only on day 2 ------------- #
    d = client.post(pc_url, json=pc_body(
        end_date="2026-05-02",
        stock=[{"concentrate": "A液", "stock_ml": 1500},
               {"concentrate": "B液", "stock_ml": 125}],
        quotes=[{"concentrate": "A液", "package_ml": 1500, "price": 100,
                 "arrival_date": "2026-05-03"},
                {"concentrate": "B液", "package_ml": 125, "price": 50,
                 "arrival_date": "2026-05-03"}])).json()
    assert d["feasible"] is False
    assert d["first_shortfall"]["batch_index"] == 2
    print("46c) first shortfall points at the first uncovered batch (2)")

    # 47) arrival is stocked before that day's FIRST local batch --------- #
    # 08:00 and 08:10:30 merge into one 700 mL batch; a same-day arrival is
    # available to it; a 00:00 batch must also see same-day arrivals.
    d = client.post(pc_url, json=pc_body(sessions=[
        {"start_time": "00:00", "duration_minutes": 5, "water_ml": 10000}],
        quotes=same_day_quotes)).json()
    assert d["feasible"] is True and d["total_cost"] == 150.0
    assert d["batches"][0]["local_start"] == "2026-05-01T00:00:00+0800"
    assert len(d["batches"][0]["inbound"]) == 2
    print("47) same-day delivery usable even at local 00:00 batch")

    # 47b) arrival before the horizon folds into initial availability ----- #
    d = client.post(pc_url, json=pc_body(quotes=[
        {"concentrate": "A液", "package_ml": 1500, "price": 100,
         "arrival_date": "2026-04-30"},
        {"concentrate": "B液", "package_ml": 125, "price": 50,
         "arrival_date": "2026-04-20"},
    ])).json()
    assert d["feasible"] and d["total_cost"] == 150
    liq = {x["concentrate"]: x for x in d["liquids"]}
    assert liq["A液"]["initial_available_ml"] == 1500
    assert liq["B液"]["initial_available_ml"] == 125
    assert d["batches"][0]["inbound"] == []
    print("47b) pre-horizon arrivals fold into initial available stock")

    # 48) cross-midnight batch draws once at START; next-day arrival no help #
    d = client.post(pc_url, json=pc_body(
        sessions=[{"start_time": "23:55", "duration_minutes": 10,
                   "water_ml": 10000}],
        quotes=[
            {"concentrate": "A液", "package_ml": 1500, "price": 100,
             "arrival_date": "2026-05-02"},
            {"concentrate": "B液", "package_ml": 125, "price": 50,
             "arrival_date": "2026-05-01"},
        ])).json()
    assert d["feasible"] is False
    assert d["batches"][0]["crosses_midnight"] is True
    assert [x["concentrate"]
            for x in d["first_shortfall"]["liquids"]] == ["A液"]
    print("48) cross-midnight batch: 05-02 delivery not usable at 05-01 23:55")

    # 48b) two days with a 23:55 batch each: day-1 batch needs day-1 stock;
    #      day-2 delivery can still serve day-2's 23:55 batch ------------- #
    d = client.post(pc_url, json=pc_body(
        end_date="2026-05-02",
        sessions=[{"start_time": "23:55", "duration_minutes": 10,
                   "water_ml": 10000}],
        quotes=[
            {"concentrate": "A液", "package_ml": 1500, "price": 100,
             "arrival_date": "2026-05-01"},
            {"concentrate": "A液", "package_ml": 1500, "price": 90,
             "arrival_date": "2026-05-02"},
            {"concentrate": "B液", "package_ml": 125, "price": 50,
             "arrival_date": "2026-05-01"},
            {"concentrate": "B液", "package_ml": 125, "price": 40,
             "arrival_date": "2026-05-02"},
        ])).json()
    assert d["feasible"], d.get("first_shortfall")
    got = {q["index"]: q["packages"] for q in d["quotes"]}
    assert got == {1: 1, 2: 1, 3: 1, 4: 1}, got
    assert d["total_cost"] == 280
    print("48b) per-day timing: expensive day-1 + cheap day-2 packs")

    # 49) merged batch demand uses the MERGED water; arrivals appear on the
    #     day's first batch only ----------------------------------------- #
    d = client.post(pc_url, json=pc_body(sessions=[
        {"start_time": "08:00", "duration_minutes": 10, "water_ml": 5000},
        {"start_time": "08:10:30", "duration_minutes": 5, "water_ml": 5000}],
        quotes=same_day_quotes)).json()
    assert d["feasible"]
    assert d["batch_count_before_merge"] == 2
    assert d["batch_count_after_merge"] == 1
    b0 = d["batches"][0]
    assert b0["merged"] and b0["water_ml"] == 10000
    need = {x["concentrate"]: x["required_ml"] for x in b0["liquids"]}
    assert abs(need["A液"] - 1500) < 1e-9 and abs(need["B液"] - 125) < 1e-9
    print("49) merged-batch demand from merged water, single inbound entry")

    # 49b) delivery on a suspended day is usable by the next active day --- #
    d = client.post(pc_url, json=pc_body(
        start_date="2026-05-01", end_date="2026-05-03",
        sessions=[{"start_time": "08:00", "duration_minutes": 10,
                   "water_ml": 10000}],
        suspended_dates=["2026-05-02"],
        stock=[{"concentrate": "A液", "stock_ml": 1500},
               {"concentrate": "B液", "stock_ml": 125}],
        quotes=[
            {"concentrate": "A液", "package_ml": 1500, "price": 100,
             "arrival_date": "2026-05-02"},
            {"concentrate": "B液", "package_ml": 125, "price": 50,
             "arrival_date": "2026-05-02"},
        ])).json()
    assert d["feasible"], d.get("first_shortfall")
    assert len(d["batches"]) == 2
    assert d["batches"][1]["attributed_date"] == "2026-05-03"
    assert len(d["batches"][1]["inbound"]) == 2
    print("49b) suspended-day arrival serves the next active batch")

    # 50) 404 / 422 matrix ------------------------------------------------ #
    good = pc_body(quotes=same_day_quotes)
    assert client.post(
        "/api/recipes/999/versions/1/procurement-plan", json=good
    ).status_code == 404
    assert client.post(
        f"/api/recipes/{pc_rid}/versions/99/procurement-plan", json=good
    ).status_code == 404

    def pc_422(overrides_or_body, label):
        body = (overrides_or_body if isinstance(overrides_or_body, dict)
                and "quotes" in overrides_or_body
                else pc_body(**overrides_or_body))
        rr = client.post(pc_url, json=body)
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])

    bad = json.loads(json.dumps(good))
    bad["quotes"][0]["concentrate"] = "C液"
    rr = client.post(pc_url, json=bad)
    assert rr.status_code == 422 and "未知浓缩液" in rr.text
    bad = json.loads(json.dumps(good))
    bad["quotes"].append({"concentrate": "C液", "package_ml": 1, "price": 1,
                          "arrival_date": "2026-05-01"})
    rr = client.post(pc_url, json=bad)
    assert rr.status_code == 422 and "未知浓缩液" in rr.text
    bad = json.loads(json.dumps(good))
    bad["stock"] = [{"concentrate": "A液", "stock_ml": 0},
                    {"concentrate": "C液", "stock_ml": 0}]
    assert client.post(pc_url, json=bad).status_code == 422
    bad = json.loads(json.dumps(good))
    bad["stock"] = [{"concentrate": "A液", "stock_ml": 0}]
    assert client.post(pc_url, json=bad).status_code == 422
    bad = json.loads(json.dumps(good))
    bad["stock"] = [{"concentrate": "A液", "stock_ml": 0},
                    {"concentrate": "A液", "stock_ml": 1},
                    {"concentrate": "B液", "stock_ml": 0}]
    assert client.post(pc_url, json=bad).status_code == 422
    bad = json.loads(json.dumps(good))
    bad["stock"][0]["stock_ml"] = -1
    assert client.post(pc_url, json=bad).status_code == 422

    for field, value in [
        ("package_ml", 0), ("package_ml", -5), ("package_ml", "1500"),
        ("price", 0), ("price", -1), ("price", "100"),
        ("max_packages", -1), ("max_packages", 1.5),
        ("arrival_date", "not-a-date"),
    ]:
        bad = json.loads(json.dumps(good))
        bad["quotes"][0][field] = value
        assert client.post(pc_url, json=bad).status_code == 422, (field, value)
    for token in ("NaN", "Infinity"):
        bad = json.loads(json.dumps(good))
        bad["quotes"][0]["package_ml"] = None
        raw = json.dumps(bad).replace(
            '"package_ml": null', '"package_ml": ' + token, 1)
        rr = client.post(pc_url, content=raw,
                         headers={"Content-Type": "application/json"})
        assert rr.status_code == 422, token
    bad = json.loads(json.dumps(good))
    bad["bogus"] = 1
    assert client.post(pc_url, json=bad).status_code == 422
    bad = json.loads(json.dumps(good))
    bad["quotes"][0]["bogus"] = 1
    assert client.post(pc_url, json=bad).status_code == 422
    # inherited irrigation-schedule validation
    pc_422({"timezone": "Mars/Olympus", "quotes": []}, "bad timezone")
    pc_422({"end_date": "2026-04-30", "quotes": []}, "bad range")
    pc_422({"sessions": [], "quotes": []}, "empty sessions")
    pc_422({"suspended_dates": ["2026-05-02", "2026-05-02"],
            "quotes": []}, "duplicate suspended")
    # empty quotes alone is legal (stock may suffice)
    assert client.post(pc_url, json=pc_body(stock=[
        {"concentrate": "A液", "stock_ml": 1500},
        {"concentrate": "B液", "stock_ml": 125}])).status_code == 200
    print("50) procurement 404/422 matrix ok")

    # 51) infeasible (force-saved) version: warning echoed, plan computed - #
    bad_req = base_request()
    bad_req["concentrates"][0]["available_volume"] = 50
    r = client.post("/api/recipes?force=true",
                    json={"name": "bad-procurement", "request": bad_req})
    assert r.status_code == 201
    bp_rid = r.json()["recipe_id"]
    d = client.post(
        f"/api/recipes/{bp_rid}/versions/1/procurement-plan",
        json=pc_body(stock=[
            {"concentrate": "A液", "stock_ml": 100000},
            {"concentrate": "B液", "stock_ml": 100000}])).json()
    assert d["recipe_feasible"] is False
    assert d["warning"] and "不可行" in d["warning"]
    assert d["feasible"] is True
    print("51) infeasible version procurement carries warning")

    # 52) brute-force oracle over random small instances ------------------ #
    import itertools
    import random
    one_liq_recipe = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 50}},
        "concentrates": [
            {"name": "X", "available_volume": 1000,
             "concentrations": {"N": 500}}],
        "max_concentrate_fraction": 1.0, "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/recipes", json={
        "name": "采购暴力枚举", "request": one_liq_recipe})
    assert r.status_code == 201
    x_rid = r.json()["recipe_id"]
    x_url = f"/api/recipes/{x_rid}/versions/1/procurement-plan"

    def brute_one(needs, qs, init):
        """Enumerate optimum (cost, packs); None if infeasible.

        Returns ``(best, optimal_combos)`` where ``optimal_combos`` lists
        every quote-count vector attaining the minimum cost and, within
        that, the minimum package total (spec tie-break), since different
        same-price/same-size quotes are interchangeable.
        """
        doms = [range((cap if cap is not None else 6) + 1)
                for _, _, _, cap in qs]
        best = None
        allbest: list[tuple] = []
        for combo in itertools.product(*doms):
            level, ok = init, True
            for t, need in enumerate(needs):
                for j, (size, _, cut, _cap) in enumerate(qs):
                    if cut == t:
                        level += size * combo[j]
                level -= need
                if level < -1e-7:
                    ok = False
                    break
            if not ok:
                continue
            cost = sum(price * combo[j]
                       for j, (_, price, _, _) in enumerate(qs))
            cand = (cost, sum(combo), tuple(combo))
            if best is None or cand[:2] < best[:2]:
                best = cand
                allbest = [cand]
            elif cand[:2] == best[:2]:
                allbest.append(cand)
        return best, [b[2] for b in allbest]

    rng = random.Random(42)
    checked = 0
    for _ in range(400):
        days = rng.randint(1, 3)
        water = rng.choice([50, 100, 150, 200])
        init = rng.choice([0, 50, 100])
        qs = []
        payload_quotes = []
        for _j in range(rng.randint(0, 3)):
            size = rng.choice([25, 50, 75, 100, 125])
            price = rng.choice([5, 10, 15, 20])
            cut = rng.randint(0, days)  # days == arrives after last batch
            cap = rng.choice([None, 0, 1, 2, 3])
            qs.append((size, price, cut, cap))
            payload_quotes.append({
                "concentrate": "X", "package_ml": size, "price": price,
                "arrival_date": f"2026-05-{1 + cut:02d}",
                "max_packages": cap})
        body = {
            "timezone": "UTC",
            "start_date": "2026-05-01",
            "end_date": f"2026-05-{days:02d}",
            "sessions": [{"start_time": "12:00", "duration_minutes": 5,
                          "water_ml": water}],
            "stock": [{"concentrate": "X", "stock_ml": init}],
            "quotes": payload_quotes,
        }
        d = client.post(x_url, json=body).json()
        needs = [0.1 * water] * days
        exp, alt = brute_one(needs, qs, init)
        if exp is None:
            assert d["feasible"] is False, (checked, d)
            assert d["first_shortfall"] is not None
        else:
            assert d["feasible"] is True, (checked, d)
            assert abs(d["total_cost"] - exp[0]) < 1e-6, (
                checked, d["total_cost"], exp)
            assert d["total_packages"] == exp[1], (
                checked, d["total_packages"], exp)
            assert tuple(q["packages"] for q in d["quotes"]) in alt
        checked += 1
    print(f"52) procurement brute-force oracle matched on {checked} instances")

    # =================================================================== #
    # In-tank retest correction
    # (POST /api/recipes/{id}/versions/{v}/retest-correction)
    # =================================================================== #
    # Single-concentrate version: target N=100 mg/L in 1000 mL; concentrate
    # M carries N=50000 mg/L (50 mg per mL).
    rt_recipe = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             "concentrations": {"N": 50000, "Fe": 1000}},
        ],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/recipes", json={
        "name": "复测纠偏配方", "note": "retest", "request": rt_recipe})
    assert r.status_code == 201, r.text
    rt_rid = r.json()["recipe_id"]
    rt_url = f"/api/recipes/{rt_rid}/versions/1/retest-correction"

    def rt_body(**overrides) -> dict:
        body = {
            "current_volume_ml": 500,
            "final_volume_ml": 1000,
            "measurements": [
                {"element": "n", "lower_mg_l": 80, "upper_mg_l": 82}],
            "stock": [{"concentrate": "M", "stock_ml": 100}],
        }
        body.update(overrides)
        return body

    # 53) minimax balance: x* = 1.19 mL centres the interval on the target #
    # Tank holds 40..41 mg N (80..82 mg/L x 500 mL); target at 1000 mL is
    # 100 mg. Adding 59.5 mg (1.19 mL of M) centres the final interval at
    # 99.5..100.5 mg/L -> worst endpoint deviation 0.5% <= tolerance 5%.
    before = client.get(f"/api/recipes/{rt_rid}/versions/1").json()
    r = client.post(rt_url, json=rt_body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["recipe_id"] == rt_rid and d["version"] == 1
    assert d["within_tolerance"] is True and d["optimal"] is True
    add = {a["concentrate"]: a for a in d["additions"]}
    assert abs(add["M"]["volume_ml"] - 1.19) < 1e-4, add
    assert add["M"]["stock_ml"] == 100.0
    assert abs(d["water_to_add_ml"] - 498.81) < 1e-4
    assert abs(d["concentrate_total_ml"] - 1.19) < 1e-4
    # volume closure: current + concentrates + water == final
    assert abs(d["current_volume_ml"] + d["concentrate_total_ml"]
               + d["water_to_add_ml"] - d["final_volume_ml"]) < 1e-6
    assert abs(d["max_relative_deviation"] - 0.005) < 1e-6
    el = d["elements"][0]
    assert el["element"] == "N" and el["status"] == "ok"
    assert el["measured_lower_mg_l"] == 80.0
    assert el["measured_upper_mg_l"] == 82.0
    assert el["target_amount_mg"] == 100.0
    assert abs(el["final_concentration_at_lower"] - 99.5) < 1e-3
    assert abs(el["final_concentration_at_upper"] - 100.5) < 1e-3
    # exact tie between endpoints -> deterministically reported as lower
    assert el["worst_endpoint"] == "lower"
    assert abs(el["relative_deviation"] - (-0.005)) < 1e-6
    assert d["violations"] == []
    assert d["tolerance"] == 0.05
    # read-only: the saved version is byte-identical after the call
    after = client.get(f"/api/recipes/{rt_rid}/versions/1").json()
    assert before == after
    print("53) retest minimax: 1.19 mL M + 498.81 mL water, dev -0.5%")

    # 54) addable stock binds -> best plan still returned, worst endpoint - #
    # Only 0.5 mL of M (25 mg) can be added: final 65..66 mg vs 100 mg.
    d = client.post(rt_url, json=rt_body(
        stock=[{"concentrate": "M", "stock_ml": 0.5}])).json()
    assert d["within_tolerance"] is False and d["optimal"] is True
    assert d["additions"][0]["volume_ml"] == 0.5
    assert abs(d["water_to_add_ml"] - 499.5) < 1e-6
    el = d["elements"][0]
    assert el["worst_endpoint"] == "lower" and el["status"] == "under"
    assert abs(el["relative_deviation"] - (-0.35)) < 1e-6
    assert abs(d["max_relative_deviation"] - 0.35) < 1e-6
    v = d["violations"]
    assert len(v) == 1 and v[0]["code"] == "ELEMENT_DEVIATION"
    assert v[0]["element"] == "N" and "下界" in v[0]["message"]
    assert abs(v[0]["actual"] - 65.0) < 1e-6
    assert abs(v[0]["limit"] - 95.0) < 1e-6  # 100 * (1 - 5%)
    print("54) stock-capped retest returns best plan + worst endpoint")

    # 55) already over target -> pure water dilution, worst endpoint upper #
    # 210..220 mg/L in 500 mL = 105..110 mg; any M makes it worse, so the
    # optimum adds only water: final 105..110 mg/L, worst +10% at the upper
    # endpoint (beyond the 5% tolerance).
    d = client.post(rt_url, json=rt_body(measurements=[
        {"element": "N", "lower_mg_l": 210, "upper_mg_l": 220}])).json()
    assert d["within_tolerance"] is False
    assert d["additions"][0]["volume_ml"] == 0.0
    assert d["water_to_add_ml"] == 500.0
    el = d["elements"][0]
    assert el["worst_endpoint"] == "upper" and el["status"] == "over"
    assert abs(el["relative_deviation"] - 0.10) < 1e-6
    assert abs(el["final_concentration_at_upper"] - 110.0) < 1e-6
    assert d["violations"][0]["element"] == "N"
    assert "上界" in d["violations"][0]["message"]
    print("55) over-target tank: water-only optimum, worst=upper endpoint")

    # 56) multi-element version: minimax bounded by interval widths ------ #
    # Measured intervals have intrinsic width, so t=0 is impossible: P's
    # interval spans 192..200 mg around target 500 mg -> t >= 0.008. The
    # optimum centres P exactly (B 76 mL = 304 mg, unique) and keeps N/K
    # within +-0.8% as well.
    r = client.post("/api/recipes", json={
        "name": "复测多元素", "request": base_request()})
    assert r.status_code == 201
    ab_rid = r.json()["recipe_id"]
    ab_url = f"/api/recipes/{ab_rid}/versions/1/retest-correction"
    d = client.post(ab_url, json={
        "current_volume_ml": 4000,
        "final_volume_ml": 10000,
        "measurements": [
            {"element": "N", "lower_mg_l": 170, "upper_mg_l": 175},
            {"element": "P", "lower_mg_l": 48, "upper_mg_l": 50},
            {"element": "K", "lower_mg_l": 80, "upper_mg_l": 83},
        ],
        "stock": [
            {"concentrate": "A液", "stock_ml": 2000},
            {"concentrate": "B液", "stock_ml": 2000},
        ],
    }).json()
    assert d["within_tolerance"] is True, d
    assert abs(d["max_relative_deviation"] - 0.008) < 1e-6, d
    assert all(e["status"] == "ok" for e in d["elements"])
    adds = {a["concentrate"]: a["volume_ml"] for a in d["additions"]}
    # P must be centred: added 304 mg -> B exactly 76 mL (4 mg/mL)
    assert abs(adds["B液"] - 76.0) < 1e-4, adds
    # N band at t=0.008: added in [1085.6, 1134.4] mg -> A in [904.7, 945.3]
    assert 904.0 <= adds["A液"] <= 946.0, adds
    elems = {e["element"]: e for e in d["elements"]}
    # P endpoints tie at +-0.008 -> deterministic lower-endpoint report
    assert elems["P"]["worst_endpoint"] == "lower"
    assert abs(elems["P"]["relative_deviation"] - (-0.008)) < 1e-6
    assert abs(d["current_volume_ml"] + d["concentrate_total_ml"]
               + d["water_to_add_ml"] - 10000) < 1e-6
    print("56) multi-element retest: minimax 0.8% (P interval width)")

    # 56b) brute-force oracle: fine grid search matches the LP minimax ---- #
    def brute_retest() -> float:
        """Independent oracle: exhaustive 1 mL grid over (x_A, x_B)."""
        V0 = 4000.0
        lo = {"N": 170 * V0 / 1000, "P": 48 * V0 / 1000, "K": 80 * V0 / 1000}
        hi = {"N": 175 * V0 / 1000, "P": 50 * V0 / 1000, "K": 83 * V0 / 1000}
        T = {"N": 1800.0, "P": 500.0, "K": 850.0}
        best = None
        for xa in range(880, 961):
            for xb in range(60, 91):
                if xa + xb > 6000:
                    continue
                t = 0.0
                for e in ("N", "P", "K"):
                    a = {"N": 1.2 * xa, "P": 4.0 * xb,
                         "K": 0.4 * xa + 2.0 * xb}[e]
                    t = max(t,
                            abs(lo[e] + a - T[e]) / T[e],
                            abs(hi[e] + a - T[e]) / T[e])
                if best is None or t < best:
                    best = t
        return best

    t_grid = brute_retest()
    assert abs(d["max_relative_deviation"] - t_grid) < 1e-9, (
        d["max_relative_deviation"], t_grid)
    print("56b) retest LP matches brute-force grid oracle:", t_grid)

    # 57) retest 404/422 matrix ------------------------------------------ #
    assert client.post("/api/recipes/999/versions/1/retest-correction",
                       json=rt_body()).status_code == 404
    assert client.post(
        f"/api/recipes/{rt_rid}/versions/99/retest-correction",
        json=rt_body()).status_code == 404

    def rt_422(body, label):
        rr = client.post(rt_url, json=body)
        assert rr.status_code == 422, (label, rr.status_code, rr.text[:300])
        return rr

    rr = rt_422(rt_body(final_volume_ml=400), "final < current")
    assert "不能小于现有液量" in rr.text
    rr = rt_422(rt_body(measurements=[
        {"element": "N", "lower_mg_l": 90, "upper_mg_l": 80}]),
        "inverted interval")
    assert "颠倒" in rr.text
    rr = rt_422(rt_body(stock=[{"concentrate": "X", "stock_ml": 1}]),
                "unknown stock name")
    assert "未使用" in rr.json()["detail"]["message"]
    rt_422(rt_body(stock=[]), "empty stock")
    rt_422(rt_body(stock=[{"concentrate": "M", "stock_ml": 1},
                          {"concentrate": "M", "stock_ml": 2}]),
           "duplicate stock")
    rt_422(rt_body(stock=[{"concentrate": "M", "stock_ml": -1}]),
           "negative stock")
    rr = rt_422(rt_body(measurements=[
        {"element": "N", "lower_mg_l": 80, "upper_mg_l": 82},
        {"element": "P", "lower_mg_l": 1, "upper_mg_l": 2}]),
        "measurement beyond targets")
    assert "目标之外" in rr.json()["detail"]["message"]
    rt_422(rt_body(measurements=[
        {"element": "N", "lower_mg_l": 80, "upper_mg_l": 82},
        {"element": "n", "lower_mg_l": 1, "upper_mg_l": 2}]),
        "duplicate element (case-insensitive)")
    rt_422(rt_body(measurements=[]), "empty measurements")
    rt_422(rt_body(measurements=[
        {"element": "Xx", "lower_mg_l": 1, "upper_mg_l": 2}]),
        "unknown element symbol")
    rt_422(rt_body(current_volume_ml=0), "zero current volume")
    rt_422(rt_body(final_volume_ml=0), "zero final volume")
    bad = rt_body()
    bad["bogus"] = 1
    rt_422(bad, "extra top-level field")
    bad = rt_body()
    bad["measurements"][0]["bogus"] = 1
    rt_422(bad, "extra measurement field")
    # missing target element K on the 3-element version
    rr = client.post(ab_url, json={
        "current_volume_ml": 4000, "final_volume_ml": 10000,
        "measurements": [
            {"element": "N", "lower_mg_l": 170, "upper_mg_l": 175},
            {"element": "P", "lower_mg_l": 48, "upper_mg_l": 50}],
        "stock": [{"concentrate": "A液", "stock_ml": 2000},
                  {"concentrate": "B液", "stock_ml": 2000}]})
    assert rr.status_code == 422
    assert "缺少" in rr.json()["detail"]["message"]
    # missing stock for one concentrate on the 2-liquid version
    rr = client.post(ab_url, json={
        "current_volume_ml": 4000, "final_volume_ml": 10000,
        "measurements": [
            {"element": "N", "lower_mg_l": 170, "upper_mg_l": 175},
            {"element": "P", "lower_mg_l": 48, "upper_mg_l": 50},
            {"element": "K", "lower_mg_l": 80, "upper_mg_l": 83}],
        "stock": [{"concentrate": "A液", "stock_ml": 2000}]})
    assert rr.status_code == 422
    assert "缺少" in rr.json()["detail"]["message"]
    # NaN / Infinity tokens (non-standard JSON) must be rejected, not 500
    for token in ("NaN", "Infinity"):
        rr = client.post(
            rt_url,
            content=(
                '{"current_volume_ml": 500, "final_volume_ml": 1000,'
                ' "measurements": [{"element": "N", "lower_mg_l": '
                + token + ', "upper_mg_l": 82}],'
                ' "stock": [{"concentrate": "M", "stock_ml": 100}]}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert rr.status_code == 422, (token, rr.status_code, rr.text[:200])
    for token in ("NaN", "Infinity"):
        rr = client.post(
            rt_url,
            content=(
                '{"current_volume_ml": 500, "final_volume_ml": 1000,'
                ' "measurements": [{"element": "N", "lower_mg_l": 80,'
                ' "upper_mg_l": 82}],'
                ' "stock": [{"concentrate": "M", "stock_ml": '
                + token + '}]}'
            ),
            headers={"Content-Type": "application/json"},
        )
        assert rr.status_code == 422, ("stock", token, rr.status_code)
    print("57) retest 404/422 matrix ok")

    # 58) zero-target version rejected ----------------------------------- #
    zt = {
        "target_volume_ml": 1000,
        "targets": {"concentrations": {"N": 100, "B": 0}},
        "concentrates": [
            {"name": "M", "available_volume": 100,
             "concentrations": {"N": 50000, "B": 500}}],
        "max_concentrate_fraction": 0.5,
        "min_water_ml": 0,
        "tolerance": 0.05,
    }
    r = client.post("/api/recipes", json={"name": "零目标配方", "request": zt})
    assert r.status_code == 201, r.text
    z_rid = r.json()["recipe_id"]
    rr = client.post(
        f"/api/recipes/{z_rid}/versions/1/retest-correction",
        json={
            "current_volume_ml": 500, "final_volume_ml": 1000,
            "measurements": [
                {"element": "N", "lower_mg_l": 80, "upper_mg_l": 82},
                {"element": "B", "lower_mg_l": 0, "upper_mg_l": 0}],
            "stock": [{"concentrate": "M", "stock_ml": 100}]})
    assert rr.status_code == 422
    assert "为零" in rr.json()["detail"]["message"]
    print("58) zero-target version rejected with 422")

    # 59) final == current: nothing can be added, state reported as-is ---- #
    d = client.post(rt_url, json=rt_body(final_volume_ml=500)).json()
    assert d["additions"][0]["volume_ml"] == 0.0
    assert d["water_to_add_ml"] == 0.0
    # 80..82 mg/L vs target 100 -> worst -20% at the lower endpoint
    el = d["elements"][0]
    assert el["worst_endpoint"] == "lower" and el["status"] == "under"
    assert abs(el["relative_deviation"] - (-0.20)) < 1e-6
    assert d["within_tolerance"] is False
    print("59) final == current volume: zero-add plan evaluated as-is")

print("\nALL SMOKE TESTS PASSED")
