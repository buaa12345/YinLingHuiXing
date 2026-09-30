"""
银龄慧行后端配置。

- MySQL 连接：数据库 yinling，表 users。默认使用本机 root，需要你填写密码。
- 语义寻路指令：对应前端需求中的 semantic_route.py 调用参数。
"""
import os

# ============================ MySQL 配置 ============================
# 直接用本机 MySQL，可通过环境变量覆盖（YINLING_DB_HOST 等）。
DB_HOST = os.getenv("YINLING_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("YINLING_DB_PORT", "3306"))
DB_USER = os.getenv("YINLING_DB_USER", "root")
# ⚠️ MySQL root 密码（也可通过环境变量 YINLING_DB_PASSWORD 覆盖）
DB_PASSWORD = os.getenv("YINLING_DB_PASSWORD", "Buaa362880!")
DB_NAME = os.getenv("YINLING_DB_NAME", "yinling")

# 默认养老院 id（测试阶段统一填 1）
DEFAULT_NURSING_HOME_ID = 1

# ======================= 默认管理员账号 =======================
# 首次启动（init_db）时若不存在则自动创建；注册接口不再允许注册管理员身份。
# 初始密码仅用于首次登录，登录后请在数据库中自行修改密码（改 userspw 为新的 PBKDF2 哈希）。
DEFAULT_ADMIN_USERNAME = os.getenv("YINLING_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("YINLING_ADMIN_PASSWORD", "123456")

# ======================= 语义寻路命令配置 =======================
# 需要在宿主机终端中运行的 semantic_route.py 相关路径
HOV_PROJECT_DIR = r"E:\all\06-论文\03-互联网+\HOV-SG-graph-dev-multi-part"
GRAPH_DIR = r"E:/hovsg_output/real_robot/room_orbbec_20260807_104332_merged/graph"
DATASET_DIR = (
    r"E:/all/06-论文/03-互联网+/HOV-SG-graph-dev-multi-part/"
    r"scene_manual_20260731_113213/room_orbbec_20260807_104332_merged"
)
CLIP_CHECKPOINT = "checkpoints/laion2b_s32b_b79k.bin"
OUTPUT_DIR = "E:/route_result"
# 前端弹窗要读取的目录（route.png / route.json）
RESULT_DIR = r"E:\route_result_door"
# 在哪个目录下运行命令
RUN_DIR = HOV_PROJECT_DIR
# 宿主机 python 解释器（语义分析脚本所在环境）
PYTHON_CMD = "python"
# 命令执行超时（秒）
COMMAND_TIMEOUT = 600
