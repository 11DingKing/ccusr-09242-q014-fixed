"""核验队列端到端测试：通过本地接口推进虚拟时间验证关键语义。

覆盖：
* 优先级（高风险优先）与普通记录老化防饿死
* 短暂租约到期后重新分配，迟到完成请求不能覆盖新负责人
* 转交冲突（旧令牌立即失效）
* 并发领取同一条记录只有一人成功
* 服务重启后队列状态与审计流水可恢复
* 退回补件与批量补件重新入队（幂等）
"""

import os
import tempfile
import threading
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.clock import clock
from app.core.database import get_db
from app.models import Base, VerificationItem
from app.services import verification_queue as vq
from main import app
from fastapi.testclient import TestClient

API = "/api/v1/verification"


class QueueApiTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="gt_queue_", suffix=".db")
        os.close(fd)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        clock.reset()

        def _override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()
        os.unlink(self.db_path)
        clock.reset()

    # ---- helpers ----
    def enqueue(self, title, risk="普通", year=2026, material="材料不完整", actor="就业办", reason="入队核验"):
        resp = self.client.post(f"{API}/items", json={
            "title": title,
            "risk_level": risk,
            "graduation_year": year,
            "material_status": material,
            "actor": actor,
            "reason": reason,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def claim(self, actor, **extra):
        body = {"actor": actor, "reason": f"{actor}领取", "lease_seconds": 300}
        body.update(extra)
        resp = self.client.post(f"{API}/claim", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def advance(self, seconds):
        resp = self.client.post("/api/v1/dev/clock/advance", json={"seconds": seconds})
        self.assertEqual(resp.status_code, 200, resp.text)


class PriorityTests(QueueApiTestBase):
    def test_high_risk_goes_first(self):
        self.enqueue("普通事项", risk="普通")
        self.enqueue("高风险事项", risk="高风险")

        first = self.claim("张老师")
        self.assertTrue(first["claimed"])
        self.assertEqual(first["item"]["title"], "高风险事项")
        self.assertEqual(first["item"]["owner"], "张老师")
        self.assertIsNotNone(first["item"]["lease_token"])

        second = self.claim("李老师")
        self.assertTrue(second["claimed"])
        self.assertEqual(second["item"]["title"], "普通事项")

        third = self.claim("王老师")
        self.assertFalse(third["claimed"])

    def test_earlier_year_and_complete_material_break_ties(self):
        # 同为普通、同刻入队：更早届次优先
        self.enqueue("2026届", year=2026)
        self.enqueue("2025届", year=2025)
        first = self.claim("张老师")
        self.assertEqual(first["item"]["title"], "2025届")

        second = self.claim("张老师")
        self.assertEqual(second["item"]["title"], "2026届")

        # 同风险同届次：材料齐全者优先
        self.enqueue("缺材料", year=2024, material="材料不完整")
        self.enqueue("材料齐", year=2024, material="材料齐全")
        third = self.claim("张老师")
        self.assertEqual(third["item"]["title"], "材料齐")

    def test_normal_records_age_and_are_not_starved(self):
        # 普通记录先入队等待
        normal = self.enqueue("等待很久的普通事项", risk="普通")

        # 等待 599 秒后，新来的高风险仍优先（加成 600 秒）
        self.advance(599)
        self.enqueue("新来的高风险A", risk="高风险")
        first = self.claim("张老师")
        self.assertEqual(first["item"]["title"], "新来的高风险A")

        # 再等待超过加成窗口：另一条新入队高风险不应再插队到老普通记录前面
        self.advance(5)
        self.enqueue("新来的高风险B", risk="高风险")
        second = self.claim("李老师")
        self.assertEqual(second["item"]["id"], normal["id"])
        self.assertEqual(second["item"]["title"], "等待很久的普通事项")

        third = self.claim("王老师")
        self.assertEqual(third["item"]["title"], "新来的高风险B")


class LeaseAndFencingTests(QueueApiTestBase):
    def test_duplicate_claim_blocked_while_lease_valid(self):
        item = self.enqueue("事项X")

        first = self.claim("张老师")
        self.assertTrue(first["claimed"])

        # 队列中已无可领取记录
        second = self.claim("李老师")
        self.assertFalse(second["claimed"])

        # 指定领取同一条：租约未到期被拒
        resp = self.client.post(f"{API}/claim", json={
            "actor": "李老师", "lease_seconds": 300, "item_id": item["id"],
        })
        self.assertEqual(resp.status_code, 409)

    def test_lease_expiry_allows_reassignment_and_late_complete_rejected(self):
        item = self.enqueue("事项X")
        claimed = self.claim("张老师", lease_seconds=300)
        token_a = claimed["item"]["lease_token"]

        # 租约到期前：旧负责人正常完成是允许的（另一条记录验证续租路径）
        self.advance(299)
        renew = self.client.post(f"{API}/items/{item['id']}/renew", json={
            "actor": "张老师", "token": token_a, "reason": "还没核验完，续租", "lease_seconds": 300,
        })
        self.assertEqual(renew.status_code, 200, renew.text)

        # 续租后 301 秒，租约再次到期
        self.advance(301)
        late_complete = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "张老师", "token": token_a, "reason": "迟到的完成",
        })
        self.assertEqual(late_complete.status_code, 409)
        self.assertIn("租约", late_complete.json()["detail"])

        # 李老师重新分配到该事项
        reassigned = self.claim("李老师", lease_seconds=300)
        self.assertTrue(reassigned["claimed"])
        self.assertEqual(reassigned["item"]["id"], item["id"])
        self.assertEqual(reassigned["item"]["owner"], "李老师")
        token_b = reassigned["item"]["lease_token"]
        self.assertNotEqual(token_a, token_b)

        # 旧负责人拿着旧令牌的迟到完成请求不能覆盖新负责人
        stale = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "张老师", "token": token_a, "reason": "旧负责人迟到完成",
        })
        self.assertEqual(stale.status_code, 409)
        self.assertIn("令牌", stale.json()["detail"])

        # 记录仍属于李老师，并由其正常完成
        got = self.client.get(f"{API}/items/{item['id']}").json()
        self.assertEqual(got["owner"], "李老师")
        self.assertEqual(got["state"], "核验中")

        done = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "李老师", "token": token_b, "reason": "核验通过",
        })
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["state"], "已完成")
        self.assertEqual(done.json()["completed_by"], "李老师")

        # 已完成记录不能重复处理
        again = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "李老师", "token": token_b, "reason": "重复完成",
        })
        self.assertEqual(again.status_code, 409)

    def test_transfer_invalidates_old_token(self):
        item = self.enqueue("事项Y")
        claimed = self.claim("张老师")
        token_a = claimed["item"]["lease_token"]

        # 持错误令牌转交被拒
        bad = self.client.post(f"{API}/items/{item['id']}/transfer", json={
            "actor": "张老师", "to_teacher": "李老师", "token": "deadbeef",
            "reason": "撞库转交", "lease_seconds": 300,
        })
        self.assertEqual(bad.status_code, 409)

        # 不能转给自己
        self_transfer = self.client.post(f"{API}/items/{item['id']}/transfer", json={
            "actor": "张老师", "to_teacher": "张老师", "token": token_a,
            "reason": "自交", "lease_seconds": 300,
        })
        self.assertEqual(self_transfer.status_code, 400)

        # 正常转交
        transfer = self.client.post(f"{API}/items/{item['id']}/transfer", json={
            "actor": "张老师", "to_teacher": "李老师", "token": token_a,
            "reason": "李老师更熟悉该单位", "lease_seconds": 300,
        })
        self.assertEqual(transfer.status_code, 200, transfer.text)
        token_b = transfer.json()["lease_token"]
        self.assertEqual(transfer.json()["owner"], "李老师")
        self.assertNotEqual(token_a, token_b)

        # 旧负责人的旧令牌立即失效：退回/完成都被拒
        stale_return = self.client.post(f"{API}/items/{item['id']}/return", json={
            "actor": "张老师", "token": token_a, "reason": "旧负责人退回",
        })
        self.assertEqual(stale_return.status_code, 409)
        stale_complete = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "张老师", "token": token_a, "reason": "旧负责人完成",
        })
        self.assertEqual(stale_complete.status_code, 409)

        # 新负责人完成成功
        ok = self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "李老师", "token": token_b, "reason": "核验属实，完成",
        })
        self.assertEqual(ok.status_code, 200)


