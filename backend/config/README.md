# 检查调度规则库

`medical_rules.sqlite3` 是按表 A、B、C 初始化的 SQLite 数据库，`medical_rules.sql` 包含建表和初始数据。现有 JSON 保存此前 15 项检查的配置。本数据库采用 40 项表 A 的 EX 编码，接入推荐服务前需核对医嘱编码映射。

| 表 | 用途 |
| --- | --- |
| `exam_catalog` | 40 项检查目录，包含模拟科室和测试用占用时长（分钟） |
| `physiological_conditions` | 生理状态及判断方式 |
| `exam_condition_rules` | 每个项目的准备要求、时间或前置条件、待确认项 |
| `precedence_rules` | 检查与临床事件的先后关系，保留原表 H、H/C、C、S 等级 |
| `exam_precedence_rules` | 检查 A 必须排在检查 B 前的规则，以及从 A 结束到 B 开始的建议间隔 |
| `state_transition_rules` | 改变生理状态的事件描述 |
| `exam_condition_links` | 关联检查和生理状态，关联用于索引，具体要求以规则行中的描述为准 |

初始规则为 `draft`。时间描述保留原表口径，待医院确认后再录入可执行的时段或间隔。营业时间窗一旦配置，以检查开始时刻判定。D17 的来源标为 `derived`，供核对推导结果。先后规则的端点可以是检查、临床事件或检查子步骤，事件端点保留原表名称。

项目先后表的规则编号以 `EP-` 开头，两端使用 `exam_catalog` 中的检查编码。默认在 A、B 都出现在医嘱时检查顺序。如果做 B 必须有 A 的结果，将 `requires_prior_result` 设为 1。此时 A 缺席、也无已核实结果时，B 会暂缓。建议间隔以分钟记录，未知时留空。候选集测试页会显示间隔，当前仅校验先后顺序。

科室与占用时长是模拟参数。模拟器只有五个科室，脑电图归入心电队列，眼底检查和内镜归入放射队列，睡眠监测归入呼吸队列。动态心电图的占用时长仅模拟安装与交接。

`exam_precedence.json` 保存候选集测试用的项目先后规则。两端均在医嘱中时生效，每条填写 `note`。测试服务每次生成候选顺序时重新读取文件，并与 SQLite 项目先后表合并。两处的规则编号应唯一。

在仓库根目录运行以下命令，可以从 SQL 创建一份新的数据库：

```sh
sqlite3 backend/config/medical_rules_new.sqlite3 ".read backend/config/medical_rules.sql"
```
