"""核心业务测试：开案校验、幂等补报、齐量门控、风险分配、
放行/加测/转人工、封存快照裁决、中断恢复与重试幂等、并发冒烟。"""
import threading
import unittest

from calibseq import core, store

ZONES = [
    {"zone_id": "LOW", "name": "低温", "lower": -0.5, "upper": 0.5},
    {"zone_id": "MID", "name": "常温", "lower": -0.5, "upper": 0.5},
    {"zone_id": "HIGH", "name": "高温", "lower": -0.6, "upper": 0.6},
]
TIGHT = [0.10, 0.05, -0.05, -0.10]       # 区间窄，首轮即落容差
WIDE = [0.40, -0.40, 0.30, -0.30]        # 首轮区间超出容差，二轮补样后收敛
TIGHT2 = [0.05, -0.05, 0.02, -0.02]


def make_case(conn, rounds=(4, 8), risk=0.05, zones=None, name="批次T24"):
    return core.create_case(
        conn, name=name, overall_risk=risk,
        zones=ZONES if zones is None else zones, rounds=list(rounds),
    )


def report_values(conn, cid, zone, values, prefix):
    recs = [{"record_id": f"{prefix}-{i}", "zone_id": zone, "value": v}
            for i, v in enumerate(values)]
    return core.report_measurements(conn, cid, recs)


def report_all_zones(conn, cid, values_by_zone, prefix):
    for zid, vals in values_by_zone.items():
        report_values(conn, cid, zid, vals, f"{prefix}-{zid}")


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")

    def test_bad_risk(self):
        for bad in (0, -0.1, 1, 0.9, float("nan"), float("inf"), "x", True):
            with self.subTest(bad=bad):
                with self.assertRaises(core.Validation):
                    make_case(self.conn, risk=bad)

    def test_bad_zones(self):
        with self.assertRaises(core.Validation):
            make_case(self.conn, zones=[])
        with self.assertRaises(core.Validation):  # 下限 >= 上限
            make_case(self.conn, zones=[{"zone_id": "A", "lower": 1, "upper": 1}])
        with self.assertRaises(core.Validation):  # 编号重复
            make_case(self.conn, zones=[
                {"zone_id": "A", "lower": -1, "upper": 1},
                {"zone_id": "A", "lower": -2, "upper": 2},
            ])

    def test_bad_rounds(self):
        for bad in ([], [1], [4, 4], [8, 4], [2.5], ["4"], [4] * 13):
            with self.subTest(bad=bad):
                with self.assertRaises(core.Validation):
                    make_case(self.conn, rounds=bad)

    def test_allocation_sums_to_overall(self):
        k = make_case(self.conn, rounds=(4, 8, 12), risk=0.05)
        alloc = k["allocation"]
        total = alloc["per_zone"] * len(k["zones"]) * k["total_rounds"]
        self.assertAlmostEqual(total, 0.05, places=12)
        self.assertAlmostEqual(alloc["per_round"], 0.05 / 3, places=12)
        self.assertAlmostEqual(alloc["per_zone"], 0.05 / 3 / 3, places=12)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.case = make_case(self.conn)
        self.cid = self.case["id"]

    def test_idempotent_duplicate_and_conflict(self):
        recs = [{"record_id": "R-1", "zone_id": "LOW", "value": 0.1}]
        r1 = core.report_measurements(self.conn, self.cid, recs)
        self.assertEqual(r1["results"][0]["status"], "stored")
        # 同编号同内容：幂等
        r2 = core.report_measurements(self.conn, self.cid, recs)
        self.assertEqual(r2["results"][0]["status"], "duplicate")
        # 同编号不同内容：冲突
        r3 = core.report_measurements(self.conn, self.cid,
                                      [{"record_id": "R-1", "zone_id": "LOW", "value": 0.2}])
        self.assertEqual(r3["results"][0]["status"], "conflict")
        # 全库只有一条
        self.assertEqual(r3["counts"]["LOW"], 1)

    def test_invalid_records(self):
        r = core.report_measurements(self.conn, self.cid, [
            {"record_id": "R-1", "zone_id": "NOPE", "value": 0.1},
            {"record_id": "R-2", "zone_id": "LOW", "value": float("nan")},
            {"record_id": "R-3", "zone_id": "LOW"},
            {"record_id": "bad id!", "zone_id": "LOW", "value": 0.1},
        ])
        self.assertEqual([x["status"] for x in r["results"]],
                         ["invalid", "invalid", "invalid", "invalid"])

    def test_auto_record_id(self):
        r = core.report_measurements(self.conn, self.cid,
                                     [{"zone_id": "LOW", "value": 0.1}])
        self.assertEqual(r["results"][0]["status"], "stored")
        self.assertTrue(r["results"][0]["record_id"].startswith("A-"))