class ReturnAndBatchResubmitTests(QueueApiTestBase):
    def test_return_then_batch_resubmit_requeues(self):
        i1 = self.enqueue("事项1")
        i2 = self.enqueue("事项2")
        i3 = self.enqueue("事项3")

        # 张老师依次领取并退回前两条（每次领取不同事项）
        for item_id in (i1["id"], i2["id"]):
            claimed = self.claim("张老师", item_id=item_id)
            self.assertTrue(claimed["claimed"])
            token = claimed["item"]["lease_token"]
            resp = self.client.post(f"{API}/items/{item_id}/return", json={
                "actor": "张老师", "token": token, "reason": "缺少劳动合同盖章页",
            })
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json()["state"], "待补件")
            self.assertIsNone(resp.json()["owner"])

        got1 = self.client.get(f"{API}/items/{i1['id']}").json()
        self.assertEqual(got1["material_status"], "材料不完整")

        # 退回补件中的事项不能被领取
        blocked_resp = self.client.post(f"{API}/claim", json={
            "actor": "李老师", "lease_seconds": 300, "item_id": i1["id"],
        })
        self.assertEqual(blocked_resp.status_code, 409)

        # 推进虚拟时间，使一直在队列中的 i3 明显老化；随后批量补件
        self.advance(100)
        # 批量补件：i1、i2 到齐；i3 仍在队列应跳过；不存在的 id 进入 missing
        batch = self.client.post(f"{API}/batch-resubmit", json={
            "actor": "材料专员",
            "item_ids": [i1["id"], i2["id"], i3["id"], 99999],
            "reason": "学生已补交盖章页",
            "material_note": "盖章页已扫描归档",
        })
        self.assertEqual(batch.status_code, 200, batch.text)
        body = batch.json()
        self.assertEqual(sorted(body["reopened"]), sorted([i1["id"], i2["id"]]))
        self.assertEqual(body["skipped"], [i3["id"]])
        self.assertEqual(body["missing"], [99999])
        for reopened in body["results"]:
            self.assertEqual(reopened["state"], "待领取")
            self.assertEqual(reopened["material_status"], "材料齐全")

        # 幂等：再次批量补件，全部跳过
        again = self.client.post(f"{API}/batch-resubmit", json={
            "actor": "材料专员", "item_ids": [i1["id"], i2["id"]], "reason": "重复提交",
        })
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["reopened"], [])
        self.assertEqual(sorted(again.json()["skipped"]), sorted([i1["id"], i2["id"]]))

        # 补件到齐的记录可再次被领取并完成（i3 一直在队列中等待更久，正常按优先级先领取它）
        c3 = self.claim("李老师")
        self.assertEqual(c3["item"]["id"], i3["id"])
        self.client.post(f"{API}/items/{i3['id']}/complete", json={
            "actor": "李老师", "token": c3["item"]["lease_token"], "reason": "核验通过",
        })
        claimed = self.claim("李老师")
        self.assertIn(claimed["item"]["id"], (i1["id"], i2["id"]))
        self.assertEqual(claimed["item"]["material_status"], "材料齐全")
        done = self.client.post(f"{API}/items/{claimed['item']['id']}/complete", json={
            "actor": "李老师", "token": claimed["item"]["lease_token"],
            "reason": "补件核验通过",
        })
        self.assertEqual(done.status_code, 200)


