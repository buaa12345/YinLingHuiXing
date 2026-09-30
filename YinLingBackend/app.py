"""
银龄慧行后端服务 (Flask)

功能：
  1. 注册：生成全局唯一雪花 ID，密码经 PBKDF2 哈希后密文存储，写入 MySQL(yinling.users)。
     仅允许注册普通用户；管理员账号由 init_db 预置，只能登录不能注册。
  2. 登录：校验用户名与密文密码。
  3. 语义分析：接收指令，在宿主机终端运行 semantic_route.py，返回第一候选物品与 route 图，
     并把每一次查询写入 navigation_records（含用户索引与正确与否）。
     置信度 = min(原始分数×100×3, 100)：>90 视为正确；60~90 待用户确认/纠错；<60 视为错误。
  4. 导航记录：确认（正确）/ 纠错（错误+文字，并用纠错文字重新导航）/ 按用户分页查询。

启动方式（在 YinLingBackend 目录下）：
    .\\venv\\Scripts\\python.exe app.py
"""
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time

import pymysql
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import config

app = Flask(__name__)
CORS(app)  # 允许 React Native 前端跨域访问

# ============================ 雪花 ID ============================
class Snowflake:
    """64 位全局唯一雪花 ID 生成器。"""

    def __init__(self, worker_id: int = 1, datacenter_id: int = 1, epoch: int = 1609459200000):
        self.worker_id = worker_id
        self.datacenter_id = datacenter_id
        self.sequence = 0
        self.last_timestamp = -1
        self.epoch = epoch
        self._lock = threading.Lock()

    def next_id(self) -> int:
        with self._lock:
            timestamp = int(time.time() * 1000)
            if timestamp < self.last_timestamp:
                raise RuntimeError("时钟回拨，无法生成雪花 ID")
            if timestamp == self.last_timestamp:
                self.sequence = (self.sequence + 1) & 0xFFF
                if self.sequence == 0:
                    while timestamp <= self.last_timestamp:
                        timestamp = int(time.time() * 1000)
            else:
                self.sequence = 0
            self.last_timestamp = timestamp
            snow_id = (
                ((timestamp - self.epoch) << 22)
                | (self.datacenter_id << 17)
                | (self.worker_id << 12)
                | self.sequence
            )
            return snow_id


snowflake = Snowflake()

# ============================ 密码哈希 ============================
def hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 加盐哈希，返回 '盐$哈希' 字符串。"""
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000
    ).hex()
    return f"{salt}${pwd_hash}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的密文是否匹配。"""
    try:
        salt, pwd_hash = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000
    ).hex()
    return hmac.compare_digest(calc, pwd_hash)


