# 流域监测分区日仓

供数站提交观测批，白班账员更正读数和归区，库管冻结分区盘点并发行交班材料。夜班仅凭隔离仓实收件签存日终，次班按原号领取账面、核账和首执。

## 运行

使用 Python 3.11 标准库。项目根目录的 `python3 -B check_project.py` 执行完整回归。该入口由运行平台随工程提供。`python3 -B demo.py` 用两个临时 SQLite 库办理交班，在删除源库后恢复，并重开隔离库复领。演示中的东区在一条 5 L 时盘点，随后该条转入西区按 7 L 记录，补正到 8 L 后西区盘点。日终东区空账，西区一条 8 L。

## 办理顺序

1. 供数站提交日账、批号和有序具名原行。原行名在批内唯一。
2. 白班点收合法整批，一次提交原输入、正式分录、现行修订和首执。原样重投复领原首执。
3. 白班凭原行的现行修订办理跨区转记，两区作用一起生效。
4. 白班可在现有分区补正计量，零净补正也计入正式序。
5. 库管按日账、分区和正式截点冻结盘点。复领使用盘点号。
6. 库管选择东、西各一份原盘点和目标截点，发行具名交班件。
7. 夜班将实际收到的交班件登记到独立隔离库，记录实收号和内容标识。
8. 夜班核对指定来件及目标，按区续记并一次签存分区账、逐笔核账和首执。
9. 次班按恢复号复领原成果。

## 公开接口

下表的 `db` 是调用者打开的 `sqlite3.Connection`，使用默认元组行格式。源仓和隔离仓分别调用 `journal.initialize_source(db)` 和 `restore.initialize_isolated(db)`。初始化创建缺失结构，保留已有业务数据。调用侧管理连接寿命。业务写入口自行提交，调用前连接应无外层事务。读取函数使用同一职责的对应库。

| 接口 | 输入与结果 |
| --- | --- |
| `intake.submit(db, request)` | `BatchRequest(day, batch_id, readings)`，`readings` 为有序 `tuple[ReadingInput, ...]`，每项为 `ReadingInput(line_id, zone, litres)`。返回 `IntakeReceipt`。同日同批同参复领，同号换值、重排行或非法行拒绝。 |
| `intake.read_intake_receipt(db, day, batch_id)` | 按日账与批号复领首执。 |
| `journal.read_current_revision(db, day, batch_id, line_id)` | 读取原行的现行修订。 |
| `journal.transfer(db, day, expected, destination, litres)` | 以读取的 `ObservationRevision` 为前置条件，目的区须不同于出区。返回含两侧作用的 `Posting`。旧修订、串日或非法输入拒绝。 |
| `journal.correct_litres(db, day, expected, litres)` | 以现行修订为前置条件办理区内补正，返回 `Posting`。 |
| `journal.balance_at_cut(db, day, zone, cut)` | 从可查证分录汇总该正式截点的 `ZoneBalance`。 |
| `inventory.freeze(db, day, zone, count_id, cut)` | 以新盘点号保存 `InventoryRecord`。未知或无法计量的截点拒绝。 |
| `inventory.read_inventory(db, count_id)` | 凭盘点号读取原件。 |
| `issue.issue_pack(db, day, issue_id, inventory_ids, target)` | 输入新发行号、东、西各一份盘点号和目标截点。返回 `IssuedPackage`，其 `raw` 是原传输字节。缺件、重区、串日、错截点或缺续记拒绝。 |
| `issue.read_issued_package(db, issue_id)` | 读取原实发件和原窗口。 |
| `restore.receive_pack(db, receive_id, raw)` | 返回 `ReceivedPackage`。原实收号与原字节可重投，实收号或同日发行身份的绑定冲突拒收。 |
| `restore.read_received_package(db, receive_id)` | 读回固定实收版本。 |
| `restore.restore_day(db, receive_id, target, restore_id)` | 核账并签存 `RestoredDay`。同一恢复号与原实收号、目标可复领，绑定变化拒绝。 |
| `restore.read_day(db, restore_id)` | 按指定恢复号读取原 `RestoredDay`，含 `reconciliation` 和 `receipt`。 |

读取未知号返回 `None`。输入、身份或核账错误以 `ValueError` 报告，数据库拒绝则保留 SQLite 错误。被拒绝的点收、更正、发行或签存不会留下部分成果。隔离仓允许先登记外层正确的交班件，再在恢复时核对正文的业务完整性。

## 计量与交换

每个正式日账采用 `YYYY-MM-DD`，分区标识为 `east` 和 `west`。同一个日账只有一条正式源序，正式号从 1 连续分配，截点 0 表示已登记日账的日初。读数为非负整数升，缺失、空值、布尔值、非整数或负读数整批拒绝。首投每行加一条和原升数，跨区转记分别扣除一条旧值、增加一条目的值，同区补正只记升数差。账面使用整数精确累加并显式列出空区。零升有效观测仍计一条。

盘点原件绑定日账、分区、盘点号、截点、条数、升数和出处。后续更正不改变冻结原件。交班件外层版本为 1，包含类型、正文及按规范 JSON 求得的 SHA-256 内容标识。正文包括盘点目录、按号交付的原件计量和源序续记。目录将实际盘点身份及截点与原件标识绑定，传输件由交班渠道提供。续记从最早所选盘点之后开始，连续到目标并包含目标。空窗仍保有日账和截点。盘点之前的源分录移出保留窗后，已有盘点与其后的完整续记仍可发行。重新计算盘点需要从日初到所求截点的完整分录。

隔离恢复用各区自己的盘点截点判定每侧作用是否已覆盖。核账按正式号返回所有续记事务，每侧列出作用、覆盖结论、盘点号、原件标识和出处。整份结果另保留实收号、发行号、内容标识和目标截点。等额盘点须按原身份核对，零净事务与其他正式事务一样保留。账面、核账、出处和首执共同提交，重开后按指定恢复号读取。

## 回归场景

- `tests/test_intake.py` 核对原批复领、异参和换序拒绝、同值异批、非法整批回滚及重开。
- `tests/test_journal.py` 核对原子转记的两侧计量、旧修订与串日拒绝。
- `tests/test_issue.py` 核对冻结盘点、同区补正、实发原件复领、空窗及无效发行目标。
- `tests/test_restore.py` 核对同截点恢复、反向错峰、停源、等额换件、漏序、实收冲突、签存失败、重试及历史领取。
