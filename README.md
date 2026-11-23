# 电台档案 UTC 查档服务

按录音的 **UTC 时间** 查节目。旧日志只记本地开播时刻，服务接收单频道日志与
**IANA 时区**，负责把本地时刻换算成 UTC；夏令时回拨导致同一本地时刻出现两次时，
**两种 UTC 时刻都会纳入核对**。

- 框架：FastAPI + SQLite（WAL 模式）
- 时间精度：本地时刻与 UTC 纪元秒均支持**最多 6 位小数（微秒）**；整秒与
  带小数秒的记录可在同一份档案中混用，内部统一用 `Decimal` 精确计算
- 时段语义：半开区间 `[start, end)`，含起点、不含终点
- 排法：每个节目从其合法 UTC 开播候选中选一个；按流水号顺序首尾不相叠、
  且不与任何停播段相交，即为一种完整排法
- 查询：给定一个 UTC 秒，返回在**至少一种完整排法**中播到该秒的节目流水号，
  以及该秒是否**可能是空档**（存在一种完整排法不播任何节目）

## 时间精度语义

| 输入 | 行为 |
| --- | --- |
| `local_start` 带小数秒，普通时刻 | 正常受理，得到一个带小数秒的 UTC 候选 |
| `local_start` 带小数秒，回拨重叠时刻 | 正常受理，得到**两个**带小数秒的 UTC 候选 |
| `local_start` 带小数秒，春令跳空（真正不存在） | 继续 **422 拒绝**，并说明本地时刻不存在 |
| `local_start` 超过 6 位小数 | 422 拒绝（不静默截断） |
| `utc_second` 为整数（如 `1730611800`） | 查询**该整秒** `[t, t+1)`，仍能命中在这一秒内（例如 `.25` 处）开播的节目 |
| `utc_second` 带小数秒（如 `1730611800.25`） | 精确到微秒的瞬时查询 |
| 停播段 `start_utc/end_utc` | 支持整秒或小数秒；半开区间 `[start, end)`，同样最多 6 位小数 |

响应中的纪元秒：整数按 JSON 整数返回；小数在二进制浮点可精确表示时按 JSON 数字
返回，否则按十进制字符串返回，避免微秒在传输中丢失。输入同时接受 JSON 数字与
十进制字符串。

旧库（`blackouts.start_utc/end_utc` 为 `INTEGER`）在启动时自动、幂等地迁移为
`TEXT` 十进制列，旧整秒数据原样保留，并与新写入的小数秒记录共用同一套比较逻辑。

## 目录结构

