# 电台档案 UTC 查档服务

按录音的 **UTC 时间** 查节目。旧日志只记本地开播时刻，服务接收单频道日志与
**IANA 时区**，负责把本地时刻换算成 UTC；夏令时回拨导致同一本地时刻出现两次时，
**两种 UTC 时刻都会纳入核对**。

- 框架：FastAPI + SQLite（WAL 模式）
- 时段语义：半开区间 `[start, end)`，含起点、不含终点
- 排法：每个节目从其合法 UTC 开播候选中选一个；按流水号顺序首尾不相叠、
  且不与任何停播段相交，即为一种完整排法
- 查询：给定一个 UTC 秒，返回在**至少一种完整排法**中播到该秒的节目流水号，
  以及该秒是否**可能是空档**（存在一种完整排法不播任何节目）

## 目录结构

```
app/
  domain.py    # 时区换算、回拨双候选、DP 可解性/空档判定（O(n·k)）
  db.py        # SQLite 访问、WAL、写事务原子提交、快照读
  schemas.py   # Pydantic 入参/出参与校验
  main.py      # FastAPI 端点
tests/         # pytest：API 端到端 + DP 对暴力枚举的逐秒一致性
Dockerfile, docker-compose.yml
```

## 启动

```bash
docker compose up --build
# 打开交互式文档
open http://localhost:8000/docs
```

数据库存放在命名卷 `radio-data`（容器内 `/data/radio.db`），容器重建后数据保留。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/channels` | 接收/整份替换单频道日志（含 IANA 时区） |
| GET | `/channels/{id}` | 读取一份已提交档案（回显版本号） |
| GET | `/channels/{id}/solve` | 整档是否有解、完整排法数 |
| PUT | `/channels/{id}/blackouts` | 登记/更正一条 UTC 停播段（按 id 覆盖） |
| DELETE | `/channels/{id}/blackouts/{bid}` | 删除停播段 |
| GET | `/channels/{id}/query?utc_second=` | 查档：播出节目 + 空档判定 |

## 在 /docs 试查回拨时段（2024-11-03，America/New_York）

纽约在 2024-11-03 02:00 本地回拨到 01:00，本地 `01:30` 出现两次：

- 第一次（夏令时 EDT, UTC-4）→ `05:30Z` = 纪元秒 **1730611800**
- 第二次（标准时 EST, UTC-5）→ `06:30Z` = 纪元秒 **1730615400**

### 1. `POST /channels` 上传日志

```json
{
  "channel_id": "ny-fm",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-11-03T01:30:00", "duration": 900},
    {"seq": 2, "local_start": "2024-11-03T01:45:00", "duration": 900}
  ]
}
```

响应 `arrangement_count = 3`，对应三种完整排法（早早 / 早晚 / 晚晚）。

### 2. `GET /channels/ny-fm/query?utc_second=1730611800`（05:30Z）

```json
{
  "on_air": [1],
  "may_be_gap": true,
  "in_blackout": false,
  "arrangement_count": 3
}
```

节目 1 在部分排法中播到该秒；选择「晚晚」排法时该秒是空档，故 `may_be_gap=true`。

### 3. `PUT /channels/ny-fm/blackouts` 登记停播段 05:30–06:00Z

```json
{"blackout_id": "bo-1", "start_utc": 1730611800, "end_utc": 1730613600, "note": "检修"}
```

封掉两种含早候选的排法，只剩「晚晚」一种；再查 05:30Z 得到
`on_air=[]`、`may_be_gap=true`、`in_blackout=true`；查 06:30Z（1730615400）
得到 `on_air=[1]`、`may_be_gap=false`（唯一排法里必播）。

### 4. 尝试让整档无解的停播段会被拒绝

再登记 06:30–07:00Z（`start_utc=1730615400, end_utc=1730617200`）将封死最后一种
排法，接口返回 **409**，旧档版本号不变、继续可查。

### 等价 curl 流程

```bash
curl -s localhost:8000/channels -H 'content-type: application/json' -d @- <<'JSON'
{"channel_id":"ny-fm","timezone":"America/New_York","programs":[
 {"seq":1,"local_start":"2024-11-03T01:30:00","duration":900},
 {"seq":2,"local_start":"2024-11-03T01:45:00","duration":900}]}
JSON
curl -s 'localhost:8000/channels/ny-fm/query?utc_second=1730611800'
curl -s -X PUT localhost:8000/channels/ny-fm/blackouts -H 'content-type: application/json' \
  -d '{"blackout_id":"bo-1","start_utc":1730611800,"end_utc":1730613600}'
curl -s 'localhost:8000/channels/ny-fm/query?utc_second=1730615400'
```

## 一致性保证

- 每次查询在**单快照事务**里读取整档，只对应一份已提交资料；响应带 `version`，
  交错的查档/更正可按版本号核对。
- 每次写入在**单个 `BEGIN IMMEDIATE` 事务**内完成落库与可解性校验；
  整档无解（含春令时跳空的不存在本地时刻、停播段封死全部排法）或写入失败一律
  回滚，旧档案原封不动、继续可查，`version` 不增加。

## 本地测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```
