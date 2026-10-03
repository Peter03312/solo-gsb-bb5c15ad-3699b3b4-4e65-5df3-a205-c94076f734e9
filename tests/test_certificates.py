"""端到端自测：测温设备校准证明（登记/作废/查询）与按样本的可信测温复核。

运行：python -m tests.test_certificates
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_certificates.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="王采集", seal="SEAL-C", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev="DEV-A"):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev,
        "occurred_at": when, "value_celsius": value})


def cert(cid, dev="DEV-A", start="2026-09-30T00:00:00Z", end="2026-09-30T12:00:00Z",
         issuer="省计量院"):
    return client.post(f"{API}/temperature-certificates", json={
        "cert_id": cid, "device_id": dev, "valid_from": start, "valid_until": end,
        "issuer": issuer})


def void(cid, reason="证明作废"):
    return client.post(f"{API}/temperature-certificates/{cid}/void", json={"reason": reason})


def get_cert(cid):
    return client.get(f"{API}/temperature-certificates/{cid}")


def trusted(code):
    return client.get(f"{API}/samples/{code}/temperature/trusted")


def review(code):
    return client.get(f"{API}/samples/{code}/temperature")


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


# ---------------------------------------------------------------- 1. 证明登记：字段校验与幂等/冲突
print("1) 证明登记：字段校验、同号幂等/异内容冲突")
r = cert("CERT-001")
check("新证明登记 201", r.status_code == 201, r.text)
body = r.json()["certificate"]
check("登记回显字段与有效状态",
      body["cert_id"] == "CERT-001" and body["device_id"] == "DEV-A"
      and body["valid_from"] == "2026-09-30T00:00:00Z"
      and body["valid_until"] == "2026-09-30T12:00:00Z"
      and body["issuer"] == "省计量院"
      and body["status"] == "effective" and body["voided_at"] is None
      and body["void_reason"] is None and body["registered_at"],
      str(body))
check("新建响应 idempotent=false", r.json()["idempotent"] is False, r.text)

r = cert("CERT-001")
check("同号同内容重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等重试返回原记录（登记时间不变）",
      r.json()["certificate"]["registered_at"] == body["registered_at"], r.text)
r = client.post(f"{API}/temperature-certificates", json={
    "cert_id": "CERT-001", "device_id": "DEV-A",
    "valid_from": "2026-09-30T08:00:00+08:00", "valid_until": "2026-09-30T20:00:00+08:00",
    "issuer": "省计量院"})
check("同内容不同时区表示仍幂等（时区归一）", r.status_code == 200, r.text)

r = cert("CERT-001", issuer="另一机构")
check("同号异内容（签发人）409 cert_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "cert_conflict", r.text)
r = cert("CERT-001", dev="DEV-OTHER")
check("同号异内容（设备号）409", r.status_code == 409, r.text)
r = cert("CERT-001", end="2026-09-30T13:00:00Z")
check("同号异内容（截止时刻）409", r.status_code == 409, r.text)

r = cert("CERT-BAD-1", start="2026-09-30T06:00:00Z", end="2026-09-30T06:00:00Z")
check("起止相等（空时段）422", r.status_code == 422, r.text)
r = cert("CERT-BAD-2", start="2026-09-30T07:00:00Z", end="2026-09-30T06:00:00Z")
check("起始晚于截止 422", r.status_code == 422, r.text)
check("无效时段不留记录", get_cert("CERT-BAD-1").status_code == 404
      and get_cert("CERT-BAD-2").status_code == 404)
r = client.post(f"{API}/temperature-certificates", json={
    "cert_id": "", "device_id": "DEV-A", "valid_from": "2026-09-30T00:00:00Z",
    "valid_until": "2026-09-30T01:00:00Z", "issuer": "省计量院"})
check("空证明号 422", r.status_code == 422, r.text)
r = client.post(f"{API}/temperature-certificates", json={
    "cert_id": "CERT-BAD-3", "device_id": "", "valid_from": "2026-09-30T00:00:00Z",
    "valid_until": "2026-09-30T01:00:00Z", "issuer": "省计量院"})
check("空设备号 422", r.status_code == 422, r.text)
r = client.post(f"{API}/temperature-certificates", json={
    "cert_id": "CERT-BAD-4", "device_id": "DEV-A", "valid_from": "2026-09-30T00:00:00Z",
    "valid_until": "2026-09-30T01:00:00Z", "issuer": ""})
check("空签发人 422", r.status_code == 422, r.text)

# ---------------------------------------------------------------- 2. 同设备时段重叠判定
print("2) 同一设备有效证明时段不得重叠（左闭右开）")
# CERT-001: DEV-A [00:00, 12:00)
r = cert("CERT-002", start="2026-09-30T11:00:00Z", end="2026-09-30T13:00:00Z")
check("部分重叠（后段相交）409 cert_period_overlap",
      r.status_code == 409 and r.json()["error"]["code"] == "cert_period_overlap", r.text)
check("重叠明细给出冲突证明号",
      r.json()["error"]["details"]["overlapping"][0]["cert_id"] == "CERT-001", r.text)
r = cert("CERT-003", start="2026-09-30T01:00:00Z", end="2026-09-30T02:00:00Z")
check("被包含（子区间）409", r.status_code == 409, r.text)
r = cert("CERT-004", start="2026-09-29T23:00:00Z", end="2026-09-30T00:30:00Z")
check("部分重叠（前段相交）409", r.status_code == 409, r.text)
r = cert("CERT-005", start="2026-09-29T22:00:00Z", end="2026-09-30T13:00:00Z")
check("包含（覆盖全区间）409", r.status_code == 409, r.text)
check("重叠拒绝不留记录",
      all(get_cert(f"CERT-00{i}").status_code == 404 for i in (2, 3, 4, 5)))

r = cert("CERT-006", start="2026-09-30T12:00:00Z", end="2026-09-30T18:00:00Z")
check("端点相邻（前证明截止=后证明起始）不重叠 201（左闭右开）", r.status_code == 201, r.text)
r = cert("CERT-007", start="2026-09-29T18:00:00Z", end="2026-09-30T00:00:00Z")
check("端点相邻（反向）不重叠 201", r.status_code == 201, r.text)
r = cert("CERT-008", dev="DEV-B", start="2026-09-30T01:00:00Z", end="2026-09-30T05:00:00Z")
check("不同设备同时段不冲突 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 3. 证明作废：理由、幂等与时段释放
print("3) 证明作废：附理由、幂等/冲突、作废后时段释放")
r = void("CERT-NOPE")
check("作废未知证明号 404 cert_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "cert_not_found", r.text)
r = client.post(f"{API}/temperature-certificates/CERT-006/void", json={"reason": "   "})
check("空白作废理由 422", r.status_code == 422, r.text)

r = void("CERT-006", "检定机构撤回原证明")
check("作废成功 200", r.status_code == 200 and r.json()["idempotent"] is False, r.text)
body = r.json()["certificate"]
check("作废状态与理由留痕",
      body["status"] == "voided" and body["void_reason"] == "检定机构撤回原证明"
      and body["voided_at"], str(body))
r = void("CERT-006", "检定机构撤回原证明")
check("同号同理由重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = void("CERT-006", "换一个理由")
check("同号异理由 409 cert_void_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "cert_void_conflict", r.text)

r = cert("CERT-009", start="2026-09-30T12:00:00Z", end="2026-09-30T18:00:00Z")
check("作废后时段释放，可补登同时段新证明 201", r.status_code == 201, r.text)
r = get_cert("CERT-006")
check("作废证明查询仍可见（留痕不删除）",
      r.status_code == 200 and r.json()["certificate"]["status"] == "voided", r.text)
r = get_cert("CERT-NOPE")
check("查询未知证明号 404", r.status_code == 404
      and r.json()["error"]["code"] == "cert_not_found", r.text)

r = cert("CERT-009", start="2026-09-30T12:00:00Z", end="2026-09-30T18:00:00Z")
check("已作废后再登记的同号同内容重试仍幂等 200", r.status_code == 200, r.text)
r = void("CERT-009", "补登的证明也撤回")
check("作废 CERT-009 成功", r.status_code == 200, r.text)
r = cert("CERT-009")
check("已作废证明同号异内容再登记 409（证明号永久占用）",
      r.status_code == 409 and r.json()["error"]["code"] == "cert_conflict", r.text)

# ---------------------------------------------------------------- 4. 可信复核：逐条证明匹配与缺证原因
print("4) 可信复核：逐条列出匹配证明号或缺证原因")
reg("C-S1")
cfg("C-S1")
ev({"event_id": "C-E-T1", "event_type": "transfer", "sample_code": "C-S1",
    "occurred_at": "2026-09-30T02:00:00Z",
    "transfer_no": "TR-C1", "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "C-E-R1", "event_type": "receive", "sample_code": "C-S1",
    "occurred_at": "2026-09-30T04:00:00Z",
    "transfer_no": "TR-C1", "receiver": "李运输", "seal_no": "SEAL-C"})
# DEV-TA 读数：01:30 / 02:30（超温 9.6）/ 04:30；DEV-TB 读数：03:00（无任何证明）
rd("C-S1", "C-RD-1", "2026-09-30T01:30:00Z", 4.0, dev="DEV-TA")
rd("C-S1", "C-RD-2", "2026-09-30T02:30:00Z", 9.6, dev="DEV-TA")
rd("C-S1", "C-RD-3", "2026-09-30T04:30:00Z", 5.0, dev="DEV-TA")
rd("C-S1", "C-RD-4", "2026-09-30T03:00:00Z", 6.0, dev="DEV-TB")

r = trusted("C-S1")
check("可信复核查询 200", r.status_code == 200, r.text)
v = r.json()
check("无任何证明：无法评估标记",
      v["evaluable"] is False and v["trusted_reading_count"] == 0
      and v["unevaluable_reason"], str(v))
check("无法评估时超温点与缺测区间为空",
      v["out_of_range_points"] == [] and v["gap_intervals"] == [], str(v))
reasons = {x["reading_id"]: x["uncovered_reason"] for x in v["readings"]}
check("设备无证明缺证原因 no_certificate_for_device",
      reasons["C-RD-1"]["code"] == "no_certificate_for_device"
      and reasons["C-RD-4"]["code"] == "no_certificate_for_device", str(reasons))
check("缺证读数 certificate 为 null 且 coverage=uncovered",
      all(x["certificate"] is None and x["coverage"] == "uncovered" for x in v["readings"]),
      str(v["readings"]))

# 补录 DEV-TA 证明 [01:00, 03:00)：覆盖 01:30 与 02:30，不覆盖 04:30
cert("CERT-A1", dev="DEV-TA", start="2026-09-30T01:00:00Z", end="2026-09-30T03:00:00Z")
v = trusted("C-S1").json()
check("补录证明后即时重算：有证读数 2 条",
      v["trusted_reading_count"] == 2 and v["evaluable"] is True
      and v["unevaluable_reason"] is None, str(v))
by_id = {x["reading_id"]: x for x in v["readings"]}
check("有证读数给出匹配证明号",
      by_id["C-RD-1"]["coverage"] == "covered"
      and by_id["C-RD-1"]["certificate"]["cert_id"] == "CERT-A1"
      and by_id["C-RD-1"]["uncovered_reason"] is None, str(by_id["C-RD-1"]))
check("有效期外读数缺证原因 reading_outside_certificate_period",
      by_id["C-RD-3"]["uncovered_reason"]["code"] == "reading_outside_certificate_period",
      str(by_id["C-RD-3"]))
check("有效期外缺证明细附设备有效时段",
      by_id["C-RD-3"]["uncovered_reason"]["device_valid_periods"]
      == [{"cert_id": "CERT-A1", "valid_from": "2026-09-30T01:00:00Z",
           "valid_until": "2026-09-30T03:00:00Z"}], str(by_id["C-RD-3"]))

# ---------------------------------------------------------------- 5. 可信复核：仅用有证读数评估
print("5) 可信复核：仅用有证读数计算超温点与缺测区间，沿用保管链拆分与归属")
check("无证超温读数不计入超温点（C-RD-2 有证且超温）",
      [p["reading_id"] for p in v["out_of_range_points"]] == ["C-RD-2"], str(v["out_of_range_points"]))
p = v["out_of_range_points"][0]
check("超温点附证明号与归属（在途移交）",
      p["cert_id"] == "CERT-A1" and p["attribution"]["kind"] == "in_transit"
      and p["attribution"]["transfer_no"] == "TR-C1", str(p))
check("有证读数间隔 60 分钟 > 限值 30 分钟：报缺测区间",
      len(v["gap_intervals"]) == 1
      and v["gap_intervals"][0]["before_reading_id"] == "C-RD-1"
      and v["gap_intervals"][0]["after_reading_id"] == "C-RD-2", str(v["gap_intervals"]))
g = v["gap_intervals"][0]
check("缺测区间跨移交时刻拆分并逐段归属",
      [s["attribution"]["kind"] for s in g["segments"]] == ["custodian", "in_transit"]
      and g["segments"][0]["attribution"]["custodian"] == "王采集"
      and g["segments"][1]["attribution"]["transfer_no"] == "TR-C1", str(g["segments"]))

# 补录 DEV-TA 第二段证明 [03:00, 06:00)：04:30 读数变为有证 → 与 02:30 形成相邻有证对
cert("CERT-A2", dev="DEV-TA", start="2026-09-30T03:00:00Z", end="2026-09-30T06:00:00Z")
v = trusted("C-S1").json()
check("第二段证明后 04:30 读数转为有证",
      v["trusted_reading_count"] == 3
      and {x["reading_id"] for x in v["readings"] if x["coverage"] == "covered"}
      == {"C-RD-1", "C-RD-2", "C-RD-3"}, str(v["readings"]))
check("相邻有证读数重新配对（02:30→04:30 跨签收时刻拆分）",
      len(v["gap_intervals"]) == 2
      and v["gap_intervals"][1]["before_reading_id"] == "C-RD-2"
      and v["gap_intervals"][1]["after_reading_id"] == "C-RD-3"
      and [s["attribution"]["kind"] for s in v["gap_intervals"][1]["segments"]]
      == ["in_transit", "custodian"], str(v["gap_intervals"]))
check("签收后段归属签收人",
      v["gap_intervals"][1]["segments"][1]["attribution"]["custodian"] == "李运输",
      str(v["gap_intervals"]))

# 作废第一段证明：01:30/02:30 读数失去覆盖，即时重算
void("CERT-A1", "证明签发信息有误，作废重开")
v = trusted("C-S1").json()
check("作废证明不再覆盖读数：仅剩 04:30 一条有证",
      v["trusted_reading_count"] == 1
      and [x["reading_id"] for x in v["readings"] if x["coverage"] == "covered"] == ["C-RD-3"],
      str(v["readings"]))
check("单条有证读数：可评估但无缺测区间、无超温点",
      v["evaluable"] is True and v["gap_intervals"] == [] and v["out_of_range_points"] == [],
      str(v))
check("被作废证明覆盖过的读数转为缺证（时刻在剩余有效期外）",
      {x["reading_id"]: x["uncovered_reason"]["code"] for x in v["readings"]
       if x["coverage"] == "uncovered"}["C-RD-1"] == "reading_outside_certificate_period",
      str(v["readings"]))

# 边界：左闭右开——发生时刻=valid_from 覆盖，=valid_until 不覆盖
print("6) 有效期边界（左闭右开）与查询前置校验")
reg("C-S2")
cfg("C-S2")
rd("C-S2", "C-RD-F", "2026-09-30T02:00:00Z", 4.0, dev="DEV-TA2")
rd("C-S2", "C-RD-U", "2026-09-30T05:00:00Z", 4.0, dev="DEV-TA2")
cert("CERT-B1", dev="DEV-TA2", start="2026-09-30T02:00:00Z", end="2026-09-30T05:00:00Z")
v = trusted("C-S2").json()
by_id = {x["reading_id"]: x for x in v["readings"]}
check("发生时刻=valid_from 覆盖（左闭）",
      by_id["C-RD-F"]["coverage"] == "covered", str(by_id["C-RD-F"]))
check("发生时刻=valid_until 不覆盖（右开）",
      by_id["C-RD-U"]["coverage"] == "uncovered"
      and by_id["C-RD-U"]["uncovered_reason"]["code"] == "reading_outside_certificate_period",
      str(by_id["C-RD-U"]))

r = trusted("C-NOPE")
check("可信复核：样本不存在 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
reg("C-S3")
r = trusted("C-S3")
check("可信复核：未设定测温配置 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
cfg("C-S3")
v = trusted("C-S3").json()
check("有配置无读数：无法评估标记",
      v["evaluable"] is False and v["reading_count"] == 0
      and v["unevaluable_reason"], str(v))

# ---------------------------------------------------------------- 7. 与既有功能隔离
print("7) 证明登记/作废不影响原始读数、现有测温查询、保管链版本")
before_review = review("C-S1").json()
before_versions = versions("C-S1")
cert("CERT-A3", dev="DEV-TA", start="2026-09-30T06:00:00Z", end="2026-09-30T09:00:00Z")
void("CERT-A3", "演示隔离性")
after_review = review("C-S1").json()
after_versions = versions("C-S1")
check("现有测温查询结果不变（含无证读数照常参与）",
      before_review == after_review
      and after_review["reading_count"] == 4
      and len(after_review["out_of_range_points"]) == 1, str(after_review))
check("保管链版本不增（证明操作不产生版本）",
      before_versions["current_version"] == after_versions["current_version"],
      f"{before_versions['current_version']} -> {after_versions['current_version']}")
check("读数记录不变",
      [x["reading_id"] for x in after_review["readings"]]
      == ["C-RD-1", "C-RD-2", "C-RD-4", "C-RD-3"], str(after_review["readings"]))

# 核定后：证明登记/作废/可信查询仍可用（设备维度证据，不受样本冻结影响）
reg("C-S4")
cfg("C-S4")
rd("C-S4", "C-RD-A", "2026-09-30T02:00:00Z", 4.0, dev="DEV-TA3")
client.post(f"{API}/samples/C-S4/approve", json={"approver": "质量主管"})
r = cert("CERT-A4", dev="DEV-TA3", start="2026-09-30T01:00:00Z", end="2026-09-30T03:00:00Z")
check("样本核定后仍可登记证明（设备维度）", r.status_code == 201, r.text)
v = trusted("C-S4").json()
check("核定后可信复核查询可用", v["evaluable"] is True and v["trusted_reading_count"] == 1, str(v))
r = void("CERT-A4", "核定后撤回证明")
check("核定后仍可作废证明", r.status_code == 200, r.text)

# ---------------------------------------------------------------- 8. 并发
print("8) 并发：同号同内容一建一幂等；异内容/重叠一胜一败")
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_cert(tag, cid, dev="DEV-C", start="2026-09-30T00:00:00Z",
              end="2026-09-30T06:00:00Z", issuer="省计量院"):
    barrier.wait()
    results[tag] = cert(cid, dev=dev, start=start, end=end, issuer=issuer).status_code


t1 = threading.Thread(target=post_cert, args=("A", "CERT-CC1"))
t2 = threading.Thread(target=post_cert, args=("B", "CERT-CC1"))
t1.start(); t2.start(); t1.join(); t2.join()
check("同号同内容并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=post_cert, args=("C", "CERT-CC2"), kwargs={"dev": "DEV-C2"})
t2 = threading.Thread(target=post_cert, args=("D", "CERT-CC2"),
                      kwargs={"dev": "DEV-C2", "issuer": "另一机构"})
t1.start(); t2.start(); t1.join(); t2.join()
check("同号异内容并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))

results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=post_cert, args=("E", "CERT-CC3"),
                      kwargs={"dev": "DEV-C3", "end": "2026-09-30T08:00:00Z"})
t2 = threading.Thread(target=post_cert, args=("F", "CERT-CC4"),
                      kwargs={"dev": "DEV-C3", "start": "2026-09-30T05:00:00Z"})
t1.start(); t2.start(); t1.join(); t2.join()
check("同设备重叠时段并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))
got = [cid for cid in ("CERT-CC3", "CERT-CC4") if get_cert(cid).status_code == 200]
check("重叠并发恰一条落库", len(got) == 1, str(got))

results.clear()
barrier = threading.Barrier(2)


def post_void(tag, reason):
    barrier.wait()
    results[tag] = void("CERT-CC1", reason).status_code


t1 = threading.Thread(target=post_void, args=("G", "并发作废"))
t2 = threading.Thread(target=post_void, args=("H", "并发作废"))
t1.start(); t2.start(); t1.join(); t2.join()
check("同理由作废并发：均 200（一作废一幂等）", sorted(results.values()) == [200, 200], str(results))
check("并发作废后状态留痕一致",
      get_cert("CERT-CC1").json()["certificate"]["void_reason"] == "并发作废")

check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