class TestJudgeFlow(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.case = make_case(self.conn)
        self.cid = self.case["id"]

    def test_quota_gating(self):
        report_all_zones(self.conn, self.cid,
                         {"LOW": TIGHT, "MID": TIGHT, "HIGH": TIGHT[:3]}, "r1")
        r = core.judge(self.conn, self.cid)
        self.assertEqual(r["outcome"], "insufficient")
        self.assertEqual(r["shortfall"], {"HIGH": 1})
        # 未齐量不产生判定运行
        self.assertEqual(core.get_case_detail(self.conn, self.cid)["runs"], [])

    def test_pass_first_round(self):
        report_all_zones(self.conn, self.cid,
                         {"LOW": TIGHT, "MID": TIGHT, "HIGH": TIGHT}, "r1")
        r = core.judge(self.conn, self.cid)
        self.assertEqual(r["outcome"], "completed")
        self.assertEqual(r["run"]["decision"], "PASS")
        detail = core.get_case_detail(self.conn, self.cid)
        self.assertEqual(detail["status"], "RELEASED")
        self.assertIsNotNone(detail["decided_at"])
        # 各温区区间确实落在容差内
        for z in r["run"]["result"]["zones"]:
            self.assertTrue(z["within"])
            self.assertGreaterEqual(z["ci_lo"], z["lower"])
            self.assertLessEqual(z["ci_hi"], z["upper"])
        # 定案后拒绝补报
        with self.assertRaises(core.CaseDecided):
            report_values(self.conn, self.cid, "LOW", [0.0], "late")
        # 定案后再判定返回 decided
        again = core.judge(self.conn, self.cid)
        self.assertEqual(again["outcome"], "decided")

    def test_continue_then_pass(self):
        report_all_zones(self.conn, self.cid,
                         {"LOW": TIGHT, "MID": TIGHT, "HIGH": WIDE}, "r1")
        r1 = core.judge(self.conn, self.cid)
        self.assertEqual(r1["run"]["decision"], "CONTINUE")
        detail = core.get_case_detail(self.conn, self.cid)
        self.assertEqual(detail["status"], "OPEN")
        self.assertEqual(detail["current_round"], 2)
        # 高温区首轮未落入
        high = next(z for z in r1["run"]["result"]["zones"] if z["zone_id"] == "HIGH")
        self.assertFalse(high["within"])
        # 第二轮补样后通过
        report_all_zones(self.conn, self.cid,
                         {"LOW": TIGHT2, "MID": TIGHT2, "HIGH": TIGHT2}, "r2")
        r2 = core.judge(self.conn, self.cid)
        self.assertEqual(r2["run"]["decision"], "PASS")
        self.assertEqual(core.get_case_detail(self.conn, self.cid)["status"], "RELEASED")

    def test_final_round_fail_goes_manual(self):
        case = make_case(self.conn, rounds=(4,), name="单轮案件")
        cid = case["id"]
        report_all_zones(self.conn, cid,
                         {"LOW": TIGHT, "MID": TIGHT, "HIGH": WIDE}, "r1")
        r = core.judge(self.conn, cid)
        self.assertEqual(r["run"]["decision"], "MANUAL_REVIEW")
        detail = core.get_case_detail(self.conn, cid)
        self.assertEqual(detail["status"], "MANUAL_REVIEW")
        with self.assertRaises(core.CaseDecided):
            report_values(self.conn, cid, "LOW", [0.0], "late")


class TestSealResume(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.case = make_case(self.conn)
        self.cid = self.case["id"]
        report_all_zones(self.conn, self.cid,
                         {"LOW": TIGHT, "MID": TIGHT, "HIGH": TIGHT}, "r1")

    def test_sealed_snapshot_ignores_concurrent_reports(self):
        # 封存后、计算前到达的补报不影响本次裁决
        seal = core.seal_for_judgment(self.conn, self.cid)
        self.assertEqual(seal["outcome"], "sealed")
        report_all_zones(self.conn, self.cid,
                         {"LOW": [99.0] * 4, "MID": [99.0] * 4, "HIGH": [99.0] * 4}, "wild")
        fin = core.finalize_run(self.conn, seal["run_id"])
        self.assertEqual(fin["run"]["decision"], "PASS")
        for z in fin["run"]["result"]["zones"]:
            self.assertEqual(z["n"], 4)          # 只用封存的 4 条
            self.assertAlmostEqual(z["mean"], 0.0, places=12)
        # 野值仍在库里，计入后续
        detail = core.get_case_detail(self.conn, self.cid)
        self.assertEqual(detail["zones"][0]["count"], 8)

    def test_resume_completes_sealed_run(self):
        seal = core.seal_for_judgment(self.conn, self.cid)
        # 模拟计算中断：不调用 finalize，直接恢复
        r = core.resume(self.conn, self.cid)
        self.assertEqual(r["resumed"], [seal["run_id"]])
        detail = core.get_case_detail(self.conn, self.cid)
        self.assertEqual(detail["status"], "RELEASED")
        self.assertFalse(detail["sealed_pending"])
        # 再次恢复无事可做
        self.assertEqual(core.resume(self.conn, self.cid)["resumed"], [])

    def test_retry_does_not_consume_extra_risk(self):
        seal = core.seal_for_judgment(self.conn, self.cid)
        run_id = seal["run_id"]
        # 中断后重试 judge：应续算同一封存运行，而不是新建
        r1 = core.judge(self.conn, self.cid)
        self.assertEqual(r1["run"]["id"], run_id)
        self.assertFalse(r1["replayed"])
        # 重复 finalize：幂等回放，结果与风险分配完全一致
        r2 = core.finalize_run(self.conn, run_id)
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["run"]["result"], r2["run"]["result"])
        self.assertEqual(r1["run"]["alpha_round"], r2["run"]["alpha_round"])
        # 全案只有一个判定运行：重试没有新增风险额度占用
        runs = core.get_case_detail(self.conn, self.cid)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["alpha_round"], 0.05 / 2)

    def test_pending_seal_reused_by_judge(self):
        seal = core.seal_for_judgment(self.conn, self.cid)
        again = core.seal_for_judgment(self.conn, self.cid)
        self.assertEqual(again["outcome"], "pending")
        self.assertEqual(again["run_id"], seal["run_id"])


