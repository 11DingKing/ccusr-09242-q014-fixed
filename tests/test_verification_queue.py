"""核验队列端到端测试：优先级、租约、转交冲突、重启恢复与批量补件。

测试通过本地接口 /api/v1/clock/advance 推进虚拟时间，不依赖真实等待。
"""

import os
import sys
import unittest

# 在任何被测模块导入前切换到独立测试库，避免测试触碰开发数据库。
# 注意：unittest discover 会把本目录的模块当作顶层模块导入，
# tests/__init__.py 不一定执行，因此每个测试模块都要各自设置。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(_TESTS_DIR, '.test_gradtrack.db')}",
)
# 支持直接 python tests/test_verification_queue.py 运行
if os.path.dirname(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(_TESTS_DIR))

from fastapi.testclient import TestClient

from app.core import engine
from app.models import Base
from main import app

API = "/api/v1"
QUEUE = f"{API}/verification-queue"
CLOCK = f"{API}/clock"


def _make_client() -> TestClient:
    return TestClient(app)


class VerificationQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        self.college_id = self._create_college()

    # ---------- 测试数据准备 ----------

    def _create_college(self) -> int:
        resp = self.client.post(f"{API}/colleges", json={"name": "测试学院", "code": "T001"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _create_graduate(self, student_id: str, year: int = 2026,
                         destination_status: str = "已落实") -> int:
        resp = self.client.post(f"{API}/graduates", json={
            "student_id": student_id,
            "name": f"学生{student_id}",
            "major": "计算机科学",
            "graduation_year": year,
            "college_id": self.college_id,
            "destination_status": destination_status,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _enqueue(self, graduate_id: int, actor: str = "教务员", **kwargs) -> dict:
        payload = {"graduate_id": graduate_id, "actor": actor, "reason": "待核验"}
        payload.update(kwargs)
        resp = self.client.post(f"{QUEUE}/enqueue", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _claim(self, actor: str, expect: int = 200, **kwargs) -> dict:
        payload = {"actor": actor, "reason": "开始核验"}
        payload.update(kwargs)
        resp = self.client.post(f"{QUEUE}/claim", json=payload)
        self.assertEqual(resp.status_code, expect, resp.text)
        return resp.json()

    def _advance(self, seconds: float):
        resp = self.client.post(f"{CLOCK}/advance", json={"seconds": seconds})
        self.assertEqual(resp.status_code, 200, resp.text)

    def _get_task(self, task_id: int) -> dict:
        resp = self.client.get(f"{QUEUE}/tasks/{task_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _get_events(self, task_id: int) -> list:
        resp = self.client.get(f"{QUEUE}/tasks/{task_id}/events")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ---------- 优先级 ----------

    def test_high_risk_claimed_before_normal(self):
        normal_gid = self._create_graduate("S001")
        high_gid = self._create_graduate("S002")
        self._enqueue(normal_gid, risk_level="普通", material_completeness=90)
        self._enqueue(high_gid, risk_level="高风险", material_completeness=20)

        first = self._claim("老师A")
        self.assertEqual(first["task"]["graduate_id"], high_gid)
        self.assertEqual(first["task"]["risk_level"], "高风险")

        second = self._claim("老师B")
        self.assertEqual(second["task"]["graduate_id"], normal_gid)

    def test_aging_prevents_starvation_of_normal_tasks(self):
        normal_gid = self._create_graduate("S011")
        self._enqueue(normal_gid, risk_level="普通", material_completeness=90)

        # 普通任务等待超过高风险基础分（7200 秒）后，新入队的高风险不再插队
        self._advance(7201)
        high_gid = self._create_graduate("S012")
        self._enqueue(high_gid, risk_level="高风险", material_completeness=10)

        first = self._claim("老师A")
        self.assertEqual(first["task"]["graduate_id"], normal_gid)

        second = self._claim("老师B")
        self.assertEqual(second["task"]["graduate_id"], high_gid)

    def test_claim_filters_by_year_risk_and_material(self):
        gid_2025 = self._create_graduate("S021", year=2025)
        gid_2026 = self._create_graduate("S022", year=2026)
        self._enqueue(gid_2025, risk_level="高风险", material_completeness=30)
        self._enqueue(gid_2026, risk_level="高风险", material_completeness=80)

        resp = self._claim("老师A", graduation_year=2026, min_material_completeness=50)
        self.assertEqual(resp["task"]["graduate_id"], gid_2026)

        resp = self._claim("老师B", graduation_year=2026, min_material_completeness=50,
                           expect=409)
        self.assertEqual(resp["detail"]["code"], "no_claimable_task")

    # ---------- 租约 ----------

    def test_lease_blocks_second_claim_until_expiry(self):
        gid = self._create_graduate("S031")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)

        claimed = self._claim("老师A", lease_seconds=300)
        self.assertEqual(claimed["task"]["assignee"], "老师A")
        self.assertEqual(claimed["task"]["version"], task["version"] + 1)

        # 租约未到期：其他老师无法领取
        resp = self._claim("老师B", expect=409)
        self.assertEqual(resp["detail"]["code"], "no_claimable_task")

        # 租约到期后：任务可被重新分配
        self._advance(301)
        reclaimed = self._claim("老师B")
        self.assertEqual(reclaimed["task"]["assignee"], "老师B")
        self.assertEqual(reclaimed["task"]["claim_count"], 2)

        # 原负责人迟到的完成请求不能覆盖新负责人
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师A", "reason": "迟到的完成"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "not_owner")
        self.assertEqual(self._get_task(task["id"])["assignee"], "老师B")

        # 新负责人在租约内可以完成
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师B", "reason": "核验通过"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "已完成")

    def test_expired_lease_rejects_late_completion(self):
        gid = self._create_graduate("S032")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)
        self._claim("老师A", lease_seconds=30)

        self._advance(31)
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师A", "reason": "迟到完成"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "lease_expired")
        # 任务仍未完成，可被重新领取
        self.assertEqual(self._get_task(task["id"])["status"], "核验中")
        self.assertTrue(self._get_task(task["id"])["claimable"])

    def test_renew_extends_lease(self):
        gid = self._create_graduate("S033")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)
        self._claim("老师A", lease_seconds=300)

        self._advance(200)
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/renew",
            json={"actor": "老师A", "reason": "需要更多时间", "lease_seconds": 300},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # 原租约早已过期，但续租后仍不可被他人领取
        self._advance(200)
        resp = self._claim("老师B", expect=409)
        self.assertEqual(resp["detail"]["code"], "no_claimable_task")

        # 续租也到期后才可重新分配
        self._advance(101)
        reclaimed = self._claim("老师B")
        self.assertEqual(reclaimed["task"]["assignee"], "老师B")

    # ---------- 转交 ----------

    def test_transfer_requires_current_owner(self):
        gid = self._create_graduate("S041")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)
        self._claim("老师A")

        # 非持有人转交被拒绝
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/transfer",
            json={"actor": "老师B", "to_assignee": "老师C", "reason": "越权转交"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "not_owner")

        # 持有人转交成功，新负责人获得完整租约
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/transfer",
            json={"actor": "老师A", "to_assignee": "老师B", "reason": "A请假"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["assignee"], "老师B")

        # 原负责人失去操作权，完成请求被拒绝
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师A", "reason": "越权完成"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        # 新负责人在租约内完成
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师B", "reason": "核验通过"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        events = self._get_events(task["id"])
        transfer_events = [e for e in events if e["action"] == "转交"]
        self.assertEqual(len(transfer_events), 1)
        self.assertEqual(transfer_events[0]["actor"], "老师A")
        self.assertEqual(transfer_events[0]["reason"], "A请假")
        self.assertEqual(transfer_events[0]["detail"]["to_assignee"], "老师B")

    def test_transfer_after_lease_expiry_rejected(self):
        gid = self._create_graduate("S042")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)
        self._claim("老师A", lease_seconds=30)

        self._advance(31)
        resp = self.client.post(
            f"{QUEUE}/tasks/{task['id']}/transfer",
            json={"actor": "老师A", "to_assignee": "老师B", "reason": "租约过期后转交"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "lease_expired")

    # ---------- 服务重启恢复 ----------

    def test_restart_recovers_queue_state_and_clock(self):
        gid = self._create_graduate("S051")
        task = self._enqueue(gid, risk_level="高风险", material_completeness=40)
        claimed = self._claim("老师A", lease_seconds=300)
        lease_expires_at = claimed["lease_expires_at"]
        self._advance(100)

        # 模拟服务重启：丢弃数据库连接，重新挂载应用
        engine.dispose()
        with _make_client() as restarted:
            resp = restarted.get(f"{QUEUE}/tasks/{task['id']}")
            self.assertEqual(resp.status_code, 200, resp.text)
            restored = resp.json()
            self.assertEqual(restored["status"], "核验中")
            self.assertEqual(restored["assignee"], "老师A")
            self.assertEqual(restored["lease_expires_at"], lease_expires_at)
            self.assertFalse(restored["claimable"])

            # 重启后虚拟时钟保持：再推进 201 秒租约才到期
            resp = restarted.post(f"{CLOCK}/advance", json={"seconds": 201})
            self.assertEqual(resp.status_code, 200, resp.text)

            resp = restarted.post(f"{QUEUE}/claim", json={"actor": "老师B"})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["task"]["assignee"], "老师B")

            # 审计事件在重启后依然完整
            resp = restarted.get(f"{QUEUE}/tasks/{task['id']}/events")
            actions = [e["action"] for e in resp.json()]
            self.assertEqual(actions, ["入队", "领取", "领取"])

    # ---------- 退回补件与批量补件 ----------

    def test_return_supplement_and_batch_receive(self):
        gid1 = self._create_graduate("S061")
        gid2 = self._create_graduate("S062")
        gid3 = self._create_graduate("S063")
        t1 = self._enqueue(gid1, risk_level="普通", material_completeness=40)
        t2 = self._enqueue(gid2, risk_level="普通", material_completeness=50)
        t3 = self._enqueue(gid3, risk_level="普通", material_completeness=90)

        self._claim("老师A")  # t1
        self._claim("老师A")  # t2

        # 非持有人不能退回
        resp = self.client.post(
            f"{QUEUE}/tasks/{t1['id']}/return-supplement",
            json={"actor": "老师B", "reason": "越权退回"},
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        # 持有人退回补件，任务退出领取池
        for task_id in (t1["id"], t2["id"]):
            resp = self.client.post(
                f"{QUEUE}/tasks/{task_id}/return-supplement",
                json={"actor": "老师A", "reason": "缺少就业证明",
                      "required_documents": ["就业证明"]},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["status"], "待补件")

        claimed = self._claim("老师B")
        self.assertEqual(claimed["task"]["id"], t3["id"])
        resp = self._claim("老师C", expect=409)
        self.assertEqual(resp["detail"]["code"], "no_claimable_task")

        # 批量补件：t1、t2 成功回到队列，t3 状态不符被跳过但不影响其他记录
        resp = self.client.post(f"{QUEUE}/supplement-received/batch", json={
            "actor": "教务员",
            "task_ids": [t1["id"], t2["id"], t3["id"]],
            "reason": "材料已收齐",
            "material_completeness": 100,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(sorted(result["succeeded"]), [t1["id"], t2["id"]])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["task_id"], t3["id"])
        self.assertEqual(result["failed"][0]["code"], "invalid_state")

        for task_id in (t1["id"], t2["id"]):
            task = self._get_task(task_id)
            self.assertEqual(task["status"], "待领取")
            self.assertEqual(task["material_completeness"], 100)
            self.assertTrue(task["claimable"])

        # 批量补件后其他老师可以领取补件任务
        claimed = self._claim("老师C")
        self.assertIn(claimed["task"]["id"], (t1["id"], t2["id"]))

    def test_batch_receive_defaults_to_all_waiting(self):
        gid1 = self._create_graduate("S071", year=2025)
        gid2 = self._create_graduate("S072", year=2026)
        t1 = self._enqueue(gid1, risk_level="普通", material_completeness=40)
        t2 = self._enqueue(gid2, risk_level="普通", material_completeness=40)
        self._claim("老师A")
        self._claim("老师A")
        for task_id in (t1["id"], t2["id"]):
            self.client.post(
                f"{QUEUE}/tasks/{task_id}/return-supplement",
                json={"actor": "老师A", "reason": "缺材料"},
            )

        # 只处理 2025 届的待补件任务
        resp = self.client.post(f"{QUEUE}/supplement-received/batch", json={
            "actor": "教务员", "graduation_year": 2025, "material_completeness": 95,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["succeeded"], [t1["id"]])
        self.assertEqual(self._get_task(t1["id"])["status"], "待领取")
        self.assertEqual(self._get_task(t2["id"])["status"], "待补件")

    # ---------- 审计与入队约束 ----------

    def test_audit_trail_covers_full_lifecycle(self):
        gid = self._create_graduate("S081")
        task = self._enqueue(gid, actor="教务员", reason="年度核验",
                             risk_level="普通", material_completeness=70)
        self._claim("老师A", reason="分工领取")
        self.client.post(
            f"{QUEUE}/tasks/{task['id']}/return-supplement",
            json={"actor": "老师A", "reason": "缺少劳动合同"},
        )
        self.client.post(
            f"{QUEUE}/tasks/{task['id']}/supplement-received",
            json={"actor": "教务员", "reason": "学生已补交", "material_completeness": 100},
        )
        self._claim("老师B")
        self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师B", "reason": "核验通过"},
        )

        events = self._get_events(task["id"])
        self.assertEqual(
            [e["action"] for e in events],
            ["入队", "领取", "退回补件", "补件到位", "领取", "完成"],
        )
        for event in events:
            self.assertTrue(event["actor"])
            self.assertIsNotNone(event["created_at"])
        self.assertEqual(events[0]["actor"], "教务员")
        self.assertEqual(events[0]["reason"], "年度核验")
        self.assertEqual(events[2]["reason"], "缺少劳动合同")

    def test_duplicate_enqueue_rejected_until_completed(self):
        gid = self._create_graduate("S091")
        task = self._enqueue(gid, risk_level="普通", material_completeness=80)

        resp = self.client.post(f"{QUEUE}/enqueue", json={
            "graduate_id": gid, "actor": "教务员", "reason": "重复入队",
        })
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], "already_queued")

        self._claim("老师A")
        self.client.post(
            f"{QUEUE}/tasks/{task['id']}/complete",
            json={"actor": "老师A", "reason": "核验通过"},
        )

        # 完成后允许重新入队（如毕业去向发生变动）
        resp = self.client.post(f"{QUEUE}/enqueue", json={
            "graduate_id": gid, "actor": "教务员", "reason": "去向变动复核",
            "risk_level": "高风险", "material_completeness": 60,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "待领取")
        self.assertEqual(resp.json()["id"], task["id"])

    def test_auto_risk_assessment(self):
        changing_gid = self._create_graduate("S101", destination_status="变动中")
        task = self._enqueue(changing_gid, material_completeness=90)
        self.assertEqual(task["risk_level"], "高风险")

        confirmed_gid = self._create_graduate("S102", destination_status="已落实")
        task = self._enqueue(confirmed_gid, material_completeness=30)
        self.assertEqual(task["risk_level"], "高风险")

        ok_gid = self._create_graduate("S103", destination_status="已落实")
        task = self._enqueue(ok_gid, material_completeness=90)
        self.assertEqual(task["risk_level"], "普通")

    # ---------- 并发领取 ----------

    def test_concurrent_claims_assign_each_task_once(self):
        """多名老师同时领取：每条任务只会被一位老师领到。"""
        import threading

        from app.core import SessionLocal
        from app.services import verification_service

        for i in range(20):
            gid = self._create_graduate(f"S2{i:02d}")
            self._enqueue(gid, risk_level="普通", material_completeness=80)

        barrier = threading.Barrier(4)
        claimed_by: dict[int, str] = {}
        errors: list[str] = []
        lock = threading.Lock()

        def worker(name: str):
            db = SessionLocal()
            try:
                barrier.wait(timeout=10)
                while True:
                    try:
                        task = verification_service.claim_task(db, actor=name)
                    except verification_service.QueueConflict as exc:
                        if exc.code == "no_claimable_task":
                            return
                        with lock:
                            errors.append(f"{name}: {exc.message}")
                        return
                    with lock:
                        if task.id in claimed_by:
                            errors.append(f"任务 {task.id} 被重复领取")
                        claimed_by[task.id] = name
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(f"老师{c}",)) for c in "ABCD"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual(errors, [])
        self.assertEqual(len(claimed_by), 20)

        resp = self.client.get(f"{QUEUE}/tasks", params={"status": "核验中"})
        self.assertEqual(resp.status_code, 200, resp.text)
        tasks = resp.json()
        self.assertEqual(len(tasks), 20)
        self.assertTrue(all(task["assignee"] for task in tasks))


if __name__ == "__main__":
    unittest.main()
