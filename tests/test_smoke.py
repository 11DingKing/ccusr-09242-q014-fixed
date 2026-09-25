"""验证就业追踪服务的基础入口和统计工具。"""

import os
import unittest

# 在任何被测模块导入前切换到独立测试库，避免测试触碰开发数据库
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_gradtrack.db"),
)

from main import app
from app.services.cohort_scope import CohortMember, CohortRule, apply_cohort_rule
from app.utils.salary_utils import format_salary_display


class ServiceSmokeTests(unittest.TestCase):
    def test_application_metadata_and_formatting(self):
        self.assertIn("就业", app.title)
        self.assertEqual(format_salary_display(None), "暂无数据")
        self.assertGreaterEqual(len(app.routes), 8)

    def test_cohort_rule_has_stable_order(self):
        members = [
            CohortMember(2, 2026, 1, None, "verified", "employment", None, None, None, None),
            CohortMember(1, 2026, 1, None, "verified", "employment", None, None, None, None),
        ]
        selected = apply_cohort_rule(members, CohortRule(version="2026-a"))
        self.assertEqual([member.graduate_id for member in selected], [1, 2])


if __name__ == "__main__":
    unittest.main()