class TestConcurrency(unittest.TestCase):
    def test_concurrent_report_and_judge(self):
        conn = store.connect(":memory:")
        case = core.create_case(
            conn, name="并发", overall_risk=0.05, zones=ZONES, rounds=[20])
        cid = case["id"]
        errors = []
        lock = threading.Lock()

        def reporter(tag):
            try:
                for batch in range(6):
                    recs = [
                        {"record_id": f"{tag}-{z}-{batch}-{i}",
                         "zone_id": z, "value": 0.01 * ((i + batch) % 3 - 1)}
                        for z in ("LOW", "MID", "HIGH") for i in range(2)
                    ]
                    core.report_measurements(conn, cid, recs)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(("report", tag, e))

        def judger():
            try:
                for _ in range(40):
                    core.judge(conn, cid)
            except core.CaseDecided:
                pass
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(("judge", "J", e))

        # 3 个报告线程各补 36 条（每温区 12），2 个判定线程并发
        threads = ([threading.Thread(target=reporter, args=(f"T{k}",)) for k in range(3)]
                   + [threading.Thread(target=judger) for _ in range(2)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 判定线程不应因并发报错（样本不足属正常业务返回）
        self.assertEqual([e for e in errors if e[0] == "judge"], [])
        # 补报可能因定案被拒（CaseDecided），其余错误不允许
        self.assertTrue(all(isinstance(e[2], core.CaseDecided) for e in errors if e[0] == "report"))
        detail = core.get_case_detail(conn, cid)
        # 数据无丢失无重复：每线程 36 条，定案前全部入库或部分被拒
        self.assertLessEqual(detail["zones"][0]["count"], 36)
        # 每个轮次至多一个判定运行，且全部完成（无残留 SEALED）
        self.assertFalse(detail["sealed_pending"])
        rounds_seen = [r["round_no"] for r in detail["runs"]]
        self.assertEqual(len(rounds_seen), len(set(rounds_seen)))
        if detail["status"] == "RELEASED":
            self.assertEqual(detail["runs"][-1]["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
