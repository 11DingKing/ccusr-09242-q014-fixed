"""测试包初始化：在任何被测模块导入前把数据库切到独立测试文件。"""

import os

_TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_gradtrack.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