# ============================ MySQL 连接 ============================
def get_connection():
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def init_db():
    """建库建表（users + navigation_records），预置默认管理员，并补齐历史表结构。"""
    try:
        conn = pymysql.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` DEFAULT CHARACTER SET utf8mb4")
            cur.execute(f"USE `{config.DB_NAME}`")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    idusers BIGINT PRIMARY KEY,
                    usersname VARCHAR(64) UNIQUE NOT NULL,
                    userspw VARCHAR(255) NOT NULL,
                    id_nursing_home INT DEFAULT 1,
                    role VARCHAR(20) DEFAULT 'user',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 若已存在的表缺少 role 列则补上
            cur.execute("SHOW COLUMNS FROM users LIKE 'role'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user'")
            # 确保 idusers 能放下雪花 ID（若原本是 INT 升级为 BIGINT，保留原主键）
            cur.execute("SHOW COLUMNS FROM users LIKE 'idusers'")
            col = cur.fetchone()
            if col and "bigint" not in str(col.get("Type", "")).lower():
                cur.execute("ALTER TABLE users MODIFY idusers BIGINT NOT NULL")
            # userspw 存放"盐$哈希"，若长度不足 255 则加长
            import re
            cur.execute("SHOW COLUMNS FROM users LIKE 'userspw'")
            col = cur.fetchone()
            if col:
                m = re.search(r"varchar\((\d+)\)", str(col.get("Type", "")).lower())
                if m and int(m.group(1)) < 127:
                    cur.execute("ALTER TABLE users MODIFY userspw VARCHAR(255) NOT NULL")
            # 确保用户名唯一（若表尚未建立唯一索引）
            cur.execute("SHOW INDEX FROM users")
            has_unique = any(
                i.get("Column_name") == "usersname" and i.get("Non_unique") == 0
                for i in cur.fetchall()
            )
            if not has_unique:
                try:
                    cur.execute("ALTER TABLE users ADD UNIQUE INDEX idx_usersname (usersname)")
                except Exception as exc:
                    print(f"[init_db] 无法添加用户名唯一索引：{exc}")
            # 导航查询记录表：以 idusers 为索引（加快按用户分页查询），
            # is_correct 存正确与否（1 正确 / 0 错误 / NULL 待用户反馈）
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS navigation_records (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    idusers BIGINT NOT NULL,
                    query_text VARCHAR(255) NOT NULL,
                    name VARCHAR(128) DEFAULT NULL,
                    object_id VARCHAR(64) DEFAULT NULL,
                    room_id VARCHAR(64) DEFAULT NULL,
                    position VARCHAR(255) DEFAULT NULL,
                    score DOUBLE DEFAULT NULL,
                    confidence DOUBLE DEFAULT NULL,
                    is_correct TINYINT DEFAULT NULL,
                    correction TEXT DEFAULT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    KEY idx_navigation_records_user (idusers, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 预置默认管理员账号（注册接口已不允许注册管理员身份）
            cur.execute(
                "SELECT idusers, role FROM users WHERE usersname = %s",
                (config.DEFAULT_ADMIN_USERNAME,),
            )
            admin_row = cur.fetchone()
            if not admin_row:
                cur.execute(
                    "INSERT INTO users (idusers, usersname, userspw, id_nursing_home, role) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        snowflake.next_id(),
                        config.DEFAULT_ADMIN_USERNAME,
                        hash_password(config.DEFAULT_ADMIN_PASSWORD),
                        config.DEFAULT_NURSING_HOME_ID,
                        "admin",
                    ),
                )
                print(
                    f"[init_db] 已预置默认管理员账号 {config.DEFAULT_ADMIN_USERNAME}"
                    "（初始密码见 config.py，请尽快修改）"
                )
            elif str(admin_row.get("role")) != "admin":
                print(
                    f"[init_db] 警告：已存在名为 {config.DEFAULT_ADMIN_USERNAME} 的普通用户，"
                    "未自动提升为管理员，请手动处理"
                )
        conn.close()
        print("[init_db] 数据库校验完成")
    except Exception as exc:  # 凭据错误等：不阻断启动，运行时报错更清晰
        print(f"[init_db] 警告：初始化数据库失败（{exc}）。请在 config.py 中设置正确的 MySQL 密码。")


# ============================ 注册 / 登录 ============================
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "user").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "用户名和密码不能为空"}), 400
    # 管理员账号只能登录、不能注册：预置账号由 init_db 生成
    if role == "admin":
        return jsonify(
            {"success": False, "error": "管理员账号不允许注册，请使用管理员账号直接登录"}
        ), 403
    role = "user"

    uid = snowflake.next_id()
    pwd_hash = hash_password(password)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 注册前先查重，避免同名重复注册
                cur.execute("SELECT idusers FROM users WHERE usersname = %s", (username,))
                if cur.fetchone():
                    return jsonify({"success": False, "error": "用户名已存在"}), 409
                cur.execute(
                    "INSERT INTO users (idusers, usersname, userspw, id_nursing_home, role) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (uid, username, pwd_hash, config.DEFAULT_NURSING_HOME_ID, role),
                )
        return jsonify(
            {
                "success": True,
                "idusers": str(uid),  # 雪花 ID 超出 JS 安全整数范围，统一用字符串传输
                "usersname": username,
                "role": role,
                "message": "注册成功",
            }
        )
    except pymysql.err.IntegrityError:
        return jsonify({"success": False, "error": "用户名已存在"}), 409
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"success": False, "error": "用户名和密码不能为空"}), 400

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT idusers, usersname, userspw, role FROM users WHERE usersname = %s", (username,))
                row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "error": "用户不存在"}), 404
        if not verify_password(password, row["userspw"]):
            return jsonify({"success": False, "error": "密码错误"}), 401
        return jsonify(
            {
                "success": True,
                "user": {
                    "idusers": str(row["idusers"]),  # 雪花 ID 用字符串传输，避免前端精度丢失
                    "usersname": row["usersname"],
                    "role": row["role"],
                },
            }
        )
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500


@app.route("/api/users", methods=["GET"])
def list_users():
    """管理端：查询所有注册用户。"""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT idusers, usersname, id_nursing_home, role FROM users ORDER BY idusers"
                )
                rows = cur.fetchall()
        # 雪花 ID 用字符串传输，避免前端 JSON 精度丢失
        for row in rows:
            row["idusers"] = str(row["idusers"])
        return jsonify({"success": True, "users": rows})
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500


# ============================ 语义寻路命令 ============================
def build_command(query: str) -> list:
    """按需求拼装 semantic_route.py 命令（列表形式，避免引号转义问题）。"""
    return [
        config.PYTHON_CMD,
        "application/semantic_route.py",
        "--graph-dir",
        config.GRAPH_DIR,
        "--dataset-dir",
        config.DATASET_DIR,
        "--clip-checkpoint",
        config.CLIP_CHECKPOINT,
        "--query",
        query,
        "--output-dir",
        config.OUTPUT_DIR,
        "--start-frame",
        "last",
        "--top-k",
        "5",
    ]


def run_semantic_route(query: str):
    """在宿主机终端运行语义寻路命令，返回 (stdout, stderr)。"""
    cmd = build_command(query)
    proc = subprocess.run(
        cmd,
        cwd=config.RUN_DIR,
        capture_output=True,
        text=True,
        timeout=config.COMMAND_TIMEOUT,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout, proc.stderr


def sync_result_files():
    """把 output-dir 下生成的 route.png / route.json 同步到弹窗读取的 RESULT_DIR。

    说明：命令使用 --output-dir E:/route_result，但前端弹窗读取 E:\\route_result_door，
    这里做一个兜底同步，确保弹窗能拿到最新结果。
    """
    import shutil

    os.makedirs(config.RESULT_DIR, exist_ok=True)
    for name in ("route.png", "route.json"):
        src = os.path.join(config.OUTPUT_DIR, name)
        dst = os.path.join(config.RESULT_DIR, name)
        if os.path.exists(src):
            shutil.copyfile(src, dst)


def first_candidate_from_route(route_dict: dict) -> dict | None:
    """从 route.json 中选取第一候选物品的主要参数。"""
    targets = route_dict.get("ranked_targets")
    if isinstance(targets, list) and targets:
        t = targets[0]
    else:
        t = route_dict.get("target")
    if not isinstance(t, dict):
        return None
    return {
        "object_id": t.get("object_id"),
        "room_id": t.get("room_id"),
        "name": t.get("name"),
        "score": t.get("score"),
        "position": t.get("position"),
    }


def confidence_from_score(score) -> float:
    """置信度换算：min(原始分数 × 100 × 3, 100)，即界面显示百分数的 3 倍、封顶 100。"""
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = 0.0
    return min(value * 100.0 * 3.0, 100.0)


def initial_is_correct(confidence: float):
    """按置信度给出记录的初始判定：>90 正确，<60 错误，60~90 待用户确认/纠错。"""
    if confidence > 90:
        return 1
    if confidence < 60:
        return 0
    return None


def run_route_pipeline(query: str):
    """执行语义寻路命令并读取结果文件，返回 (route_dict, error_message)。

    route_dict 为 None 表示执行失败，error_message 记录原因。
    """
    cmd_error = None
    try:
        run_semantic_route(query)
        # 将 output-dir 生成的结果同步到弹窗读取目录
        sync_result_files()
    except subprocess.TimeoutExpired:
        cmd_error = f"语义分析超时（{config.COMMAND_TIMEOUT}s）"
    except FileNotFoundError:
        cmd_error = f"找不到命令或解释器：{config.PYTHON_CMD}"
    except Exception as exc:
        cmd_error = f"执行异常：{exc}"

    route_path = os.path.join(config.RESULT_DIR, "route.json")
    if not os.path.exists(route_path):
        return None, cmd_error or f"未找到 result 文件：{route_path}"

    try:
        with open(route_path, "r", encoding="utf-8") as f:
            route_dict = json.load(f)
    except Exception as exc:
        return None, f"读取 route.json 失败：{exc}"
    return route_dict, cmd_error


def insert_navigation_record(idusers: int, query: str, target: dict, confidence: float) -> int:
    """写入一条导航查询记录，返回记录自增主键。"""
    try:
        score = float(target.get("score"))
    except (TypeError, ValueError):
        score = None
    position = target.get("position")
    position_text = json.dumps(position, ensure_ascii=False) if position is not None else None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO navigation_records "
                "(idusers, query_text, name, object_id, room_id, position, score, confidence, is_correct) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    idusers,
                    query,
                    target.get("name"),
                    target.get("object_id"),
                    target.get("room_id"),
                    position_text,
                    score,
                    confidence,
                    initial_is_correct(confidence),
                ),
            )
            return int(cur.lastrowid)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"success": False, "error": "指令不能为空"}), 400
    try:
        idusers = int(data.get("idusers"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "缺少用户信息（idusers）"}), 400

    route_dict, cmd_error = run_route_pipeline(query)
    if route_dict is None:
        return jsonify({"success": False, "error": cmd_error}), 500

    target = first_candidate_from_route(route_dict)
    confidence = None
    record_id = None
    if target:
        confidence = confidence_from_score(target.get("score"))
        try:
            record_id = insert_navigation_record(idusers, query, target, confidence)
        except Exception as exc:
            print(f"[analyze] 写入导航记录失败：{exc}")

    message = cmd_error or "分析完成"
    return jsonify(
        {
            "success": True,
            "target": target,
            "confidence": confidence,
            "record_id": record_id,
            "message": message,
        }
    )


@app.route("/api/records/confirm", methods=["POST"])
def confirm_record():
    """用户确认导航结果正确：把记录标记为正确（is_correct=1）。"""
    data = request.get_json(silent=True) or {}
    try:
        record_id = int(data.get("record_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "缺少记录 id"}), 400

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM navigation_records WHERE id = %s", (record_id,))
                if not cur.fetchone():
                    return jsonify({"success": False, "error": "记录不存在"}), 404
                cur.execute(
                    "UPDATE navigation_records SET is_correct = 1 WHERE id = %s", (record_id,)
                )
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500


@app.route("/api/records/correct", methods=["POST"])
def correct_record():
    """用户纠错：原记录标记为错误并保存纠错文字，再用纠错文字重新生成一次导航结果。"""
    data = request.get_json(silent=True) or {}
    try:
        record_id = int(data.get("record_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "缺少记录 id"}), 400
    correction = (data.get("correction") or "").strip()
    if not correction:
        return jsonify({"success": False, "error": "纠错内容不能为空"}), 400

    # 1. 原记录标记为错误，并保存用户输入的纠错内容
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, idusers FROM navigation_records WHERE id = %s", (record_id,)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"success": False, "error": "记录不存在"}), 404
                idusers = int(row["idusers"])
                cur.execute(
                    "UPDATE navigation_records SET is_correct = 0, correction = %s WHERE id = %s",
                    (correction, record_id),
                )
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500

    # 2. 用纠错内容重新执行一次语义寻路，并作为新记录写入
    route_dict, cmd_error = run_route_pipeline(correction)
    if route_dict is None:
        return jsonify({"success": False, "error": cmd_error}), 500

    target = first_candidate_from_route(route_dict)
    confidence = None
    new_record_id = None
    if target:
        confidence = confidence_from_score(target.get("score"))
        try:
            new_record_id = insert_navigation_record(idusers, correction, target, confidence)
        except Exception as exc:
            print(f"[correct] 写入导航记录失败：{exc}")

    message = cmd_error or "纠错后重新分析完成"
    return jsonify(
        {
            "success": True,
            "target": target,
            "confidence": confidence,
            "record_id": new_record_id,
            "message": message,
        }
    )


@app.route("/api/users/<user_id>/records", methods=["GET"])
def list_user_records(user_id):
    """管理端：分页查询某个用户的导航记录（默认每页 10 条，最新优先）。"""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "用户 id 不合法"}), 400
    try:
        page = max(int(request.args.get("page", 1)), 1)
        page_size = min(max(int(request.args.get("page_size", 10)), 1), 50)
    except (TypeError, ValueError):
        page, page_size = 1, 10

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS total FROM navigation_records WHERE idusers = %s",
                    (uid,),
                )
                total = int(cur.fetchone()["total"])
                cur.execute(
                    "SELECT id, query_text, name, confidence, is_correct, correction, created_at "
                    "FROM navigation_records WHERE idusers = %s "
                    "ORDER BY id DESC LIMIT %s OFFSET %s",
                    (uid, page_size, (page - 1) * page_size),
                )
                rows = cur.fetchall()
        items = []
        for row in rows:
            created = row.get("created_at")
            items.append(
                {
                    "id": int(row["id"]),
                    "query_text": row["query_text"],
                    "name": row["name"],
                    "confidence": row["confidence"],
                    "is_correct": row["is_correct"],
                    "correction": row["correction"],
                    "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
                }
            )
        return jsonify(
            {"success": True, "total": total, "page": page, "page_size": page_size, "items": items}
        )
    except Exception as exc:
        return jsonify({"success": False, "error": f"数据库错误：{exc}"}), 500


@app.route("/api/route-image", methods=["GET"])
def route_image():
    image_path = os.path.join(config.RESULT_DIR, "route.png")
    if not os.path.exists(image_path):
        return jsonify({"success": False, "error": "route.png 不存在"}), 404
    return send_file(image_path, mimetype="image/png")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "yinling-backend"})


if __name__ == "__main__":
    init_db()
    # 监听所有网卡，方便局域网内真机访问；端口 5000
    app.run(host="0.0.0.0", port=5000, debug=False)
