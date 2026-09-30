# 银龄慧行后端 (YinLingBackend)

Flask 后端，为前端 `YinLingHuiXingApp` 提供注册、登录与语义寻路能力。

## 依赖

虚拟环境 `venv` 已安装：`Flask`、`flask-cors`、`PyMySQL`。

## 第 1 步：MySQL 配置

`config.py` 中的 `DB_PASSWORD` 已配置为当前数据库密码。若密码变更，改这里即可
（也可用环境变量 `YINLING_DB_PASSWORD` 覆盖）。

> 说明：`users` 表的 `idusers` 存放雪花 ID（64 位），首次启动会自动建库建表并确保
> `idusers` 为 `BIGINT`、补上 `role` 列、`userspw` 加长到 255、并为 `usersname` 建立唯一索引。
>
> 首次启动还会：
> 1. 创建 `navigation_records` 表（存每一次导航查询，含 `idusers` 索引与 `is_correct` 正确与否字段）；
> 2. 预置默认管理员账号 `admin / 123456`（注册接口不允许注册管理员，登录后请尽快在数据库中修改密码）。
>
> 接口返回的 `idusers` 一律为字符串（雪花 ID 超出 JS 安全整数范围，避免前端精度丢失）。

## 第 2 步：启动后端

在该目录下运行：

```powershell
.\venv\Scripts\python.exe app.py
```

默认监听 `0.0.0.0:5000`，局域网内真机可通过电脑 IP 访问。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/register` | 注册（雪花 ID + 密码 PBKDF2 哈希 + 写入 MySQL；仅限普通用户） |
| POST | `/api/login` | 登录（校验用户名与密文密码） |
| POST | `/api/analyze` | 语义寻路（运行 `semantic_route.py`，计算置信度并写 `navigation_records`） |
| POST | `/api/records/confirm` | 用户确认结果正确（`is_correct=1`） |
| POST | `/api/records/correct` | 用户纠错（原记录标记错误+保存纠错文字，并用纠错文字重新导航） |
| GET | `/api/users/<user_id>/records?page=1&page_size=10` | 管理端：分页查询某用户的导航记录（最新优先） |
| GET | `/api/route-image` | 返回结果弹窗所需的 `route.png` |
| GET | `/api/users` | 管理端：查询所有注册用户 |
| GET | `/health` | 健康检查 |

### 置信度规则（navigation_records.confidence）

`置信度 = min(原始分数×100×3, 100)`，据此写入 `is_correct`：

- `> 90`：直接出结果，`is_correct = 1`（正确）
- `60 ~ 90`：等待用户“确认/纠错”，初始 `is_correct = NULL`（待反馈）；
  点“确认”后置 1，点“纠错”后置 0 并保存纠错文字
- `< 60`：暂停执行，仅可纠错，`is_correct = 0`（错误）

## 语义寻路命令

`config.py` 中的路径即需求给定的命令参数：

```
python application/semantic_route.py --graph-dir ... --dataset-dir ... \
  --clip-checkpoint ... --query "<指令>" --output-dir E:/route_result \
  --start-frame last --top-k 5
```

运行后会把结果同步到 `E:\route_result_door`（`route.png` / `route.json`），供前端弹窗展示
第一候选物品的主要参数。