class AuditTrailTests(QueueApiTestBase):
    def test_every_action_keeps_actor_and_reason(self):
        item = self.enqueue("审计事项", actor="就业办", reason="风险名单入队")
        claimed = self.claim("张老师", reason="按分工领取")
        token = claimed["item"]["lease_token"]

        self.client.post(f"{API}/items/{item['id']}/return", json={
            "actor": "张老师", "token": token, "reason": "缺就业证明",
        })
        self.client.post(f"{API}/batch-resubmit", json={
            "actor": "材料专员", "item_ids": [item["id"]], "reason": "就业证明已补交",
        })
        claimed2 = self.claim("李老师", item_id=item["id"])
        token2 = claimed2["item"]["lease_token"]
        self.client.post(f"{API}/items/{item['id']}/complete", json={
            "actor": "李老师", "token": token2, "reason": "材料完整，核验完成",
        })

        events = self.client.get(f"{API}/items/{item['id']}/events").json()
        actions = [(e["action"], e["actor"]) for e in events]
        self.assertEqual([a for a, _ in actions],
                         ["入队", "领取", "退回补件", "补件完成", "领取", "完成"])
        for event in events:
            self.assertTrue(event["actor"])
            self.assertTrue(event["reason"])
            self.assertIsNotNone(event["created_at"])

        # 全量流水接口同样可查
        all_events = self.client.get(f"{API}/events/list").json()
        self.assertGreaterEqual(len(all_events), 6)


