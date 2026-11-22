# 同日交班的逐行复核

## 办理入口

沿用 README.md 的连接、日账、分区和输入规则。两种版本共享原盘点号、发行号及实收号的绑定规则。

- `inventory.freeze(db, day, zone, count_id, cut, *, row_evidence=False)` 和 `issue.issue_pack(db, day, issue_id, inventory_ids, target, *, row_evidence=False)` 保持原返回类型。关键字为 `True` 时办理带行级证据的 V2 业务。V2 发行要求实际选中的两份盘点都具有冻结的行级证据。按原入口领取已冻结盘点及原实发字节。
- `restore.receive_pack(db, receive_id, raw)` 登记实际来件。V2 沿用 `issue.codec` 的外层包络及正文 `manifest`、`counts`、`postings`，正文增加 `row_evidence`。行级证据内部结构由实现决定。外层正确的来件可以先登记，正文的业务核账在复核时完成。
- `restore.review_rows(db, before_receive_id, after_receive_id, review_id)` 按指定实收号签存并返回复核。两件须为同一日账的 V2 件，第一件目标严格早于第二件。`restore.read_row_review(db, review_id)` 返回已签复核，未知号返回 `None`。同一复核号同实收号对复领原结果，绑定变化拒绝。

## 逐件核账

在盘点冻结时固定其行级依据和与实际原件的绑定。已冻结的 V2 盘点可以在最早所选截点及以前的 `postings` 被清出后继续发行。续记涵盖最早所选截点之后至目标截点的全部正式号，空窗有效。历史盘点截点须能核定当时的证据。新请求证据不足时拒绝。

隔离仓只用该件承诺的材料核定自身目标。校验实际选中盘点的出处及行级证据，核对每个实际盘点的行集合与分区计量，核对正式续记的行修订、逐笔分区作用及最终恢复计量。逐笔核对也包括已经被较晚盘点覆盖的一侧和净量为零的分录。一个原行可以出现在两个不同截点的原盘点中。每个核定目标中的原行由 `(batch_id, line_id)` 唯一标识。零升仍是一条有效观测。沿用 V1 来件的信任边界，校验实际原件与目录的绑定及包内业务一致性。

两件可有重叠或留有间隔。后件续记起点不晚于前件目标时，两件须证明相同的前目标行账，续记交集里的同号正式分录和行级变更须一致。若留有间隔，按可知端点检查相容性。前件已有原行在后件仍须存在，修订号不得倒退，同修订的分区和升数须相同。V1 的三种正式业务为接纳、迁区和区内补正。

缺证、错件或证据不相容时抛 `ValueError`，不产生新的复核成果。成功时结果与指定实收件的绑定一并提交，重开隔离库按原复核号读取相同结果。已登记来件和已有成果仍可领取。旧入口继续办理 V1，V1 件申请逐行复核时按缺证拒签。

## 返回的复核

返回可用 JSON 表示的字典，包含 `review_id`、`day`、`before`、`after` 和 `rows`。顶层 `before` 与 `after` 各含该端的 `receive_id`、`issue_id`、`identity` 和 `target`，其中 `identity` 为来件内容标识。

`rows` 是按 `(batch_id, line_id)` 升序排列的列表。每项含 `batch_id`、`line_id`、`before`、`after` 和 `changes`。每端状态含 `zone`、`litres`、`revision`、`source`。若该端实际选中的对应分区原盘点已含相同修订，`source` 为 `{"count_id": 盘点号}`。否则 `source` 为 `{"seq": 确立该修订的正式号}`。出处须可在该端实收件中定位到原行，不能引用另一件。首录前的状态用 `None`。

`changes` 为字符串列表。新增为 `["added"]`。已有行迁区记 `"moved"`，改值记 `"changed"`，同时成立时依此顺序各记一次。两个端点的分区和升数均相同时记 `["same"]`，即使修订号增加。比较的是两端状态。

## 样例和运行

`python3 -B row_review_fixture.py` 用真实 V1 入口生成两种来件。批 `station` 的 `a` 初始在东，`b` 初始在西，均为 5 L。正式号 2 和 3 依次办理两行。互换场景的前端状态均为修订 1，后端为 `a` 在西、`b` 在东，均为修订 2，分别报 `moved`。等值补正场景后端仍为修订 2，分别报 `same`。两种场景的各区计量相同。

夹具中的 `staggered` 用东区截点 1 和西区截点 2 核至 3。互换场景里 `a` 同时出现在两份原盘点，后端 `a` 在西且可指向 `west-middle`，`b` 在东且可指向正式号 3。`alternate` 选择同区、同截点、同额的 `west-spare`，可供核验错换原件。`build` 接受冻结与发行的可调用对象，便于以相同输入生成 V2 夹具。`before` 和 `after` 分别为目标 1 和 3 的空续记件。

保留 `check_project.py` 的原回归及 `demo.py` 的原交班场景。在现有 `.pointwise-runtime.json` 里沿用 `version`、`kind` 和 `setup` 的约定，`verify` 与 `demo` 分别登记参数数组。核验须执行完整原回归并检验逐行交班，失败返回非零。演示展示等值互换的两端证据，并在关闭源库、重开隔离库后凭复核号复领。
