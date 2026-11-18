"""HTTP API 冒烟测试：内存库 + 随机端口的真实服务。"""
import json
import threading
import unittest
import urllib.error
import urllib.request

from calibseq import server, store

ZONES = [
    {"zone_id": "LOW", "name": "低温", "lower": -0.5, "upper": 0.5},
    {"zone_id": "MID", "name": "常温", "lower": -0.5, "upper": 0.5},
]
TIGHT = [0.10, 0.05, -0.05, -0.10]


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = store.connect(":memory:")
        cls.httpd = server.make_server(cls.conn, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {"raw": raw}


class TestApi(ApiTestBase):
    def test_health(self):
        status, data = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_index_page(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        self.assertIn("校准分组序贯判定", html)

    def test_full_flow(self):
        # 开案
        status, case = self.call("POST", "/api/cases", {
            "name": "API流程", "overall_risk": 0.05, "zones": ZONES, "rounds": [4, 8]})
        self.assertEqual(status, 201)
        cid = case["id"]
        self.assertEqual(case["status"], "OPEN")
        # 列表可见
        status, data = self.call("GET", "/api/cases")
        self.assertTrue(any(c["id"] == cid for c in data["cases"]))
        # 齐量前判定 -> insufficient
        status, r = self.call("POST", f"/api/cases/{cid}/judge")
        self.assertEqual(r["outcome"], "insufficient")
        # 补报
        recs = [{"record_id": f"R-{z}-{i}", "zone_id": z, "value": v}
                for z in ("LOW", "MID") for i, v in enumerate(TIGHT)]
        status, r = self.call("POST", f"/api/cases/{cid}/measurements", {"records": recs})
        self.assertEqual(status, 200)
        self.assertEqual(sum(1 for x in r["results"] if x["status"] == "stored"), 8)
        # 幂等重报
        status, r = self.call("POST", f"/api/cases/{cid}/measurements", {"records": recs})
        self.assertEqual(sum(1 for x in r["results"] if x["status"] == "duplicate"), 8)
        # 判定 -> 放行
        status, r = self.call("POST", f"/api/cases/{cid}/judge")
        self.assertEqual(r["outcome"], "completed")
        self.assertEqual(r["run"]["decision"], "PASS")
        # 定案后拒绝补报
        status, r = self.call("POST", f"/api/cases/{cid}/measurements",
                              {"records": [{"record_id": "LATE-1", "zone_id": "LOW", "value": 0.0}]})
        self.assertEqual(status, 409)
        self.assertEqual(r["error"]["code"], "CASE_DECIDED")
        # 详情核对：风险分配与判定历史
        status, detail = self.call("GET", f"/api/cases/{cid}")
        self.assertEqual(detail["status"], "RELEASED")
        self.assertEqual(len(detail["runs"]), 1)
        alloc = detail["allocation"]
        self.assertAlmostEqual(alloc["per_zone"] * 2 * 2, 0.05, places=12)

    def test_validation_and_404(self):
        status, r = self.call("POST", "/api/cases", {
            "name": "坏案件", "overall_risk": 0.05, "zones": ZONES, "rounds": [4, 4]})
        self.assertEqual(status, 400)
        self.assertEqual(r["error"]["code"], "VALIDATION")
        status, r = self.call("GET", "/api/cases/99999")
        self.assertEqual(status, 404)
        status, r = self.call("POST", "/api/cases/99999/judge")
        self.assertEqual(status, 404)
        status, r = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_resume_endpoint(self):
        status, case = self.call("POST", "/api/cases", {
            "name": "恢复", "overall_risk": 0.05, "zones": ZONES, "rounds": [4]})
        cid = case["id"]
        recs = [{"record_id": f"R-{z}-{i}", "zone_id": z, "value": v}
                for z in ("LOW", "MID") for i, v in enumerate(TIGHT)]
        self.call("POST", f"/api/cases/{cid}/measurements", {"records": recs})
        status, r = self.call("POST", f"/api/cases/{cid}/resume")
        self.assertEqual(status, 200)
        self.assertEqual(r["resumed"], [])  # 无封存运行
        status, r = self.call("POST", f"/api/cases/{cid}/judge")
        self.assertEqual(r["run"]["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