class ConcurrentClaimTests(unittest.TestCase):
    """直接压服务层：两个会话并发领取，同一条记录只能有一人成功。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="gt_conc_", suffix=".db")
        os.close(fd)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        clock.reset()

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)
        clock.reset()

    def test_two_workers_claim_same_item_only_one_wins(self):
        db = self.Session()
        vq.enqueue(db, title="唯一记录", actor="就业办", reason="t",
                   graduation_year=2026)
        db.close()

        results = {}
        barrier = threading.Barrier(2)

        def worker(name):
            session = self.Session()
            try:
                barrier.wait()
                item = vq.claim_next(session, actor=name, reason="并发领取",
                                     lease_seconds=300)
                results[name] = item.id if item else None
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("张老师",))
        t2 = threading.Thread(target=worker, args=("李老师",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        winners = {k: v for k, v in results.items() if v is not None}
        self.assertEqual(len(winners), 1, results)
        winner_name = next(iter(winners))

        check = self.Session()
        item = check.query(VerificationItem).first()
        self.assertEqual(item.owner, winner_name)
        self.assertEqual(item.state.value, "核验中")
        self.assertIsNotNone(item.lease_token)
        check.close()

    def test_two_workers_two_items_each_get_distinct(self):
        db = self.Session()
        vq.enqueue(db, title="记录A", actor="就业办", reason="t", graduation_year=2025)
        vq.enqueue(db, title="记录B", actor="就业办", reason="t", graduation_year=2026)
        db.close()

        results = {}
        barrier = threading.Barrier(2)

        def worker(name):
            session = self.Session()
            try:
                barrier.wait()
                item = vq.claim_next(session, actor=name, reason="并发领取",
                                     lease_seconds=300)
                results[name] = item.id if item else None
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("张老师",))
        t2 = threading.Thread(target=worker, args=("李老师",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertIsNotNone(results["张老师"])
        self.assertIsNotNone(results["李老师"])
        self.assertNotEqual(results["张老师"], results["李老师"])


class ServiceRestartTests(unittest.TestCase):
    """服务重启（引擎重建）后队列状态、租约与审计流水必须可恢复。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="gt_restart_", suffix=".db")
        os.close(fd)
        clock.reset()

    def tearDown(self):
        os.unlink(self.db_path)
        clock.reset()

    def _make_engine(self):
        engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def test_state_and_audit_survive_restart(self):
        # ---- 第一个“进程”：建库、入队、领取、退回 ----
        engine1, Session1 = self._make_engine()
        db = Session1()
        item = vq.enqueue(
            db, title="跨重启事项", actor="就业办", reason="风险名单",
            graduation_year=2025,
        )
        item_id = item.id
        claimed = vq.claim_next(db, actor="张老师", reason="领取", lease_seconds=300)
        token = claimed.lease_token
        owner = claimed.owner
        lease_until = claimed.lease_until
        vq.return_for_material(db, item_id, actor="张老师", token=token, reason="缺材料")
        db.close()
        engine1.dispose()

        # ---- “服务重启”：全新引擎/会话指向同一个数据库文件 ----
        engine2, Session2 = self._make_engine()
        db2 = Session2()
        restarted = db2.get(VerificationItem, item_id)
        self.assertEqual(restarted.state.value, "待补件")
        self.assertEqual(restarted.material_status.value, "材料不完整")
        self.assertIsNone(restarted.owner)
        self.assertIsNone(restarted.lease_token)

        events = vq.list_events(db2, item_id=item_id)
        self.assertEqual([e.action for e in events], ["入队", "领取", "退回补件"])
        self.assertTrue(all(e.actor and e.reason for e in events))

        # 补件后重新入队，并能在重启后的服务上被领取
        vq.batch_resubmit(db2, actor="材料专员", item_ids=[item_id], reason="补交完成")
        again = vq.claim_next(db2, actor="李老师", reason="重启后领取", lease_seconds=300)
        self.assertIsNotNone(again)
        self.assertEqual(again.owner, "李老师")
        self.assertEqual(again.state.value, "核验中")
        db2.close()
        engine2.dispose()

        # 再重启一次：有效租约仍受尊重，他人不能抢占
        engine3, Session3 = self._make_engine()
        db3 = Session3()
        with self.assertRaises(vq.QueueError) as ctx:
            vq.claim_next(db3, actor="王老师", reason="抢未到期记录",
                          lease_seconds=300, item_id=item_id)
        self.assertEqual(ctx.exception.status_code, 409)
        still = db3.get(VerificationItem, item_id)
        self.assertEqual(still.owner, "李老师")
        db3.close()
        engine3.dispose()

    def test_queued_item_claimable_after_restart(self):
        engine1, Session1 = self._make_engine()
        db = Session1()
        item = vq.enqueue(db, title="重启前入队", actor="就业办", reason="t",
                          graduation_year=2026)
        item_id = item.id
        db.close()
        engine1.dispose()

        engine2, Session2 = self._make_engine()
        db2 = Session2()
        claimed = vq.claim_next(db2, actor="李老师", reason="重启后领取",
                                lease_seconds=300)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, item_id)
        db2.close()
        engine2.dispose()


class ClockEndpointTests(QueueApiTestBase):
    def test_clock_advances_and_reads(self):
        before = self.client.get("/api/v1/dev/clock").json()
        self.assertEqual(before["offset_seconds"], 0.0)

        moved = self.client.post("/api/v1/dev/clock/advance", json={"seconds": 600})
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["offset_seconds"], 600.0)

        reset = self.client.post("/api/v1/dev/clock/reset")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["offset_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