```
app/
  domain.py    # 时区换算、回拨双候选、DP 可解性/空档判定（O(n·k)）
  db.py        # SQLite 访问、WAL、写事务原子提交、快照读、旧库精度迁移
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
| GET | `/channels/{id}/query?utc_second=` | 查档：播出节目 + 空档判定（整秒或带小数秒） |

## 在 /docs 演示三种时刻

打开 <http://localhost:8000/docs>。`POST /channels` 的请求体已内置三个可直接
「Try it out」的示例（`demo-whole` / `demo-frac` / `demo-fold`）。

### A. 整秒（普通时刻，America/New_York）

```json
{
  "channel_id": "demo-whole",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-11-03T10:00:00", "duration": 900},
    {"seq": 2, "local_start": "2024-11-03T10:15:00", "duration": 900}
  ]
}
```

本地 10:00 EST = 15:00Z = 纪元秒 `1730646000`。查
`GET /channels/demo-whole/query?utc_second=1730646000` → `on_air=[1]`、
`may_be_gap=false`。

### B. 带小数秒（普通时刻）

```json
{
  "channel_id": "demo-frac",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-11-03T10:00:00.250000", "duration": 900},
    {"seq": 2, "local_start": "2024-11-03T10:15:00.750000", "duration": 900}
  ]
}
```

响应里 `utc_options` = `[1730646000.25]` —— 小数秒被保留，不会再被误报为春令跳空。

- `?utc_second=1730646000.249999` → `on_air=[]`（尚未开播）
- `?utc_second=1730646000.25` → `on_air=[1]`
- `?utc_second=1730646000`（整秒查询）→ `on_air=[1]`（命中覆盖该秒的节目）

### C. 回拨（本地时刻出现两次，带小数秒）

```json
{
  "channel_id": "demo-fold",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-11-03T01:30:00.250000", "duration": 900},
    {"seq": 2, "local_start": "2024-11-03T01:45:00.750000", "duration": 900}
  ]
}
```

纽约 2024-11-03 02:00 本地回拨到 01:00，本地 `01:30:00.250000` 出现两次：

- 第一次（EDT, UTC-4）→ `05:30:00.25Z` = **1730611800.25**
- 第二次（EST, UTC-5）→ `06:30:00.25Z` = **1730615400.25**

响应 `utc_options = [1730611800.25, 1730615400.25]`，`arrangement_count = 3`
（早早 / 早晚 / 晚晚）。两个候选都参与排法与查档。

### D. 真正不存在的本地时刻继续拒绝

```json
{
  "channel_id": "demo-gap",
  "timezone": "America/New_York",
  "programs": [
    {"seq": 1, "local_start": "2024-03-10T02:30:00.500000", "duration": 600}
  ]
}
```

2024-03-10 02:30 在纽约不存在（春令跳空），接口返回 **422**，频道不会被创建。

### 等价 curl 流程

```bash
B=http://localhost:8000
curl -s $B/channels -H 'content-type: application/json' -d '{
 "channel_id":"demo-fold","timezone":"America/New_York","programs":[
 {"seq":1,"local_start":"2024-11-03T01:30:00.250000","duration":900},
 {"seq":2,"local_start":"2024-11-03T01:45:00.750000","duration":900}]}'
curl -s "$B/channels/demo-fold/query?utc_second=1730611800.25"
curl -s "$B/channels/demo-fold/query?utc_second=1730611800"
curl -s "$B/channels/demo-fold/query?utc_second=1730615400.25"
# 带小数秒的停播段
curl -s -X PUT $B/channels/demo-fold/blackouts -H 'content-type: application/json' \
  -d '{"blackout_id":"bo-1","start_utc":1730611800.25,"end_utc":1730613600.75}'
```

## 停播段与无解保护（沿用原有语义）

以整秒日志 `ny-fm`（`2024-11-03T01:30:00` / `01:45:00`）为例：

1. 登记 05:30–06:00Z（`start_utc=1730611800, end_utc=1730613600`）封掉两种含早
   候选的排法，只剩「晚晚」一种；查 05:30Z 得 `on_air=[]`、`may_be_gap=true`、
   `in_blackout=true`；查 06:30Z（1730615400）得 `on_air=[1]`、`may_be_gap=false`。
2. 再登记 06:30–07:00Z（`start_utc=1730615400, end_utc=1730617200`）将封死最后一种
   排法，接口返回 **409**；旧档版本号不变、继续可查。上传无解日志同样返回 409 且
   不改变已提交档案。

## 一致性保证

- 每次查询在**单快照事务**里读取整档，只对应一份已提交资料；响应带 `version`，
  交错的查档/更正可按版本号核对。
- 每次写入在**单个 `BEGIN IMMEDIATE` 事务**内完成落库与可解性校验；
  整档无解（含春令时跳空的不存在本地时刻、停播段封死全部排法）或写入失败一律
  回滚，旧档案原封不动、继续可查，`version` 不增加。
- 时间比较全程使用精确的 `Decimal` 纪元秒：旧整秒记录与新小数秒记录按数值
  （而非文本）比较，节目先后、半开区间与停播冲突判断一致。

## 本地测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖：DP 与暴力枚举在整秒/小数秒边界上的一致性、回拨双候选（含小数秒）、
春令跳空拒绝、整数查询命中小数秒节目、SQLite 往返、旧库整秒数据与小数秒数据
混用、以及无解上传/停播段回滚不改动已提交档案。
