# 纸机换卷与纸幅分切索赔采录台

车间质量员采录纸机计数段、母卷、换卷搭接和断纸，分切员记录子卷刀位、实剪段和复称，售后人员核算客户取样能归到哪一张当前有效的子卷签发凭证。离线使用 Python 3.11+ 标准库与本地 SQLite，无安装依赖。

```sh
python -B -m paper_web --db paper.sqlite demo fixtures/demo.json
python -B -m paper_web --db paper.sqlite reel R2
python -B -m paper_web --db paper.sqlite scan D-cross
python -B -m paper_web --db paper.sqlite plan C
python -B -m paper_web --db paper.sqlite finish C
python -B -m paper_web --db paper.sqlite claim claim-7
python -B -m paper_web --db paper.sqlite recompute
python -B -m paper_web --db paper.sqlite receipts fixtures/receipts.json
python -B -m paper_web --db paper.sqlite receipt CR-1
python -B -m paper_web --db paper.sqlite settle CR-1 '{"claim-A":2,"claim-B":4}' after-sales
python -B -m paper_web --db paper.sqlite ledger
python -B -m paper_web --db paper.sqlite ingest fixtures/correction.json
python -B -m paper_web --db paper.sqlite recompute
python -B -m unittest discover -s tests -v
```

`demo` 使用新数据库，或原样重放到未修订的演示库。`ingest` 接受 JSON 数组，记录字段见 `fixtures/demo.json` 的 `capture`。可乱序导入，引用不完整的证据会在相应业务视图挂起。每类记录以 `id` 和正整数 `rev` 确定身份。同版本同内容可重放，异内容使整批回滚，最大版本参与现行重算。读取使用同一 SQLite 快照，签发在写事务内核对当前依据。

## 坐标、剪除和签发

所有长度用毫米，卷重用克，金额用整数分。纵横区间左闭右开，扫描矩形以 `[纵起, 纵止, 横起, 横止]` 表示。计数段用 `base + raw - first` 投影到单条纸机线的公共纵轴，每个计数段只覆盖其声明的原始区间。物理母卷相交时扣留冲突范围内的母卷子卷。搭接和断纸也走该公共轴，所以同一搭接可以冲击两卷。

同缺陷多次扫描按矩形并集计算。经核验且覆盖完整补丁的扫描窗才提供缺陷证据。保留纸幅必须被有效扫描窗并集完全覆盖。实剪用相对母卷卷首的纵向区间，覆盖子卷全宽。刀位冲突、无定位、剪越界或剪空、保留段上的搭接、断纸、严重缺陷、可能相关的未定位严重缺陷和未覆盖区域各自给出扣留原因。观察级缺陷单独保留几何证据。

登记复称时把 `plan` 输出中的 `plan` 值带入 `weigh LANE TICKET PLAN REV GRAMS`。同一剪刀方案只有一张有效复称票才可按含端点公差判定。签发命令 `issue LANE BASIS OPERATOR` 使用 `finish` 的 `basis`。签发将证据快照写入 `issue`，按当前依据幂等。影响该子卷的后续证据回执可使旧签发变为 `stale`。修正后需用新依据重签。

## 索赔重算

客户采样坐标可跨母卷。按已核验的客户样带并集分母切分证据；存在无法定位或未核验的样带时全额暂挂。确认缺陷落在唯一当前签发子卷、确切剪除段或其他待决区域，分别进入子卷、剪除和暂挂桶。扫描足以证明样区无已确认目标缺陷时另列该桶。报告附每块区域的来源标签，采用最大余数法分配整数分，同余数按桶 ID 排序。

样例中 A、C 有当前签发，B、D 扣留。`D-cross` 实际面积 400 mm²，跨两卷各 200 mm²。`claim-7` 的 1001 分分到 A 125、C 175、剪除 200、暂挂 501。补录 `correction.json` 后 A、C 签发均失效，`claim-7` 剩余有效剪除证据 25 分，976 分暂挂。`recompute` 从工位原始修订和签发凭证重建这些输出。

## 回单归账

售后导出的贷记与退回回单是只含整数分的本地 JSON，`receipts` 保存原件，字段与采集记录同样以 `id`、正整数 `rev` 确定身份，同版本重放、异内容整批回滚。贷记写 `{"type":"credit",...,"cents":N,"claims":[...]}`，`claims` 是该张原件的授权索赔集合；退回写 `{"type":"refund",...,"cents":N,"credit":"CR-1","credit_rev":1,"claim":"claim-A"}`，必须具名原贷记及其修订版本和索赔。

`receipt <id>` 给出当版可入账视图：每个授权索赔的可抵扣上界取自 `claims.py` 当版已确认的 `child:*` 子卷分额（暂挂 `reserve` 与剪除 `removed` 不计），已签认毛额占掉的部分列为 `available`；挂起退回要求的预留列为 `reserve`。`settle <id> '<分配 JSON>' <operator>` 只在逐项非负、授权集合内逐项齐全、合计等于原件金额、每索赔累计签认毛额不超过当版上界、且每个挂起退回都在对应索赔留足分额时签认。签认把完整明细快照（分配、上界、连带退回、逐索赔净额）写入追加式账本并以 sha256 见证锁定；同版同分配重放幂等，改分配会被锁定拒绝。贷记签认时同版本挂起的退回在同一事务连带入账。

退回只能消耗其具名贷记实际落给该索赔的分额：本轮与累计退回超过该贷记的该索赔分额即拒绝，即使另一张贷记也给过同一索赔。收据修订不重写历史——旧版签认保留可查，新版另行入账；在旧版签认之后才收到、具名旧版的退回不阻塞新版落账。若工位证据更正（如 `finishing.py` 的子卷签发经 `claims.py` 重算后失效）使已签认毛额超过当版上界，旧账原样保留，`ledger` 的 `discrepancies` 报出 `excess_cents` 差额，新回单按当版上界重新判定，不能通过重分历史抹平。
