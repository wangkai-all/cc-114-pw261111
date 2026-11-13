# SQL 数据空间分析台

零第三方依赖（仅 Python 3.11+ 标准库）的多空间 CSV 导入与只读 SQL 分析应用。

## 功能一览

- **多数据空间，彼此独立**：每个空间一个独立目录与 SQLite 数据库文件，表、查询、历史互不可见。
- **CSV 导入建表**：先全量校验列名冲突/保留字/字段数/类型，再在单事务内建表+插入；失败整体回滚，不留半张表。
- **SQL 关联查询**：支持 WITH/CTE、窗口函数、子查询、多表 JOIN、多语句（仅最后一条返回结果集）。
- **分页与下载**：结果物化为快照后分页浏览；CSV 下载带截断响应头与文件内标记。
- **执行历史**：记录每条查询的 SQL、状态、停止原因、耗时、行数与可定位到具体语句的错误。
- **只读安全沙箱**：写表、ATTACH、PRAGMA（除一个无害项）、危险函数全部在 SQLite *prepare 阶段*经 authorizer 拦截，不靠检查 SQL 开头。
- **超时与取消**：progress handler 周期检查时间预算与取消事件；中断后连接立即关闭释放，不阻塞其他查询。

## 快速开始

```bash
./run.sh              # 默认 http://127.0.0.1:8000
# 或
python3 -m app.server --host 0.0.0.0 --port 8000
```

浏览器打开后：新建空间 →「导入 CSV」或「载入样本」→ 在 SQL 查询页选择示例查询并执行。

## 测试

```bash
python3 -m unittest discover -s tests -p "test_*.py" -v
```

54 个用例覆盖：安全拦截（注释/WITH/子查询/多语句/ATTACH/PRAGMA/危险函数）、
行数截断、超时、用户取消、连接释放、导入事务原子性、空间隔离与完整 HTTP 链路。

## 安全模型（三层，均基于 SQLite 真实机制而非字符串判断）

| 防线 | 机制 | 拦截对象 |
|------|------|----------|
| 1. 只读连接 | `file:...?mode=ro` URI 打开 | 物理上无法写主库文件 |
| 2. Authorizer 回调 | `set_authorizer`，prepare 阶段逐动作裁决 | DML/DDL、事务、ATTACH/DETACH、PRAGMA、函数白名单外调用 |
| 3. Progress handler | 每 200 条 VM 指令回调 | 执行超时、用户取消 |

注释、`WITH` 后接写操作、子查询内 DML、多语句夹带等绕过手法，
都会经过同一个 authorizer 路径，在任何副作用发生前被拒绝。

### 函数白名单

仅放行确定性、无副作用的内置函数（聚合、字符串、数值、日期、窗口等）。
`load_extension`、`readfile`、`writefile`、`fsdir` 等有针对性提示，
未知函数一律 fail-closed。

### 唯一放行的 PRAGMA

`recursive_triggers`：仅用于解除递归 CTE 默认 1000 深度限制，
不泄露环境信息、不改变文件权限；其余 PRAGMA（含 `database_list`、
`journal_mode` 等）全部拒绝。

## 限额（可用环境变量覆盖）

| 配置 | 默认 | 环境变量 |
|------|------|----------|
| 查询超时 | 5000 ms | `SQLAB_QUERY_TIMEOUT_MS` |
| 返回行数上限 | 1000 | `SQLAB_QUERY_MAX_ROWS` |
| SQL 文本上限 | 64 KB | — |
| 单文件上传 | 10 MB | — |
| 单文件行数 / 列数 | 200,000 / 200 | — |
| 结果快照 TTL | 10 分钟 | — |
| 数据目录 | `./data` | `SQLAB_DATA_ROOT` |

## 目录结构

```
app/
  config.py       限额与常量
  dbguard.py      只读连接、authorizer、语句拆分、预算/取消
  queryrunner.py  异步执行、轮询、快照缓存、分页
  importer.py     CSV 校验、事务导入、表结构浏览
  spaces.py       空间注册表（每空间独立目录/锁）
  history.py      执行历史（独立 history.db，查询连接触达不到）
  samples.py      本地样本数据与预置查询（含攻击/压力用例）
  errors.py       错误类型与信息脱敏（剥离服务端私有路径）
  server.py       标准库 HTTP 路由
static/           单页前端（原生 HTML/CSS/JS）
tests/            unittest 测试套件
```

## 错误信息约定

- 错误可定位到第几条语句并附语句摘要；
- 所有面向前端的文本经过路径脱敏，不包含服务器绝对路径或配置；
- 停止原因枚举：`completed / row_limit / timeout / cancelled / denied / error`。
